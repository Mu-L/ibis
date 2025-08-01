from __future__ import annotations

import sqlglot as sg
import sqlglot.expressions as sge

import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
from ibis import util
from ibis.backends.sql.compilers.base import NULL, STAR, SQLGlotCompiler
from ibis.backends.sql.datatypes import ImpalaType
from ibis.backends.sql.dialects import Impala
from ibis.backends.sql.rewrites import (
    FirstValue,
    LastValue,
    lower_sample,
    rewrite_empty_order_by_window,
    split_select_distinct_with_order_by,
)


class ImpalaCompiler(SQLGlotCompiler):
    __slots__ = ()

    dialect = Impala
    type_mapper = ImpalaType
    rewrites = (
        rewrite_empty_order_by_window,
        *SQLGlotCompiler.rewrites,
    )
    post_rewrites = (split_select_distinct_with_order_by,)

    LOWERED_OPS = {
        ops.Sample: lower_sample(
            supported_methods=("block",), physical_tables_only=True
        ),
    }

    UNSUPPORTED_OPS = (
        ops.ArrayPosition,
        ops.Array,
        ops.ArgMax,
        ops.ArgMin,
        ops.Covariance,
        ops.ExtractDayOfYear,
        ops.Levenshtein,
        ops.Map,
        ops.Median,
        ops.NthValue,
        ops.RegexSplit,
        ops.RowID,
        ops.StringSplit,
        ops.StringToTime,
        ops.StructColumn,
        ops.Time,
        ops.TimeDelta,
        ops.TimestampBucket,
        ops.TimestampDelta,
        ops.Unnest,
        ops.Kurtosis,
    )

    SIMPLE_OPS = {
        ops.All: "min",
        ops.Any: "max",
        ops.ApproxMedian: "appx_median",
        ops.BaseConvert: "conv",
        ops.BitwiseAnd: "bitand",
        ops.BitwiseLeftShift: "shiftleft",
        ops.BitwiseNot: "bitnot",
        ops.BitwiseOr: "bitor",
        ops.BitwiseRightShift: "shiftright",
        ops.BitwiseXor: "bitxor",
        ops.Cot: "cot",
        ops.DayOfWeekName: "dayname",
        ops.ExtractEpochSeconds: "unix_timestamp",
        ops.Hash: "fnv_hash",
        ops.Ln: "ln",
        ops.TypeOf: "typeof",
        ops.RegexReplace: "regexp_replace",
    }

    @staticmethod
    def _minimize_spec(op, spec):
        if isinstance(func := op.func, ops.Analytic) and not isinstance(
            func, (ops.First, ops.Last, FirstValue, LastValue, ops.NthValue)
        ):
            return None
        return spec

    def visit_Log2(self, op, *, arg):
        return self.f.anon.log2(arg)

    def visit_Log10(self, op, *, arg):
        return self.f.anon.log10(arg)

    def visit_Literal(self, op, *, value, dtype):
        if value is None and dtype.is_binary():
            return NULL
        return super().visit_Literal(op, value=value, dtype=dtype)

    def visit_CountStar(self, op, *, arg, where):
        if where is not None:
            return self.f.sum(self.cast(where, op.dtype))
        return self.f.count(STAR)

    def visit_CountDistinctStar(self, op, *, arg, where):
        expressions = (
            sg.column(name, table=arg.alias_or_name, quoted=self.quoted)
            for name in op.arg.schema.keys()
        )
        if where is not None:
            expressions = (self.if_(where, expr, NULL) for expr in expressions)
        return self.f.count(sge.Distinct(expressions=list(expressions)))

    def visit_CountDistinct(self, op, *, arg, where):
        if where is not None:
            arg = self.if_(where, arg, NULL)
        return self.f.count(sge.Distinct(expressions=[arg]))

    def visit_Xor(self, op, *, left, right):
        return sg.and_(sg.or_(left, right), sg.not_(sg.and_(left, right)))

    def visit_RandomScalar(self, op):
        return self.f.rand(self.f.utc_to_unix_micros(self.f.utc_timestamp()))

    def visit_DayOfWeekIndex(self, op, *, arg):
        return self.f.pmod(self.f.dayofweek(arg) - 2, 7)

    def visit_ExtractMillisecond(self, op, *, arg):
        return self.f.extract(self.v.millisecond, arg) % 1_000

    def visit_ExtractMicrosecond(self, op, *, arg):
        return self.f.extract(self.v.microsecond, arg) % 1_000_000

    def visit_Degrees(self, op, *, arg):
        return 180.0 * arg / self.f.pi()

    def visit_Radians(self, op, *, arg):
        return self.f.pi() * arg / 180.0

    def visit_HashBytes(self, op, *, arg, how):
        if how not in ("md5", "sha1", "sha256", "sha512"):
            raise com.UnsupportedOperationError(how)
        return self.f[how](arg)

    def visit_Log(self, op, *, arg, base):
        if base is None:
            return self.f.ln(arg)
        return self.f.log(base, arg)

    def visit_DateFromYMD(self, op, *, year, month, day):
        return self.cast(
            self.f.concat(
                self.f.lpad(self.cast(year, dt.string), 4, "0"),
                "-",
                self.f.lpad(self.cast(month, dt.string), 2, "0"),
                "-",
                self.f.lpad(self.cast(day, dt.string), 2, "0"),
            ),
            dt.date,
        )

    def visit_NonNullLiteral(self, op, *, value, dtype):
        if dtype.is_date() or dtype.is_timestamp():
            # hack to return a string literal because impala doesn't support a
            # wide range of properly-typed date values
            #
            # the date implementation is very unpolished: some proper dates are
            # supported, but only within a certain range, and the
            # implementation wraps on over- and underflow
            return sge.convert(value.isoformat())
        elif dtype.is_decimal() and not value.is_finite():
            raise com.UnsupportedOperationError(
                f"Non-finite decimal literal values are not supported by Impala; got: {value}"
            )
        elif dtype.is_array() or dtype.is_map() or dtype.is_struct():
            raise com.UnsupportedBackendType(
                f"Impala does not support {dtype.__class__.__name__.lower()} literals"
            )
        elif dtype.is_uuid():
            return sge.convert(str(value))
        return None

    def visit_Cast(self, op, *, arg, to):
        from_ = op.arg.dtype
        if from_.is_temporal() and to.is_integer():
            return 1_000_000 * self.f.unix_timestamp(arg)
        return super().visit_Cast(op, arg=arg, to=to)

    def visit_StartsWith(self, op, *, arg, start):
        return arg.like(self.f.concat(start, "%"))

    def visit_EndsWith(self, op, *, arg, end):
        return arg.like(self.f.concat("%", end))

    def visit_FindInSet(self, op, *, needle, values):
        return self.f.find_in_set(needle, self.f.concat_ws(",", *values))

    def visit_ExtractUrlField(self, op, *, arg):
        return self.f.parse_url(arg, type(op).__name__[len("Extract") :].upper())

    visit_ExtractAuthority = visit_ExtractHost = visit_ExtractUserInfo = (
        visit_ExtractProtocol
    ) = visit_ExtractFile = visit_ExtractPath = visit_ExtractUrlField

    def visit_ExtractQuery(self, op, *, arg, key):
        return self.f.parse_url(*filter(None, (arg, "QUERY", key)))

    def visit_ExtractFragment(self, op, *, arg):
        return self.f.parse_url(arg, "REF")

    def visit_StringFind(self, op, *, arg, substr, start, end):
        if start is not None:
            return self.f.locate(substr, arg, start + 1)
        return self.f.locate(substr, arg)

    def visit_StringContains(self, op, *, haystack, needle):
        return self.f.locate(needle, haystack) > 0

    def visit_TimestampDiff(self, op, *, left, right):
        return self.f.unix_timestamp(left) - self.f.unix_timestamp(right)

    def visit_Strftime(self, op, *, arg, format_str):
        if not isinstance(op.format_str, ops.Literal):
            raise com.UnsupportedOperationError(
                "strftime format string must be a literal; "
                f"got: {type(op.format_str).__name__}"
            )
        format_str = sg.time.format_time(
            op.format_str.value, {v: k for k, v in Impala.TIME_MAPPING.items()}
        )
        return self.f.anon.from_unixtime(
            self.f.unix_timestamp(self.cast(arg, dt.string)), format_str
        )

    def visit_ExtractWeekOfYear(self, op, *, arg):
        return self.f.anon.weekofyear(arg)

    def visit_TimestampTruncate(self, op, *, arg, unit):
        units = {
            "Y": "YEAR",
            "M": "MONTH",
            "W": "WEEK",
            "D": "DAY",
            "h": "HOUR",
            "m": "MINUTE",
            "s": "SECOND",
            "ms": "MILLISECONDS",
            "us": "MICROSECONDS",
        }
        if unit.short == "Q":
            return self.f.anon.trunc(arg, "Q")
        if (impala_unit := units.get(unit.short)) is None:
            raise com.UnsupportedOperationError(
                f"{unit!r} unit is not supported in timestamp/date truncate"
            )
        return self.f.date_trunc(impala_unit, arg)

    def visit_DateTruncate(self, op, *, arg, unit):
        if unit.short == "Q":
            return self.f.anon.trunc(arg, "Q")
        return self.f.date_trunc(unit.name.upper(), arg)

    def visit_TimestampFromUNIX(self, op, *, arg, unit):
        arg = self.cast(util.convert_unit(arg, unit.short, "s"), dt.int32)
        return self.cast(
            self.f.anon.from_unixtime(arg, "yyyy-MM-dd HH:mm:ss"), dt.timestamp
        )

    def visit_DateAdd(self, op, *, left, right):
        return self.cast(
            super().visit_DateAdd(op, left=self.cast(left, dt.date), right=right),
            dt.date,
        )

    def visit_TimestampAdd(self, op, *, left, right):
        if not isinstance(right, sge.Interval):
            raise com.UnsupportedOperationError(
                "right operand to timestamp add operation must be a literal"
            )

        return self.cast(
            super().visit_TimestampAdd(
                op, left=self.cast(left, dt.timestamp), right=right
            ),
            dt.timestamp,
        )

    def visit_DateDiff(self, op, *, left, right):
        return self.f.anon.datediff(left, right)

    def visit_Date(self, op, *, arg):
        return self.cast(self.f.to_date(arg), dt.date)

    def visit_RegexExtract(self, op, *, arg, pattern, index):
        return self.f.anon.regexp_extract(arg, pattern, index)

    def visit_Round(self, op, *, arg, digits):
        rounded = self.f.round(*filter(None, (arg, digits)))

        dtype = op.dtype
        if dtype.is_integer():
            return self.cast(rounded, dtype)
        return rounded

    def visit_Sign(self, op, *, arg):
        sign = self.f.sign(arg)
        dtype = op.dtype
        if not dtype.is_float32():
            return self.cast(sign, dtype)
        return sign

    def visit_DateDelta(self, op, *, left, right, part):
        if not isinstance(part, sge.Literal):
            raise com.UnsupportedOperationError(
                "Only literal `part` values are supported for date delta"
            )
        if part.this != "day":
            raise com.UnsupportedOperationError(
                f"Only 'day' part is supported for date delta in the {self.dialect} backend"
            )
        return self.f.datediff(left, right)

    def visit_Strip(self, op, *, arg):
        # Impala's `TRIM` doesn't allow specifying characters to trim off, unlike
        # Impala's `RTRIM` and `LTRIM` which accept a set of characters to
        # remove.
        return self.visit_RStrip(op, arg=self.visit_LStrip(op, arg=arg))


compiler = ImpalaCompiler()
