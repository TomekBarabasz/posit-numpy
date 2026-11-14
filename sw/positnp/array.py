"""PositArray: posit arrays with the NumPy look and feel (plan, stage 3).

    import numpy as np, positnp as pn
    x = pn.array(np.linspace(-2, 2, 5))          # posit<32,2> by default; pn.array(..., pn.P16)
    y = x * x - x / 3 + 1                        # every arithmetic operator is one array_op on the backend
    np.sort(y), np.max(y), np.argmax(y), -y, abs(y), y < 0
    y.to_float()                                 # float64; NaR -> nan
    with pn.using(PositVecHw.open(32)):            # the KV260 for this block (default: PositSw, vecops)
        z = x * x
    pn.stats()                                   # NaR / saturation / inexact counts of the backend

Design (plan, stage 3 options):
- an own class, not an ndarray subclass: integer arithmetic on posit bits is wrong, so nothing
  falls through to NumPy silently; unsupported NumPy functions raise TypeError;
- level 0 (neg, abs, comparisons, sort, min, max, argmin, argmax) works on the bits as signed
  integers, because posits order like two's complement integers;
- NaR follows the posit standard: equal to itself and below every real number (so it sorts first,
  is the minimum and never the maximum), unlike NumPy's NaN;
- arithmetic runs at once (no lazy graph); arrays of any shape go to the backend flattened,
  broadcasting is done here; array (+) scalar uses the scalar mode of the backend.
"""

from __future__ import annotations

import contextlib
import math
from fractions import Fraction
from typing import Any, Dict, Optional

import numpy as np

from . import ref, vecops
from .backend_sw import PositSw, bits_dtype
from .ref import PositConfig

P8 = PositConfig(8)
P16 = PositConfig(16)
P32 = PositConfig(32)

# ---- backends ---------------------------------------------------------------------------------

_backends: Dict[PositConfig, Any] = {}       # format -> backend (PositVecHw); the rest: PositSw
_sw: Dict[PositConfig, PositSw] = {}


def set_backend(backend) -> None:
    """Backend for arrays of its format.

        pn.set_backend(hw32)               # posit32 arrays on the KV260, the other formats unchanged
        pn.set_backend([hw32, hw16, hw8])  # several engines at once (pn.board.load does this)
        pn.set_backend(None)               # everything in software (PositSw)
    """
    if backend is None:
        _backends.clear()
        return
    for b in (backend.values() if isinstance(backend, dict) else
              backend if isinstance(backend, (list, tuple)) else [backend]):
        _backends[b.config] = b


def active_backends() -> Dict[PositConfig, Any]:
    """Format -> backend for the formats not computed by PositSw."""
    return dict(_backends)


@contextlib.contextmanager
def using(backend):
    """with pn.using(hw): ... - backend(s) for the block (None: all software), the previous set restored after."""
    prev = dict(_backends)
    if backend is None:
        _backends.clear()
    else:
        set_backend(backend)
    try:
        yield backend
    finally:
        _backends.clear()
        _backends.update(prev)


def get_backend(config: PositConfig):
    """The backend registered for `config`, otherwise the PositSw for `config`."""
    be = _backends.get(config)
    if be is not None:
        return be
    if config not in _sw:
        _sw[config] = PositSw(config)
    return _sw[config]


def _stats_backend(config: Optional[PositConfig]):
    if config is None:
        if len(_backends) == 1:
            return next(iter(_backends.values()))
        return get_backend(P32)
    return get_backend(config)


def stats(config: Optional[PositConfig] = None) -> Dict[str, int]:
    """Counters since the last clear: nar, sat (clamped to maxpos/minpos), inexact, elements."""
    return _stats_backend(config).stats()


def clear_stats(config: Optional[PositConfig] = None) -> None:
    _stats_backend(config).clear_stats()


# ---- construction -------------------------------------------------------------------------------

def array(x, config: Optional[PositConfig] = None) -> "PositArray":
    """Posit array from numbers (rounded to nearest, ties to even; nan, inf -> NaR) or a PositArray.

    A float32 array going to posit32 is converted by the backend (F2P in the PL on the KV260)."""
    if isinstance(x, PositArray):
        if config is None or config == x.config:
            return x.copy()
        return array(x.to_float(), config)
    c = config or P32
    if isinstance(x, np.ndarray) and x.dtype == np.float32 and c.n == 32 and c.es == 2 and x.size:
        be = get_backend(c)
        if "f2p" in getattr(be, "array_ops", ()):
            return PositArray(be.array_op("f2p", x.view(np.uint32), scalar=0), c)
    return PositArray(vecops.from_float(c, np.asarray(x, dtype=np.float64)).bits, c)


def fma(a, b, c) -> "PositArray":
    """a * b + c with one rounding, broadcast like NumPy.

    On the KV260: c a scalar -> OP_FMA (c in OPERAND_C); b a scalar and c an array -> OP_AXPY;
    three arrays -> PositSw (needs a third stream, stage 6; counted in stats()["fallback"])."""
    first = next((x for x in (a, b, c) if isinstance(x, PositArray)), None)
    cfg = first.config if first is not None else P32
    A, B, C = (x if isinstance(x, PositArray) else array(x, cfg) for x in (a, b, c))
    if len({A.config, B.config, C.config}) != 1:
        raise TypeError("fma: posit formats differ")
    shape = np.broadcast_shapes(A.shape, B.shape, C.shape)
    be = get_backend(cfg)
    scalar = lambda x: x.size == 1 and x.ndim <= 1                                  # noqa: E731
    full = lambda x: np.ascontiguousarray(np.broadcast_to(x.bits, shape)).ravel()   # noqa: E731
    if scalar(C):
        if scalar(B):
            r = be.array_op("fma", full(A), scalar=int(B.bits.ravel()[0]), c=int(C.bits.ravel()[0]))
        else:
            r = be.array_op("fma", full(A), full(B), c=int(C.bits.ravel()[0]))
    elif scalar(B):
        r = be.array_op("fma", full(A), scalar=int(B.bits.ravel()[0]), c=full(C))
    elif scalar(A):
        r = be.array_op("fma", full(B), scalar=int(A.bits.ravel()[0]), c=full(C))     # a * b = b * a
    else:
        r = be.array_op("fma", full(A), full(B), c=full(C))
    return PositArray(np.asarray(r).reshape(shape), cfg)


def axpy(alpha, x, y) -> "PositArray":
    """alpha * x + y with one rounding (BLAS axpy); one stream pass on the KV260."""
    return fma(x, alpha, y)


def from_bits(bits, config: PositConfig = P32) -> "PositArray":
    """Posit array from raw posit bits (any integer array)."""
    return PositArray(bits, config)


def nar(shape=(), config: PositConfig = P32) -> "PositArray":
    return PositArray(np.full(shape, config.nar), config)


def zeros(shape, config: PositConfig = P32) -> "PositArray":
    return PositArray(np.zeros(shape, np.int64), config)


# ---- the class ----------------------------------------------------------------------------------

_LATER = {
    "prod": "no exact posit product reduction planned", "cumsum": "not planned (one quire per prefix)",
    "matmul": "stage 7", "exp": "stage 7",
    "log": "stage 7", "sin": "stage 7", "cos": "stage 7", "tanh": "stage 7",
}


# ---- quire reductions (stage 4) -----------------------------------------------------------------
#
# sum, dot and sumsq go to the backend's quire (PositVecHw: the FPGA; PositSw: vecops) and come
# back rounded once, plus the exact quire. mean and norm are rounded once here, from the exact
# quire: q / N through the reference division, sqrt(q) through an integer square root with a
# sticky bit - so they are correctly rounded too (no double rounding through the backend).

def _quire(x: "PositArray", mode: str, y: Optional["PositArray"] = None):
    be = get_backend(x.config)
    return be.reduce(mode, x.bits.ravel(), None if y is None else y.bits.ravel())


def quire(x, y=None, mode: Optional[str] = None):
    """The exact quire of sum(x) (or dot(x, y), or mode="sumsq") as a Fraction; nan for NaR."""
    x = x if isinstance(x, PositArray) else array(x)
    if y is not None:
        y = x._coerce(y)
    mode = mode or ("sum" if y is None else "dot")
    _, _, q, nar = _quire(x, mode, y)
    return float("nan") if nar else Fraction(q, 1 << x.config.quire_frac)


def _mean_bits(c: PositConfig, q: int, nar: bool, count: int) -> int:
    if nar or count == 0:
        return c.nar
    if q == 0:
        return 0
    quot, inexact = ref._div((q, -c.quire_frac), (count, 0))
    return ref.round_(c, quot, inexact)[0]


def _sqrt_bits(c: PositConfig, q: int, nar: bool) -> int:
    if nar or q < 0:
        return c.nar
    if q == 0:
        return 0
    extra = 64 + c.n                       # quire_frac is even: sqrt(q * 2**-F) = isqrt(q << 2e) * 2**(-F/2 - e)
    s = math.isqrt(q << (2 * extra))
    inexact = s * s != q << (2 * extra)
    return ref.round_(c, (s, -(c.quire_frac // 2) - extra), inexact)[0]


class PositArray:
    __array_priority__ = 1000
    __hash__ = None

    def __init__(self, bits, config: PositConfig = P32):
        self.config = config
        # astype copies, so the result is C-contiguous and keeps its shape (0-d too, unlike ascontiguousarray)
        self.bits = (np.asarray(bits).astype(np.int64) & config.mask).astype(bits_dtype(config))

    # ---- conversion and container ----------------------------------------------------------

    def to_float(self) -> np.ndarray:
        return vecops.to_float(self.config, self.bits)

    def to_float32(self) -> np.ndarray:
        """float32, round to nearest even (P2F in the PL for posit32 on the KV260); NaR -> nan."""
        be = get_backend(self.config)
        if self.config.n == 32 and self.config.es == 2 and self.size and "p2f" in getattr(be, "array_ops", ()):
            bits = np.asarray(be.array_op("p2f", self.bits.ravel(), scalar=0)).astype(np.uint32)
            return bits.view(np.float32).reshape(self.shape)
        return self.to_float().astype(np.float32)

    def astype(self, dtype) -> np.ndarray:
        dt = np.dtype(dtype)
        if dt == np.float32:
            return self.to_float32()
        if dt == np.float64:
            return self.to_float()
        raise TypeError(f"PositArray.astype({dt}): float32 or float64 (bits: .bits)")

    def sqrt(self) -> "PositArray":
        """Correctly rounded square root; negative -> NaR."""
        r = get_backend(self.config).array_op("sqrt", self.bits.ravel(), scalar=0)
        return PositArray(np.asarray(r).reshape(self.shape), self.config)

    def __float__(self) -> float:
        if self.size != 1:
            raise TypeError("only one-element posit arrays convert to float")
        return float(self.to_float().reshape(()))

    @property
    def shape(self):
        return self.bits.shape

    @property
    def ndim(self) -> int:
        return self.bits.ndim

    @property
    def size(self) -> int:
        return self.bits.size

    def __len__(self) -> int:
        if self.ndim == 0:
            raise TypeError("len() of a 0-d posit array")
        return len(self.bits)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __getitem__(self, idx) -> "PositArray":
        return PositArray(self.bits[idx], self.config)

    def __setitem__(self, idx, value) -> None:
        self.bits[idx] = self._coerce(value).bits

    def copy(self) -> "PositArray":
        return PositArray(self.bits.copy(), self.config)

    def reshape(self, *shape) -> "PositArray":
        return PositArray(self.bits.reshape(*shape), self.config)

    def ravel(self) -> "PositArray":
        return PositArray(self.bits.ravel(), self.config)

    flatten = ravel

    @property
    def T(self) -> "PositArray":
        return PositArray(self.bits.T, self.config)

    def equal_bits(self, other: "PositArray") -> bool:
        """Same format, shape and bits (Python truth value; == is element-wise, as in NumPy)."""
        return isinstance(other, PositArray) and other.config == self.config and np.array_equal(other.bits, self.bits)

    def __repr__(self) -> str:
        f = self.to_float()
        body = np.array2string(f, separator=", ", threshold=12, edgeitems=4,
                               formatter={"float_kind": lambda v: "NaR" if np.isnan(v) else f"{v:.8g}"})
        return f"PositArray({body}, {self.config})"

    # ---- operands ---------------------------------------------------------------------------

    def _coerce(self, other) -> "PositArray":
        if isinstance(other, PositArray):
            if other.config != self.config:
                raise TypeError(f"{self.config} and {other.config}: convert with pn.array(x, config) first")
            return other
        if isinstance(other, np.ndarray) and other.dtype == bool:
            raise TypeError("boolean arrays are not posits")
        return array(other, self.config)

    def _binary(self, name: str, other) -> "PositArray":
        o = self._coerce(other)
        be = get_backend(self.config)
        if o.size == 1 and o.shape in ((), (1,)) and self.size >= 1:
            r = be.array_op(name, self.bits.ravel(), scalar=int(o.bits.ravel()[0]))
            shape = np.broadcast_shapes(self.shape, o.shape)
            return PositArray(r.reshape(shape), self.config)
        a, b = np.broadcast_arrays(self.bits, o.bits)
        r = be.array_op(name, np.ascontiguousarray(a).ravel(), np.ascontiguousarray(b).ravel())
        return PositArray(r.reshape(a.shape), self.config)

    def _rbinary(self, name: str, other) -> "PositArray":
        return self._coerce(other)._binary(name, self)

    # ---- arithmetic: the backend ------------------------------------------------------------

    def __add__(self, other):
        return self._binary("add", other)

    def __sub__(self, other):
        return self._binary("sub", other)

    def __mul__(self, other):
        return self._binary("mul", other)

    def __truediv__(self, other):
        return self._binary("div", other)

    def __radd__(self, other):
        return self._rbinary("add", other)

    def __rsub__(self, other):
        return self._rbinary("sub", other)

    def __rmul__(self, other):
        return self._rbinary("mul", other)

    def __rtruediv__(self, other):
        return self._rbinary("div", other)

    add, sub, mul, div = __add__, __sub__, __mul__, __truediv__

    # ---- level 0: on the bits, exact ---------------------------------------------------------

    def _signed(self) -> np.ndarray:
        return vecops.to_signed(self.config, self.bits)

    def __neg__(self):
        return PositArray(vecops.neg(self.config, self.bits), self.config)

    def __pos__(self):
        return self.copy()

    def __abs__(self):
        return PositArray(vecops.absolute(self.config, self.bits), self.config)

    def _cmp(self, other, op) -> np.ndarray:
        o = self._coerce(other)
        return op(self._signed(), o._signed())

    def __lt__(self, other):
        return self._cmp(other, np.less)

    def __le__(self, other):
        return self._cmp(other, np.less_equal)

    def __gt__(self, other):
        return self._cmp(other, np.greater)

    def __ge__(self, other):
        return self._cmp(other, np.greater_equal)

    def __eq__(self, other):
        return self._cmp(other, np.equal)

    def __ne__(self, other):
        return self._cmp(other, np.not_equal)

    def isnar(self) -> np.ndarray:
        return self.bits == self.config.nar

    def _from_signed(self, s) -> "PositArray":
        return PositArray(np.asarray(s) & self.config.mask, self.config)

    def sort(self, axis=-1) -> None:
        """In place, NaR first."""
        self.bits[...] = self._from_signed(np.sort(self._signed(), axis=axis)).bits

    def max(self, axis=None):
        return self._from_signed(np.max(self._signed(), axis=axis))

    def min(self, axis=None):
        return self._from_signed(np.min(self._signed(), axis=axis))

    def argmax(self, axis=None):
        return np.argmax(self._signed(), axis=axis)

    def argmin(self, axis=None):
        return np.argmin(self._signed(), axis=axis)

    # ---- quire reductions (stage 4) -----------------------------------------------------------

    def _along(self, axis, fn) -> "PositArray":
        """fn(1-D PositArray) -> bits, for axis None (all elements) or one axis."""
        if axis is None:
            return PositArray(fn(self.ravel()), self.config)
        moved = np.moveaxis(self.bits, axis, -1)
        flat = moved.reshape(-1, moved.shape[-1])
        out = [fn(PositArray(row, self.config)) for row in flat]
        return PositArray(np.array(out, dtype=np.int64).reshape(moved.shape[:-1]), self.config)

    def sum(self, axis=None) -> "PositArray":
        """Exact sum in the quire, rounded once."""
        return self._along(axis, lambda v: _quire(v, "sum")[0])

    def mean(self, axis=None) -> "PositArray":
        """Exact sum / N, rounded once."""
        def one(v):
            _, _, q, nar = _quire(v, "sum")
            return _mean_bits(self.config, q, nar, v.size)
        return self._along(axis, one)

    def dot(self, other) -> "PositArray":
        """1-D . 1-D: exact sum of products in the quire, rounded once."""
        o = self._coerce(other)
        if self.ndim != 1 or o.ndim != 1:
            raise TypeError(f"dot of shapes {self.shape} and {o.shape}: only vectors (matrices: stage 7)")
        if o.size != self.size:
            raise ValueError(f"dot of {self.size} and {o.size} elements")
        return PositArray(_quire(self, "dot", o)[0], self.config)

    def norm(self) -> "PositArray":
        """Euclidean norm of all elements: exact sum of squares, square root rounded once."""
        _, _, q, nar = _quire(self.ravel(), "sumsq")
        return PositArray(_sqrt_bits(self.config, q, nar), self.config)

    # ---- NumPy protocols ----------------------------------------------------------------------

    _UFUNCS = {
        np.add: "add", np.subtract: "sub", np.multiply: "mul", np.true_divide: "div",
        np.negative: "neg", np.positive: "pos", np.absolute: "abs", np.square: "square",
        np.less: "lt", np.less_equal: "le", np.greater: "gt", np.greater_equal: "ge",
        np.equal: "eq", np.not_equal: "ne", np.maximum: "maximum", np.minimum: "minimum",
        np.isnan: "isnar", np.sqrt: "sqrt",
    }

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        if method != "__call__" or kwargs.get("out") is not None or set(kwargs) - {"out"}:
            return NotImplemented
        what = self._UFUNCS.get(ufunc)
        if what is None:
            if ufunc.__name__ in _LATER:
                raise TypeError(f"np.{ufunc.__name__} on posit arrays: {_LATER[ufunc.__name__]}")
            return NotImplemented
        first = next(x for x in inputs if isinstance(x, PositArray))
        args = [first._coerce(x) for x in inputs]
        a = args[0]
        if what in ("add", "sub", "mul", "div"):
            return a._binary(what, args[1])
        if what == "square":
            return a._binary("mul", a)
        if what == "neg":
            return -a
        if what == "pos":
            return a.copy()
        if what == "abs":
            return abs(a)
        if what == "isnar":
            return a.isnar()
        if what == "sqrt":
            return a.sqrt()
        if what in ("maximum", "minimum"):
            fn = np.maximum if what == "maximum" else np.minimum
            return a._from_signed(fn(a._signed(), args[1]._signed()))
        return {"lt": a.__lt__, "le": a.__le__, "gt": a.__gt__, "ge": a.__ge__,
                "eq": a.__eq__, "ne": a.__ne__}[what](args[1])

    def __array_function__(self, func, types, args, kwargs):
        impl = _FUNCTIONS.get(func)
        if impl is None:
            if func.__name__ in _LATER:
                raise TypeError(f"np.{func.__name__} on posit arrays: {_LATER[func.__name__]}")
            return NotImplemented
        if not all(issubclass(t, (PositArray, np.ndarray)) for t in types):
            return NotImplemented
        return impl(*args, **kwargs)


def _config_of(seq) -> PositConfig:
    configs = {x.config for x in seq if isinstance(x, PositArray)}
    if len(configs) != 1:
        raise TypeError(f"mixed posit formats: {sorted(map(str, configs))}")
    return configs.pop()


def _sort(a, axis=-1, **kw):
    r = a.copy()
    r.sort(axis=axis)
    return r


def _concatenate(arrays, axis=0, **kw):
    c = _config_of(arrays)
    return PositArray(np.concatenate([array(x, c).bits if not isinstance(x, PositArray) else x.bits for x in arrays],
                                     axis=axis), c)


def _stack(arrays, axis=0, **kw):
    c = _config_of(arrays)
    return PositArray(np.stack([x.bits for x in arrays], axis=axis), c)


def _where(cond, x, y):
    c = _config_of([x, y])
    xb = x.bits if isinstance(x, PositArray) else array(x, c).bits
    yb = y.bits if isinstance(y, PositArray) else array(y, c).bits
    return PositArray(np.where(np.asarray(cond), xb, yb), c)


_FUNCTIONS: Dict[Any, Any] = {
    np.sort: _sort,
    np.argsort: lambda a, axis=-1, **kw: np.argsort(a._signed(), axis=axis, kind=kw.get("kind")),
    np.max: lambda a, axis=None, **kw: a.max(axis),
    np.min: lambda a, axis=None, **kw: a.min(axis),
    np.argmax: lambda a, axis=None, **kw: a.argmax(axis),
    np.argmin: lambda a, axis=None, **kw: a.argmin(axis),
    np.concatenate: _concatenate,
    np.stack: _stack,
    np.where: _where,
    np.reshape: lambda a, shape, **kw: a.reshape(shape),
    np.ravel: lambda a, **kw: a.ravel(),
    np.shape: lambda a: a.shape,
    np.ndim: lambda a: a.ndim,
    np.size: lambda a, axis=None: a.size if axis is None else a.shape[axis],
    np.copy: lambda a, **kw: a.copy(),
    np.transpose: lambda a, axes=None: PositArray(np.transpose(a.bits, axes), a.config),
    np.array_equal: lambda a, b, **kw: isinstance(a, PositArray) and a.equal_bits(b),
    np.isnan: lambda a: a.isnar(),
    np.sum: lambda a, axis=None, **kw: a.sum(axis),
    np.mean: lambda a, axis=None, **kw: a.mean(axis),
    np.dot: lambda a, b, **kw: (a if isinstance(a, PositArray) else b._coerce(a)).dot(b),
    np.inner: lambda a, b: (a if isinstance(a, PositArray) else b._coerce(a)).dot(b),
    np.vdot: lambda a, b: (a if isinstance(a, PositArray) else b._coerce(a)).dot(b),
    np.linalg.norm: lambda x, ord=None, axis=None, keepdims=False: _norm(x, ord, axis),
}


def _norm(x, ord, axis):
    if ord is not None or axis is not None:
        raise TypeError("np.linalg.norm on posit arrays: only the Euclidean norm of all elements")
    return x.norm()
for _alias in ("amax", "amin"):
    if hasattr(np, _alias) and getattr(np, _alias) not in _FUNCTIONS:
        _FUNCTIONS[getattr(np, _alias)] = _FUNCTIONS[np.max if _alias == "amax" else np.min]
