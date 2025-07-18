"""Ibis utility functions."""

from __future__ import annotations

import base64
import collections
import collections.abc
import contextlib
import functools
import importlib.metadata
import itertools
import operator
import os
import re
import sys
import tempfile
import textwrap
import types
import uuid
import warnings
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Generic, TypeVar
from uuid import uuid4

import toolz
from packaging.version import parse as vparse

from ibis.common.typing import Coercible

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from pathlib import Path

    from packaging.version import Version

    import ibis.expr.types as ir

T = TypeVar("T", covariant=True)
S = TypeVar("S", bound=T, covariant=True)
U = TypeVar("U", covariant=True)
K = TypeVar("K")
V = TypeVar("V")


VERTICAL_ELLIPSIS = "⋮"
HORIZONTAL_ELLIPSIS = "…"


def guid() -> str:
    """Return a uuid4 hexadecimal value."""
    return uuid4().hex


def indent(text: str, spaces: int) -> str:
    """Apply an indentation using the given spaces into the given text.

    Parameters
    ----------
    text
        Text to indent
    spaces
        Number of leading spaces per line

    Returns
    -------
    str
        Indented text

    """
    prefix = " " * spaces
    return textwrap.indent(text, prefix=prefix)


def is_one_of(values: Sequence[T], t: type[U]) -> Iterator[bool]:
    """Check if the type of each value is the same of the given type.

    Parameters
    ----------
    values
        Input values
    t
        Type to check against

    Returns
    -------
    tuple

    """
    return (isinstance(x, t) for x in values)


any_of = toolz.compose(any, is_one_of)
all_of = toolz.compose(all, is_one_of)


def promote_list(val: V | Iterable[V]) -> list[V]:
    """Ensure that the value is a list.

    Parameters
    ----------
    val
        Value to promote

    Returns
    -------
    list

    """
    if isinstance(val, list):
        return val
    elif isinstance(val, dict):
        return [val]
    elif is_iterable(val):
        return list(val)
    elif val is None:
        return []
    else:
        return [val]


def promote_tuple(val: V | Iterable[V]) -> tuple[V]:
    """Ensure that the value is a tuple.

    Parameters
    ----------
    val
        Value to promote

    Returns
    -------
    tuple

    """
    if isinstance(val, tuple):
        return val
    elif is_iterable(val):
        return tuple(val)
    elif val is None:
        return ()
    else:
        return (val,)


def is_function(v: Any) -> bool:
    """Check if the given object is a function.

    Returns
    -------
    bool
        Whether `v` is a function

    """
    return isinstance(v, (types.FunctionType, types.LambdaType))


def log(msg: str) -> None:
    """Log `msg` using `options.verbose_log` if set, otherwise `print`."""
    from ibis.config import options

    if options.verbose:
        (options.verbose_log or print)(msg)


def is_iterable(o: Any) -> bool:
    """Return whether `o` is iterable and not a :class:`str` or :class:`bytes`.

    Parameters
    ----------
    o : object
        Any python object

    Returns
    -------
    bool

    Examples
    --------
    >>> is_iterable("1")
    False
    >>> is_iterable(b"1")
    False
    >>> is_iterable(iter("1"))
    True
    >>> is_iterable(i for i in range(1))
    True
    >>> is_iterable(1)
    False
    >>> is_iterable([])
    True

    """
    if isinstance(o, (str, bytes)):
        return False

    try:
        iter(o)
    except TypeError:
        return False
    else:
        return True


def convert_unit(value, unit, to, floor: bool = True):
    """Convert a value between different units.

    Convert `value`, is assumed to be in units of `unit`, to units of `to`.
    If `floor` is true, then use floor division on `value` if necessary.

    Parameters
    ----------
    value
        Number or numeric ibis expression
    unit
        Unit of `value`
    to
        Unit to convert to
    floor
        Whether or not to use floor division on `value` if necessary.

    Returns
    -------
    Union[numbers.Integral, ibis.expr.types.NumericValue]
        Integer converted unit

    Examples
    --------
    >>> one_second = 1000
    >>> x = convert_unit(one_second, "ms", "s")
    >>> x
    1
    >>> one_second = 1
    >>> x = convert_unit(one_second, "s", "ms")
    >>> x
    1000
    >>> x = convert_unit(one_second, "s", "s")
    >>> x
    1
    >>> x = convert_unit(one_second, "s", "M")
    Traceback (most recent call last):
        ...
    ValueError: Cannot convert to or from unit ... to unit ...

    """
    # Don't do anything if from and to units are equivalent
    if unit == to:
        return value

    units = ("W", "D", "h", "m", "s", "ms", "us", "ns")
    factors = (7, 24, 60, 60, 1000, 1000, 1000)

    monthly_units = ("Y", "Q", "M")
    monthly_factors = (4, 3)

    try:
        i, j = units.index(unit), units.index(to)
    except ValueError:
        try:
            i, j = monthly_units.index(unit), monthly_units.index(to)
            factors = monthly_factors
        except ValueError:
            raise ValueError(
                f"Cannot convert interval value from unit {unit} to unit {to}"
            )

    factor = functools.reduce(operator.mul, factors[min(i, j) : max(i, j)], 1)
    assert factor > 1

    if i < j:
        op = operator.mul
    else:
        assert i > j
        op = operator.floordiv if floor else operator.truediv
    try:
        return op(value.to_expr(), factor).op()
    except AttributeError:
        return op(value, factor)


# taken from the itertools documentation
def consume(iterator: Iterator[T], n: int | None = None) -> None:
    """Advance `iterator` n-steps ahead. If `n` is `None`, consume entirely."""
    # Use functions that consume iterators at C speed.
    if n is None:
        # feed the entire iterator into a zero-length deque
        collections.deque(iterator, maxlen=0)
    else:
        # advance to the empty slice starting at position n
        next(itertools.islice(iterator, n, n), None)


def flatten_iterable(iterable):
    """Recursively flatten the iterable `iterable`."""
    if not is_iterable(iterable):
        raise TypeError("flatten is only defined for non-str iterables")

    for item in iterable:
        if is_iterable(item):
            yield from flatten_iterable(item)
        else:
            yield item


def deprecated_msg(name, *, instead, as_of="", removed_in=""):
    msg = f"`{name}` is deprecated"

    msgs = []

    if as_of:
        msgs.append(f"as of v{as_of}")

    if removed_in:
        msgs.append(f"removed in v{removed_in}")

    if msgs:
        msg += f" {', '.join(msgs)}"
    msg += f"; {instead}"
    return msg


def warn_deprecated(name, *, instead, as_of="", removed_in="", stacklevel=1):
    """Warn about deprecated usage.

    The message includes a stacktrace and what to do instead.
    """

    msg = deprecated_msg(name, instead=instead, as_of=as_of, removed_in=removed_in)
    warnings.warn(msg, FutureWarning, stacklevel=stacklevel + 1)


def append_admonition(
    func: Callable, *, msg: str, body: str = "", kind: str = "warning"
) -> str:
    """Append a `kind` admonition with `msg` to `func`'s docstring."""
    if docstr := func.__doc__:
        preamble, *rest = docstr.split("\n\n", maxsplit=1)

        # count leading spaces and add them to the deprecation warning so the
        # docstring parses correctly
        leading_spaces = " " * sum(
            1 for _ in itertools.takewhile(str.isspace, rest[0] if rest else [])
        )

        lines = [f"::: {{.callout-{kind}}}", f"## {msg}", ":::"]
        admonition_doc = textwrap.indent("\n".join(lines), leading_spaces)

        if body:
            rest = [indent(body, spaces=len(leading_spaces) + 4), *rest]

        docstr = "\n\n".join([preamble, admonition_doc, *rest])
    else:
        lines = [f"::: {{.callout-{kind}}}", f"## {msg}", ":::"]
        admonition_doc = "\n".join(lines)
        if body:
            admonition_doc += f"\n\n{indent(body, spaces=4)}"
        docstr = admonition_doc
    return docstr


def deprecated(*, instead: str, as_of: str = "", removed_in: str = ""):
    """Decorate to warn of deprecated usage and what to do instead."""

    def decorator(func):
        msg = deprecated_msg(
            func.__qualname__, instead=instead, as_of=as_of, removed_in=removed_in
        )

        func.__doc__ = append_admonition(func, msg=f"DEPRECATED: {msg}")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            warn_deprecated(
                func.__qualname__,
                instead=instead,
                as_of=as_of,
                removed_in=removed_in,
                stacklevel=2,
            )
            return func(*args, **kwargs)

        return wrapper

    return decorator


def backend_sensitive(
    *,
    msg: str = "This operation differs between backends.",
    why: str = "",
):
    """Indicate that an API may be sensitive to a backend."""

    def wrapper(func):
        func.__doc__ = append_admonition(func, msg=msg, body=why, kind="info")
        return func

    return wrapper


def experimental(func):
    """Decorate a callable to add warning about API instability in docstring."""

    func.__doc__ = append_admonition(
        func, msg="This API is experimental and subject to change."
    )
    return func


@functools.cache
def backend_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Get the list of installed `ibis.backend` entrypoints."""

    if sys.version_info < (3, 10):
        eps = importlib.metadata.entry_points()["ibis.backends"]
    else:
        eps = importlib.metadata.entry_points(group="ibis.backends")
    return sorted(eps)


_common_package_aliases = {
    "pa": "pyarrow",
    "pd": "pandas",
    "np": "numpy",
    "sk": "sklearn",
    "sp": "scipy",
    "tf": "tensorflow",
}


def unalias_package(name: str) -> str:
    return _common_package_aliases.get(name, name)


def import_object(qualname: str) -> Any:
    """Attempt to import an object given its full qualname.

    Examples
    --------
    >>> ex = import_object("ibis.examples")

    Is the same as

    >>> from ibis import examples as ex

    """
    mod_name, name = qualname.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    try:
        return getattr(mod, name)
    except AttributeError:
        raise ImportError(f"cannot import name {name!r} from {mod_name!r}") from None


def normalize_filename(source: str | Path) -> str:
    source = str(source)
    for prefix in (
        "parquet",
        "csv",
        "csv.gz",
        "txt",
        "txt.gz",
        "tsv",
        "tsv.gz",
        "file",
    ):
        source = source.removeprefix(f"{prefix}://")

    def _absolufy_paths(name):
        if re.search(r"^(?:.+)://", name) is None:
            return os.path.abspath(name)
        return name

    source = _absolufy_paths(source)
    return source


def normalize_filenames(source_list):
    # Promote to list
    source_list = promote_list(source_list)

    return list(map(normalize_filename, source_list))


def gen_name(namespace: str) -> str:
    """Create a unique identifier."""
    uid = base64.b32encode(uuid.uuid4().bytes).decode().rstrip("=").lower()
    return f"ibis_{namespace}_{uid}"


def slice_to_limit_offset(
    what: slice, count: ir.IntegerScalar
) -> tuple[int | ir.IntegerScalar, int | ir.IntegerScalar]:
    """Convert a Python [`slice`](slice) to a `limit`, `offset` pair.

    Parameters
    ----------
    what
        The slice to convert
    count
        The total number of rows in the table as an expression

    Returns
    -------
    tuple[int | ir.IntegerScalar, int | ir.IntegerScalar]
        The offset and limit to use in a `Table.limit` call

    Examples
    --------
    >>> import ibis
    >>> t = ibis.table(dict(a="int", b="string"), name="t")

    First 10 rows
    >>> count = t.count()
    >>> what = slice(0, 10)
    >>> limit, offset = slice_to_limit_offset(what, count)
    >>> limit
    10
    >>> offset
    0

    Last 10 rows
    >>> what = slice(-10, None)
    >>> limit, offset = slice_to_limit_offset(what, count)
    >>> limit
    10
    >>> offset
    r0 := UnboundTable: t
      a int64
      b string
    <BLANKLINE>
    Add(CountStar(t), -10): CountStar(r0) + -10

    From 5th row to 10th row
    >>> what = slice(5, 10)
    >>> limit, offset = slice_to_limit_offset(what, count)
    >>> limit, offset
    (5, 5)

    """
    if (step := what.step) is not None and step != 1:
        raise ValueError("Slice step can only be 1")

    import ibis

    start = what.start
    stop = what.stop

    if start is None or start >= 0:
        offset = start or 0

        if stop is None:
            limit = None
        elif stop == 0:
            limit = 0
        elif stop < 0:
            limit = count + (stop - offset)
        else:  # stop > 0
            limit = max(stop - offset, 0)
    else:  # start < 0
        offset = count + start

        if stop is None:
            limit = -start
        elif stop == 0:
            limit = offset = 0
        elif stop < 0:
            limit = max(stop - start, 0)
            if limit == 0:
                offset = 0
        else:  # stop > 0
            limit = ibis.greatest((stop - start) - count, 0)
    return limit, offset


class Namespace:
    """Convenience class for creating patterns for various types from a module.

    Useful to reduce boilerplate when creating patterns for various types from
    a module.

    Parameters
    ----------
    factory
        The pattern to construct with the looked up types.
    module
        The module object or name to look up the types.

    """

    __slots__ = ("_factory", "_module")
    _factory: Callable
    _module: ModuleType

    def __init__(self, factory, module):
        if isinstance(module, str):
            module = sys.modules[module]
        self._module = module
        self._factory = factory

    def __getattr__(self, name: str):
        obj = getattr(self._module, name)
        return self._factory(obj)


class PseudoHashable(Coercible, Generic[V]):
    """A wrapper that provides a best effort precomputed hash."""

    __slots__ = ("hash", "obj")
    obj: V

    def __init__(self, obj: V):
        if isinstance(obj, collections.abc.Hashable):
            raise TypeError(f"Cannot wrap a hashable object: {obj!r}")
        elif isinstance(obj, collections.abc.Sequence):
            hashable_obj = tuple(obj)
        elif isinstance(obj, collections.abc.Mapping):
            hashable_obj = tuple(obj.items())
        elif isinstance(obj, collections.abc.Set):
            hashable_obj = frozenset(obj)
        else:
            hashable_obj = id(obj)

        self.obj = obj
        self.hash = hash((type(obj), hashable_obj))

    @classmethod
    def __coerce__(cls, value: V) -> PseudoHashable[V]:
        if isinstance(value, cls):
            return value
        return cls(value)

    def __hash__(self):
        return self.hash

    def __eq__(self, other):
        if isinstance(other, PseudoHashable):
            return self.obj == other.obj
        else:
            return NotImplemented

    def __ne__(self, other):
        if isinstance(other, PseudoHashable):
            return self.obj != other.obj
        else:
            return NotImplemented


def chunks(n: int, *, chunk_size: int) -> Iterator[tuple[int, int]]:
    """Return an iterator of chunk start and end indices.

    Parameters
    ----------
    n
        The total number of elements.
    chunk_size
        The size of each chunk.

    Returns
    -------
    int
        THE start and end indices of each chunk.

    Examples
    --------
    >>> list(chunks(10, chunk_size=3))
    [(0, 3), (3, 6), (6, 9), (9, 10)]
    >>> list(chunks(10, chunk_size=4))
    [(0, 4), (4, 8), (8, 10)]
    """
    return ((start, min(start + chunk_size, n)) for start in range(0, n, chunk_size))


def get_subclasses(obj: type[T]) -> Iterator[type[S]]:
    """Recursively compute all subclasses of `obj`.

    ::: {.callout-note}
    ## The resulting iterator does **not** include the input type object.
    :::

    Parameters
    ----------
    obj
        Any type object

    Examples
    --------
    >>> class Base: ...
    >>> class Subclass1(Base): ...
    >>> class Subclass2(Base): ...
    >>> class TransitiveSubclass(Subclass2): ...

    Everything inherits `Base` (directly or transitively)

    >>> list(get_subclasses(Base))
    [<class 'ibis.util.Subclass1'>, <class 'ibis.util.Subclass2'>, <class 'ibis.util.TransitiveSubclass'>]

    Nothing inherits from `Subclass1`

    >>> list(get_subclasses(Subclass1))
    []

    Only `TransitiveSubclass` inherits from `Subclass2`

    >>> list(get_subclasses(Subclass2))
    [<class 'ibis.util.TransitiveSubclass'>]

    Nothing inherits from `TransitiveSubclass`

    >>> list(get_subclasses(TransitiveSubclass))
    []
    """
    for child_class in obj.__subclasses__():
        yield child_class
        yield from get_subclasses(child_class)


if sys.version_info[:2] < (3, 10):

    @contextlib.contextmanager
    def mktempd():
        tmpdir = tempfile.TemporaryDirectory()
        try:
            yield tmpdir.name
        finally:
            with contextlib.suppress(Exception):
                tmpdir.cleanup()
else:

    def mktempd():
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def version(package: str) -> Version:
    try:
        version_info = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None
    else:
        return vparse(version_info)
