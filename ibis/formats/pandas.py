from __future__ import annotations

import contextlib
import datetime
from functools import partial
from importlib.util import find_spec as _find_spec
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pandas.api.types as pdt

import ibis.expr.datatypes as dt
import ibis.expr.schema as sch
from ibis import util
from ibis.common.numeric import normalize_decimal
from ibis.common.temporal import normalize_timezone
from ibis.formats import DataMapper, SchemaMapper, TableProxy
from ibis.formats.numpy import NumpyType
from ibis.formats.pyarrow import PyArrowData, PyArrowSchema, PyArrowType

if TYPE_CHECKING:
    from collections.abc import Iterable

    import polars as pl
    import pyarrow as pa
    from pandas.api.extensions import ExtensionDtype

geospatial_supported = _find_spec("geopandas") is not None


class PandasType(NumpyType):
    @classmethod
    def to_ibis(cls, typ, nullable=True):
        if isinstance(typ, pdt.DatetimeTZDtype):
            return dt.Timestamp(timezone=str(typ.tz), nullable=nullable)
        elif pdt.is_datetime64_dtype(typ):
            return dt.Timestamp(nullable=nullable)
        elif isinstance(typ, pdt.CategoricalDtype):
            if typ.categories is None or pdt.is_string_dtype(typ.categories):
                return dt.String(nullable=nullable)
            return cls.to_ibis(typ.categories.dtype, nullable=nullable)
        elif pdt.is_extension_array_dtype(typ):
            if isinstance(typ, pd.ArrowDtype):
                return PyArrowType.to_ibis(typ.pyarrow_dtype, nullable=nullable)
            else:
                name = typ.__class__.__name__.replace("Dtype", "")
                klass = getattr(dt, name)
                return klass(nullable=nullable)
        else:
            return super().to_ibis(typ, nullable=nullable)

    @classmethod
    def from_ibis(cls, dtype) -> np.dtype | pd.Ex:
        if dtype.is_timestamp() and dtype.timezone:
            return pdt.DatetimeTZDtype("ns", dtype.timezone)
        elif dtype.is_date():
            return np.dtype("M8[s]")
        elif dtype.is_interval():
            return np.dtype(f"timedelta64[{dtype.unit.short}]")
        else:
            return super().from_ibis(dtype)


class PandasSchema(SchemaMapper):
    @classmethod
    def to_ibis(
        cls, pandas_schema: pd.Series | Iterable[tuple[str, np.dtype | ExtensionDtype]]
    ) -> sch.Schema:
        if isinstance(pandas_schema, pd.Series):
            pandas_schema = pandas_schema.to_list()

        fields = {name: PandasType.to_ibis(t) for name, t in pandas_schema}

        return sch.Schema(fields)

    @classmethod
    def from_ibis(
        cls, schema: sch.Schema
    ) -> list[tuple[str, np.dtype | ExtensionDtype]]:
        names = schema.names
        types = [PandasType.from_ibis(t) for t in schema.types]
        return list(zip(names, types))


class PandasData(DataMapper):
    @classmethod
    def infer_scalar(cls, s):
        return PyArrowData.infer_scalar(s)

    @classmethod
    def infer_column(cls, s):
        return PyArrowData.infer_column(s)

    @classmethod
    def infer_table(cls, df):
        pairs = []
        for column_name in df.dtypes.keys():
            if not isinstance(column_name, str):
                raise TypeError(
                    "Column names must be strings to ingest a pandas DataFrame"
                )

            pandas_column = df[column_name]
            pandas_dtype = pandas_column.dtype
            if pandas_dtype == np.object_:
                ibis_dtype = cls.infer_column(pandas_column)
            else:
                ibis_dtype = PandasType.to_ibis(pandas_dtype)

            pairs.append((column_name, ibis_dtype))

        return sch.Schema.from_tuples(pairs)

    concat = staticmethod(pd.concat)

    @classmethod
    def convert_table(cls, df, schema):
        if schema.names != tuple(df.columns):
            raise ValueError("schema names don't match input data columns")

        columns = {
            name: cls.convert_column(df[name], dtype) for name, dtype in schema.items()
        }
        df = pd.DataFrame(columns)

        if geospatial_supported:
            from geopandas import GeoDataFrame
            from geopandas.array import GeometryDtype

            if (
                # pluck out the first geometry column if it exists
                geom := next(
                    (
                        name
                        for name, c in df.items()
                        if isinstance(c.dtype, GeometryDtype)
                    ),
                    None,
                )
            ) is not None:
                return GeoDataFrame(df, geometry=geom)
        return df

    @classmethod
    def convert_column(cls, obj, dtype):
        pandas_type = PandasType.from_ibis(dtype)

        method_name = f"convert_{dtype.__class__.__name__}"
        convert_method = getattr(cls, method_name, cls.convert_default)

        result = convert_method(obj, dtype, pandas_type)
        assert not isinstance(result, np.ndarray), f"{convert_method} -> {type(result)}"
        return result

    @classmethod
    def convert_scalar(cls, obj, dtype):
        df = PandasData.convert_table(obj, sch.Schema({str(obj.columns[0]): dtype}))
        value = df.iat[0, 0]

        if dtype.is_array():
            try:
                return value.tolist()
            except AttributeError:
                return value

        try:
            return value.item()
        except AttributeError:
            return value

    @classmethod
    def convert_GeoSpatial(cls, s, dtype, pandas_type):
        import geopandas as gpd

        if isinstance(s.dtype, gpd.array.GeometryDtype):
            return gpd.GeoSeries(s)
        return gpd.GeoSeries.from_wkb(s)

    convert_Point = convert_LineString = convert_Polygon = convert_MultiLineString = (
        convert_MultiPoint
    ) = convert_MultiPolygon = convert_GeoSpatial

    @classmethod
    def convert_default(cls, s, dtype, pandas_type):
        if s.dtype == pandas_type and dtype.is_primitive():
            return s
        try:
            return s.astype(pandas_type)
        except Exception:  # noqa: BLE001
            return s

    @classmethod
    def convert_Boolean(cls, s, dtype, pandas_type):
        if s.empty:
            return s.astype(pandas_type)
        elif pdt.is_object_dtype(s.dtype):
            return s
        elif s.dtype != pandas_type:
            return s.map(bool, na_action="ignore")
        else:
            return s

    @classmethod
    def convert_Timestamp(cls, s, dtype, pandas_type):
        if isinstance(pandas_type, pd.DatetimeTZDtype) and isinstance(
            s.dtype, pd.DatetimeTZDtype
        ):
            return s if s.dtype == pandas_type else s.dt.tz_convert(dtype.timezone)
        elif pdt.is_datetime64_dtype(s.dtype):
            return s.dt.tz_localize(dtype.timezone)
        else:
            try:
                return s.astype(pandas_type)
            except pd.errors.OutOfBoundsDatetime:  # uncovered
                try:
                    from dateutil.parser import parse as date_parse

                    return s.map(date_parse, na_action="ignore")
                except TypeError:
                    return s
            except (ValueError, TypeError):
                try:
                    return pd.to_datetime(s).dt.tz_convert(dtype.timezone)
                except TypeError:
                    return pd.to_datetime(s).dt.tz_localize(dtype.timezone)

    @classmethod
    def convert_Date(cls, s, dtype, pandas_type):
        if isinstance(s.dtype, pd.DatetimeTZDtype):
            s = s.dt.tz_convert("UTC").dt.tz_localize(None)

        try:
            return s.astype(pandas_type)
        except (ValueError, TypeError, pd._libs.tslibs.OutOfBoundsDatetime):

            def try_date(v):
                if isinstance(v, datetime.date):
                    return pd.Timestamp(v)
                elif isinstance(v, str):
                    if v.endswith("Z"):
                        datetime_obj = datetime.datetime.fromisoformat(v[:-1])
                    else:
                        datetime_obj = datetime.datetime.fromisoformat(v)
                    return pd.Timestamp(datetime_obj)
                else:
                    return v

            return s.map(try_date, na_action="ignore")

    @classmethod
    def convert_Interval(cls, s, dtype, pandas_type):
        values = s.values
        try:
            result = values.astype(pandas_type)
        except ValueError:  # can happen when `column` is DateOffsets  # uncovered
            result = s
        else:
            result = s.__class__(result, index=s.index, name=s.name)
        return result

    @classmethod
    def convert_String(cls, s, dtype, pandas_type):
        return s.astype(pandas_type, errors="ignore")

    @classmethod
    def convert_Decimal(cls, s, dtype, pandas_type):
        func = partial(
            normalize_decimal,
            precision=dtype.precision,
            scale=dtype.scale,
            strict=False,
        )
        return s.map(func, na_action="ignore")

    @classmethod
    def convert_UUID(cls, s, dtype, pandas_type):
        return s.map(cls.get_element_converter(dtype), na_action="ignore")

    @classmethod
    def convert_Struct(cls, s, dtype, pandas_type):
        return s.map(cls.get_element_converter(dtype), na_action="ignore")

    @classmethod
    def convert_Array(cls, s, dtype, pandas_type):
        return s.map(cls.get_element_converter(dtype), na_action="ignore")

    @classmethod
    def convert_Map(cls, s, dtype, pandas_type):
        return s.map(cls.get_element_converter(dtype), na_action="ignore")

    @classmethod
    def convert_JSON(cls, s, dtype, pandas_type):
        return s.map(cls.get_element_converter(dtype), na_action="ignore").astype(
            "object"
        )

    @classmethod
    def get_element_converter(cls, dtype):
        name = f"convert_{type(dtype).__name__}_element"
        funcgen = getattr(cls, name, lambda _: lambda x: x)
        return funcgen(dtype)

    @classmethod
    def convert_Struct_element(cls, dtype):
        converters = tuple(map(cls.get_element_converter, dtype.types))

        def convert(values, names=dtype.names, converters=converters):
            if values is None:
                return values

            items = (
                values.items()
                if isinstance(values, dict)
                else zip(names, util.promote_list(values))
            )
            return {
                k: converter(v) if v is not None else v
                for converter, (k, v) in zip(converters, items)
            }

        return convert

    @classmethod
    def convert_JSON_element(cls, _):
        import json

        def convert(value):
            if value is None:
                return value
            try:
                return json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return value

        return convert

    @classmethod
    def convert_Timestamp_element(cls, dtype):
        def converter(value, dtype=dtype):
            if value is None:
                return value

            with contextlib.suppress(AttributeError):
                value = value.item()

            if isinstance(value, int):
                # this can only mean a numpy or pandas timestamp because they
                # both support nanosecond precision
                #
                # when the precision is less than or equal to the value
                # supported by Python datetime.dateimte a call to .item() will
                # return a datetime.datetime but when the precision is higher
                # than the value supported by Python the value is an integer
                #
                # TODO: can we do better than implicit truncation to microseconds?
                import dateutil

                value = pd.Timestamp.fromtimestamp(value / 1e9, dateutil.tz.UTC)

            if (tz := dtype.timezone) is not None:
                value = pd.Timestamp(value)
                normed_tz = normalize_timezone(tz)
                if value.tzinfo is None:
                    return value.tz_localize(normed_tz)
                return value.tz_convert(normed_tz)

            return value.replace(tzinfo=None)

        return converter

    @classmethod
    def convert_Array_element(cls, dtype):
        convert_value = cls.get_element_converter(dtype.value_type)

        def convert(values):
            if values is None:
                return values

            return [
                convert_value(value) if value is not None else value for value in values
            ]

        return convert

    @classmethod
    def convert_Map_element(cls, dtype):
        convert_key = cls.get_element_converter(dtype.key_type)
        convert_value = cls.get_element_converter(dtype.value_type)

        def convert(raw_row):
            if raw_row is None:
                return raw_row

            row = dict(raw_row)
            return dict(
                zip(map(convert_key, row.keys()), map(convert_value, row.values()))
            )

        return convert

    @classmethod
    def convert_UUID_element(cls, _):
        from uuid import UUID

        def convert(value):
            if value is None:
                return value
            elif isinstance(value, UUID):
                return value
            elif isinstance(value, bytes):
                return UUID(bytes=value)
            return UUID(value)

        return convert


class PandasDataFrameProxy(TableProxy[pd.DataFrame]):
    def to_frame(self) -> pd.DataFrame:
        return self.obj

    def to_pyarrow(self, schema: sch.Schema) -> pa.Table:
        from decimal import Decimal

        import pyarrow as pa
        import pyarrow_hotfix  # noqa: F401

        pyarrow_schema = PyArrowSchema.from_ibis(schema)

        obj = self.obj
        if decimal_cols := [
            name for name, dtype in schema.items() if dtype.is_decimal()
        ]:
            obj = obj.assign(**{col: obj[col].map(Decimal) for col in decimal_cols})

        return pa.Table.from_pandas(obj, schema=pyarrow_schema)

    def to_polars(self, schema: sch.Schema) -> pl.DataFrame:
        import polars as pl

        from ibis.formats.polars import PolarsSchema

        pl_schema = PolarsSchema.from_ibis(schema)
        return pl.from_pandas(self.obj, schema_overrides=pl_schema)
