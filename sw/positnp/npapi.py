"""NumPy functions and ufuncs on PositArray beyond the core of positnp.array.

Registered in positnp.array (_FUNCTIONS for __array_function__, UFUNCS for __array_ufunc__) when the module is
imported. Rules, as in the rest of the library:

- shape functions (flip, roll, pad, stack, split, diag, triu, ...) work on the bits: the posit 0 has the bits 0,
  so the zeros NumPy inserts are posit zeros; values given to them (pad constant_values, insert, full_like) are
  rounded to the array's format;
- every value result is the exact value rounded once (median and percentiles from exact rationals, hypot,
  fmod, remainder, floor_divide from exact integers); arctan2 goes through float64 like the posit32 functions;
- an option NumPy has and the posit version does not raises TypeError: nothing is silently ignored.
"""

from __future__ import annotations

import importlib
import math
from fractions import Fraction
from typing import Optional

import numpy as np

from . import ref, vecops
from .ref import PositConfig

A = importlib.import_module(".array", __package__)
PositArray = A.PositArray
P32 = A.P32
_NV = object()                     # "argument not given"


def _no(name: str, **opts) -> None:
    """Raise for options that are set to something else than their default (None / False / True / _NV)."""
    bad = [k for k, v in opts.items() if not (v is None or v is _NV or v is False or (k == "where" and v is True))]
    if bad:
        raise TypeError(f"{name} on posit arrays: {', '.join(k + '=' for k in bad)} not supported")


def _posit(x, config: Optional[PositConfig] = None) -> PositArray:
    return x if isinstance(x, PositArray) else A.array(x, config)


def _dtype(res, dtype, name):
    if dtype is None:
        return res
    if isinstance(dtype, PositConfig):
        return res.astype(dtype)
    raise TypeError(f"{name}: dtype= must be a posit format (pn.P16, ...)")


# ---- reductions with NumPy's signatures (positional order matters: np.sum(a, 1, None, out)) ----------------

def _sum_like(method):
    def f(a, axis=None, dtype=None, out=None, keepdims=False, initial=_NV, where=True):
        _no(f"np.{method}", initial=initial, where=where)
        return A._to_out(_dtype(getattr(a, method)(axis=axis, keepdims=keepdims), dtype, method), out)
    return f


def _max_like(method):
    def f(a, axis=None, out=None, keepdims=False, initial=_NV, where=True):
        _no(f"np.{method}", initial=initial, where=where)
        return A._to_out(getattr(a, method)(axis=axis, keepdims=keepdims), out)
    return f


def _mean(a, axis=None, dtype=None, out=None, keepdims=False, *, where=True):
    _no("np.mean", where=where)
    return A._to_out(_dtype(a.mean(axis=axis, keepdims=keepdims), dtype, "mean"), out)


def _var_like(root):
    def f(a, axis=None, dtype=None, out=None, ddof=0, keepdims=False, *, where=True, mean=_NV, correction=_NV):
        _no("np.std" if root else "np.var", where=where, mean=mean)
        if correction is not _NV:
            ddof = correction
        r = a.std(axis, ddof, keepdims) if root else a.var(axis, ddof, keepdims)
        return A._to_out(_dtype(r, dtype, "var"), out)
    return f


def _arg_like(method):
    def f(a, axis=None, out=None, *, keepdims=False):
        return A._to_out(getattr(a, method)(axis=axis, keepdims=keepdims), out)
    return f


def _any_all(method):
    def f(a, axis=None, out=None, keepdims=False, *, where=True):
        _no(f"np.{method}", where=where)
        return A._to_out(getattr(a, method)(axis=axis, keepdims=keepdims), out)
    return f


def _cumsum(a, axis=None, dtype=None, out=None):
    return A._to_out(_dtype(a.cumsum(axis), dtype, "cumsum"), out)


def _sort(a, axis=-1, kind=None, order=None, *, stable=None):
    _no("np.sort", order=order)
    r = a.copy()
    r.sort(axis=axis)
    return r


def _argsort(a, axis=-1, kind=None, order=None, *, stable=None):
    _no("np.argsort", order=order)
    return np.argsort(a._signed(), axis=axis, kind="stable" if stable else kind)


def _reshape(a, shape=None, order="C", *, newshape=None, copy=None):
    return a.reshape(shape if shape is not None else newshape, order=order)


def _concatenate(arrays, axis=0, out=None, *, dtype=None, casting="same_kind"):
    xs = A._unify(*arrays)
    r = PositArray(np.concatenate([x.bits for x in xs], axis=axis), xs[0].config)
    return A._to_out(_dtype(r, dtype, "concatenate"), out)


def _stack(arrays, axis=0, out=None, *, dtype=None, casting="same_kind"):
    xs = A._unify(*arrays)
    r = PositArray(np.stack([x.bits for x in xs], axis=axis), xs[0].config)
    return A._to_out(_dtype(r, dtype, "stack"), out)


def _where(condition, x=_NV, y=_NV):
    if x is _NV and y is _NV:
        return np.nonzero(_truth(condition))
    xv, yv = A._unify(x, y)
    return PositArray(np.where(_truth(condition), xv.bits, yv.bits), xv.config)


def _dot(a, b, out=None):
    first = a if isinstance(a, PositArray) else b
    return A._to_out(first._coerce(a).dot(b) if first is b else a.dot(b), out)


def _truth(x):
    return x.bits != 0 if isinstance(x, PositArray) else np.asarray(x)


# ---- shape functions on the bits -------------------------------------------------------------------------

def _on_bits(f):
    def g(a, *args, **kw):
        return PositArray(f(a.bits, *args, **kw), a.config)
    return g


def _split(f):
    def g(a, *args, **kw):
        return [PositArray(p, a.config) for p in f(a.bits, *args, **kw)]
    return g


def _joined(f):
    def g(tup, *args, **kw):
        _no(f"np.{f.__name__}", dtype=kw.pop("dtype", None), casting=kw.pop("casting", None))
        xs = A._unify(*tup)
        return PositArray(f([x.bits for x in xs], *args, **kw), xs[0].config)
    return g


def _append(arr, values, axis=None):
    a, v = A._unify(arr, values)
    return PositArray(np.append(a.bits, v.bits, axis=axis), a.config)


def _insert(arr, obj, values, axis=None):
    a, v = A._unify(arr, values)
    return PositArray(np.insert(a.bits, obj, v.bits, axis=axis), a.config)


_PAD_BITWISE = ("constant", "edge", "reflect", "symmetric", "wrap", "empty")


def _pad(array, pad_width, mode="constant", **kw):
    if callable(mode) or mode not in _PAD_BITWISE:
        raise TypeError(f"np.pad on posit arrays: mode={mode!r} not supported (only {', '.join(_PAD_BITWISE)})")
    if kw.pop("reflect_type", "even") != "even":
        raise TypeError("np.pad on posit arrays: reflect_type='odd' is arithmetic, not supported")
    extra = {}
    if mode == "constant":                                      # the fill values rounded to the array's format
        cv = np.asarray(kw.pop("constant_values", 0), dtype=np.float64)
        extra["constant_values"] = vecops.from_float(array.config, cv).bits.astype(np.int64)
    if kw:
        raise TypeError(f"np.pad on posit arrays: {', '.join(kw)} not supported")
    return PositArray(np.pad(array.bits.astype(np.int64), pad_width, mode=mode, **extra), array.config)


def _like(fill):
    def f(a, *args, dtype=None, order="K", subok=True, shape=None, device=None):
        shp = a.shape if shape is None else shape
        value = fill if fill is not None else args[0]
        if dtype is not None and not isinstance(dtype, PositConfig):
            return np.full(shp, value, dtype=dtype)                 # a NumPy dtype: an ordinary array
        return full(shp, value, dtype or a.config)
    return f


def _broadcast_arrays(*args, subok=False):
    xs = A._unify(*args)
    return [PositArray(b, x.config) for x, b in zip(xs, np.broadcast_arrays(*[x.bits for x in xs]))]


# ---- selection on the order of the values (signed bits) ----------------------------------------------------

def _unique(ar, return_index=False, return_inverse=False, return_counts=False, axis=None, *, equal_nan=True,
            sorted=True):  # noqa: A002 (NumPy's name)
    res = np.unique(ar._signed(), return_index=return_index, return_inverse=return_inverse,
                    return_counts=return_counts, axis=axis)
    if not isinstance(res, tuple):
        return ar._from_signed(res)
    return (ar._from_signed(res[0]),) + tuple(res[1:])


def _searchsorted(a, v, side="left", sorter=None):
    av, vv = A._unify(a, v)
    return np.searchsorted(av._signed(), vv._signed(), side=side, sorter=sorter)


def _partition(a, kth, axis=-1, kind="introselect", order=None):
    _no("np.partition", order=order)
    return a._from_signed(np.partition(a._signed(), kth, axis=axis))


def _argpartition(a, kth, axis=-1, kind="introselect", order=None):
    _no("np.argpartition", order=order)
    return np.argpartition(a._signed(), kth, axis=axis)


def _count_nonzero(a, axis=None, *, keepdims=False):
    return np.count_nonzero(a.bits, axis=axis, keepdims=keepdims)


# ---- exact values ---------------------------------------------------------------------------------------

def _val(c: PositConfig, b) -> Optional[Fraction]:
    d = ref.decode(c, int(b))
    return None if d is None else Fraction(d[0]) * Fraction(2) ** d[1]


def _round_fractions(c: PositConfig, qs) -> np.ndarray:
    """Exact rationals (None: NaR) rounded once each."""
    ms, es, st, bad = [], [], [], []
    for q in qs:
        if q is None or q == 0:
            ms.append(0), es.append(0), st.append(False), bad.append(q is None)
            continue
        (m, e), inexact = ref._div((q.numerator, 0), (q.denominator, 0))
        ms.append(m), es.append(e), st.append(inexact), bad.append(False)
    return A._round_exact(c, ms, es, st, bad)


def sqrt_exact(m: int, e: int):
    """sqrt(m 2**e) for m > 0 as (s, e', sticky): s 2**e' truncated, sticky = not exact."""
    if e % 2:
        m, e = m << 1, e - 1
    extra = 64 + max(0, 64 - m.bit_length() // 2)
    t = m << (2 * extra)
    s = math.isqrt(t)
    return s, e // 2 - extra, s * s != t


def _pairwise(x, y, fn) -> PositArray:
    """Element-wise exact operation: fn(dyadic x, dyadic y) -> (m, e, sticky) or None (NaR); one rounding each."""
    xv, yv = A._unify(x, y)
    c = xv.config
    xb, yb = np.broadcast_arrays(xv.bits.astype(np.int64), yv.bits.astype(np.int64))
    if xb.size == 0:
        return PositArray(np.zeros(xb.shape, np.int64), c)
    u, inv = np.unique(np.stack([xb.ravel(), yb.ravel()], axis=1), axis=0, return_inverse=True)
    ms, es, st, bad = [], [], [], []
    for a, b in u.tolist():
        dx, dy = ref.decode(c, a), ref.decode(c, b)
        r = None if dx is None or dy is None else fn(dx, dy)
        if r is None:
            ms.append(0), es.append(0), st.append(False), bad.append(True)
        else:
            ms.append(r[0]), es.append(r[1]), st.append(r[2]), bad.append(False)
    bits = A._round_exact(c, ms, es, st, bad)
    return PositArray(bits[np.asarray(inv).ravel()].reshape(xb.shape), c)


def _hypot_d(dx, dy):
    m, e = ref._add(ref._mul(dx, dx), ref._mul(dy, dy))
    return (0, 0, False) if m == 0 else sqrt_exact(m, e)


def _common(dx, dy):
    e = min(dx[1], dy[1])
    return dx[0] << (dx[1] - e), dy[0] << (dy[1] - e), e


def _fmod_d(dx, dy):
    if dy[0] == 0:
        return None
    X, Y, e = _common(dx, dy)
    r = abs(X) % abs(Y)
    return (-r if X < 0 else r), e, False


def _remainder_d(dx, dy):
    if dy[0] == 0:
        return None
    X, Y, e = _common(dx, dy)
    return X % Y, e, False


def _floor_divide_d(dx, dy):
    if dy[0] == 0:
        return None
    X, Y, _ = _common(dx, dy)
    return X // Y, 0, False


def hypot(x, y) -> PositArray:
    """sqrt(x**2 + y**2), exact inside, rounded once."""
    return _pairwise(x, y, _hypot_d)


def arctan2(y, x) -> PositArray:
    """atan2(y, x) through float64 (exact inputs, one float64 rounding, then rounded to the posit - like the
    posit32 functions); NaR in, or (0, 0) (posit standard), gives NaR."""
    yv, xv = A._unify(y, x)
    c = yv.config
    fy, fx = np.broadcast_arrays(yv.to_float(), xv.to_float())
    with np.errstate(all="ignore"):
        r = np.arctan2(fy, fx)
    r = np.where((fy == 0) & (fx == 0), np.nan, r)
    return PositArray(vecops.from_float(c, r).bits, c)


def sign(x) -> PositArray:
    c = x.config
    s = x._signed()
    one = A._one_bits(c)
    r = np.where(s > 0, one, np.where(s < 0, (-one) & c.mask, 0))
    return PositArray(np.where(x.isnar(), c.nar, r), c)


def copysign(x, y) -> PositArray:
    xv, yv = A._unify(x, y)
    c = xv.config
    mag = vecops.absolute(c, xv.bits)
    neg = yv._signed() < 0
    r = np.where(neg, vecops.neg(c, mag), mag)
    return PositArray(np.where(xv.isnar() | yv.isnar(), c.nar, r), c)


def _fmax_fmin(fn):
    def f(x, y):
        xv, yv = A._unify(x, y)
        sx, sy = np.broadcast_arrays(xv._signed(), yv._signed())
        nx, ny = np.broadcast_arrays(xv.isnar(), yv.isnar())
        r = np.where(nx, sy, np.where(ny, sx, fn(sx, sy)))          # NaR ignored unless both are NaR
        return xv._from_signed(r)
    return f


UFUNCS = {
    np.sign: sign,
    np.signbit: lambda x: x._signed() < 0,
    np.isfinite: lambda x: ~x.isnar(),
    np.isinf: lambda x: np.zeros(x.shape, bool),
    np.arctan2: arctan2,
    np.hypot: hypot,
    np.fmod: lambda x, y: _pairwise(x, y, _fmod_d),
    np.remainder: lambda x, y: _pairwise(x, y, _remainder_d),
    np.floor_divide: lambda x, y: _pairwise(x, y, _floor_divide_d),
    np.copysign: copysign,
    np.fmax: _fmax_fmin(np.maximum),
    np.fmin: _fmax_fmin(np.minimum),
    np.logical_not: lambda x: ~_truth(x),
    np.logical_and: lambda x, y: _truth(x) & _truth(y),
    np.logical_or: lambda x, y: _truth(x) | _truth(y),
    np.logical_xor: lambda x, y: _truth(x) ^ _truth(y),
}


# ---- comparisons with a tolerance (on the exact values) ------------------------------------------------------

def _floats(x):
    if isinstance(x, PositArray):
        return x.to_float()
    if hasattr(x, "to_complex"):
        return x.to_complex()
    return np.asarray(x)


def _isclose(a, b, rtol=1e-05, atol=1e-08, equal_nan=False):
    return np.isclose(_floats(a), _floats(b), rtol=rtol, atol=atol, equal_nan=equal_nan)


def _allclose(a, b, rtol=1e-05, atol=1e-08, equal_nan=False):
    return bool(np.all(_isclose(a, b, rtol, atol, equal_nan)))


def _array_equiv(a1, a2):
    try:
        x, y = A._unify(a1, a2)
        return bool(np.all(np.equal(*np.broadcast_arrays(x.bits, y.bits))))
    except ValueError:
        return False


# ---- order statistics: median, percentile, quantile (exact, one rounding) -------------------------------------

_METHODS = ("linear", "lower", "higher", "nearest", "midpoint")


def _quantile_row(c: PositConfig, v: np.ndarray, qs, method: str):
    s = np.sort(vecops.to_signed(c, v))
    n = s.size
    if n == 0 or (n and s[0] == -(1 << (c.n - 1))):              # empty or NaR: NaR (like nan in NumPy)
        return [None] * len(qs)
    bits = s & c.mask
    out = []
    for q in qs:
        p = q * (n - 1)
        lo = math.floor(p)
        hi = min(lo + 1, n - 1)
        frac = p - lo
        a, b = _val(c, bits[lo]), _val(c, bits[hi])
        if method == "lower":
            out.append(a)
        elif method == "higher":
            out.append(a if frac == 0 else b)
        elif method == "nearest":
            k = lo if frac < Fraction(1, 2) or (frac == Fraction(1, 2) and lo % 2 == 0) else hi
            out.append(_val(c, bits[k]))
        elif method == "midpoint":
            out.append(a if frac == 0 else (a + b) / 2)
        else:
            out.append(a + frac * (b - a))
    return out


def _quantile_core(a, qs_frac, q_shape, axis, method, keepdims, name):
    if method not in _METHODS:
        raise TypeError(f"np.{name} on posit arrays: method={method!r} not supported (only {', '.join(_METHODS)})")
    c = a.config
    axes = a._axes(axis)
    keep = [i for i in range(a.ndim) if i not in axes]
    moved = np.transpose(a.bits, keep + list(axes))
    rest = moved.shape[:len(keep)]
    rows = moved.reshape(int(np.prod(rest, dtype=np.int64)), -1)
    vals = [_quantile_row(c, r, qs_frac, method) for r in rows]       # rows x len(qs)
    flat = _round_fractions(c, [v for row in vals for v in row]).reshape(len(rows), len(qs_frac))
    res = flat.T.reshape(tuple(q_shape) + tuple(rest))
    if keepdims:
        res = res.reshape(tuple(q_shape) + tuple(1 if i in axes else n for i, n in enumerate(a.shape)))
    return PositArray(res, c)


def _qs(q, scale):
    qa = np.asarray(q, dtype=np.float64)
    fr = [Fraction(float(v)) / scale for v in qa.ravel()]
    if any(f < 0 or f > 1 for f in fr):
        raise ValueError("quantiles must be in [0, 1] (percentiles in [0, 100])")
    return fr, qa.shape


def _quantile(a, q, axis=None, out=None, overwrite_input=False, method="linear", keepdims=False, *,
              weights=None, interpolation=None):
    _no("np.quantile", weights=weights, interpolation=interpolation)
    fr, shp = _qs(q, 1)
    return A._to_out(_quantile_core(a, fr, shp, axis, method, keepdims, "quantile"), out)


def _percentile(a, q, axis=None, out=None, overwrite_input=False, method="linear", keepdims=False, *,
                weights=None, interpolation=None):
    _no("np.percentile", weights=weights, interpolation=interpolation)
    fr, shp = _qs(q, 100)
    return A._to_out(_quantile_core(a, fr, shp, axis, method, keepdims, "percentile"), out)


def _median(a, axis=None, out=None, overwrite_input=False, keepdims=False):
    return A._to_out(_quantile_core(a, [Fraction(1, 2)], (), axis, "linear", keepdims, "median"), out)


# ---- NaR as a missing value: nansum, nanmean, nanmax, nanmin, nanvar, nanstd ---------------------------------

def _without_nar(v: PositArray) -> PositArray:
    return PositArray(v.bits[~v.isnar()], v.config)


def _nansum(a, axis=None, dtype=None, out=None, keepdims=False, initial=_NV, where=True):
    _no("np.nansum", initial=initial, where=where)
    z = PositArray(np.where(a.isnar(), 0, a.bits), a.config)
    return A._to_out(_dtype(z.sum(axis=axis, keepdims=keepdims), dtype, "nansum"), out)


def _nanmean(a, axis=None, dtype=None, out=None, keepdims=False, *, where=True):
    _no("np.nanmean", where=where)
    return A._to_out(_dtype(a._along(axis, lambda v: int(_without_nar(v).mean().bits), keepdims), dtype,
                            "nanmean"), out)


def _nanvar_like(root):
    def f(a, axis=None, dtype=None, out=None, ddof=0, keepdims=False, *, where=True, mean=_NV, correction=_NV):
        _no("np.nanvar", where=where, mean=mean)
        ddof = ddof if correction is _NV else correction
        fn = (lambda v: int(_without_nar(v).std(ddof=ddof).bits)) if root else \
            (lambda v: int(_without_nar(v).var(ddof=ddof).bits))
        return A._to_out(_dtype(a._along(axis, fn, keepdims), dtype, "nanvar"), out)
    return f


def _nanmax_like(top):
    def f(a, axis=None, out=None, keepdims=False, initial=_NV, where=True):
        _no("np.nanmax", initial=initial, where=where)
        s = a._signed()
        big = (1 << (a.config.n - 1)) - 1
        if top:
            r = np.max(s, axis=axis, keepdims=keepdims)              # NaR is the minimum: ignored unless alone
        else:
            r = np.min(np.where(a.isnar(), big + 1, s), axis=axis, keepdims=keepdims)
            r = np.where(r > big, -(big + 1), r)
        return A._to_out(a._from_signed(r), out)
    return f


# ---- weighted average, diff, trace ----------------------------------------------------------------------------

def _average(a, axis=None, weights=None, returned=False, *, keepdims=False):
    if weights is None:
        r = a.mean(axis=axis, keepdims=keepdims)
        return (r, A.array(a.size if axis is None else np.prod([a.shape[i] for i in a._axes(axis)]), a.config)) \
            if returned else r
    if axis is not None or keepdims:
        raise TypeError("np.average with weights on posit arrays: axis=None only")
    x, w = A._unify(a, weights)
    if w.shape != x.shape:
        raise TypeError("np.average on posit arrays: weights of the same shape as a")
    _, _, qd, n1 = A._quire(x.ravel(), "dot", w.ravel())
    _, _, qs, n2 = A._quire(w.ravel(), "sum")
    c = x.config
    if n1 or n2 or qs == 0:
        r = PositArray(c.nar, c)
    else:
        r = PositArray(_round_fractions(c, [Fraction(qd, qs)])[0], c)   # (qd 2^-F) / (qs 2^-F)
    if returned:
        return r, PositArray(ref.quire_round(c, qs, n2)[0], c)
    return r


def _diff(a, n=1, axis=-1, prepend=_NV, append=_NV):
    parts = []
    if prepend is not _NV:
        p = _posit(prepend, a.config)
        parts.append(p if p.ndim else PositArray(np.broadcast_to(p.bits, _one_along(a.shape, axis)), p.config))
    parts.append(a)
    if append is not _NV:
        p = _posit(append, a.config)
        parts.append(p if p.ndim else PositArray(np.broadcast_to(p.bits, _one_along(a.shape, axis)), p.config))
    x = _concatenate(parts, axis=axis) if len(parts) > 1 else a
    for _ in range(n):
        m = x.shape[axis]
        hi = PositArray(np.take(x.bits, range(1, m), axis=axis), x.config)
        lo = PositArray(np.take(x.bits, range(0, m - 1), axis=axis), x.config)
        x = hi - lo
    return x


def _one_along(shape, axis):
    s = list(shape)
    s[axis] = 1
    return tuple(s)


def _trace(a, offset=0, axis1=0, axis2=1, dtype=None, out=None):
    d = PositArray(np.diagonal(a.bits, offset, axis1, axis2), a.config)
    return A._to_out(_dtype(d.sum(axis=-1), dtype, "trace"), out)


# ---- norms ------------------------------------------------------------------------------------------------------

def _vec_norm(v: PositArray, ord) -> int:
    c = v.config
    if ord is None or ord == 2:
        _, _, q, nar = A._quire(v, "sumsq")
        return A._sqrt_bits(c, q, nar)
    if ord == 1:
        return int(abs(v).sum().bits)
    if ord == np.inf:
        return int(abs(v).max().bits) if v.size else 0
    if ord == -np.inf:
        return int(abs(v).min().bits)
    if ord == 0:
        return int(A.array(float(np.count_nonzero(v.bits)), c).bits)
    raise TypeError(f"np.linalg.norm on posit arrays: ord={ord!r} not supported for vectors (None, 1, 2, inf, "
                    "-inf, 0)")


def _norm(x, ord=None, axis=None, keepdims=False):
    x = _posit(x)
    if axis is None and ord is None:
        r = x.norm()
        return r.reshape((1,) * x.ndim) if keepdims else r
    if axis is None:
        axis = tuple(range(x.ndim)) if x.ndim == 2 else (0 if x.ndim == 1 else None)
        if axis is None:
            raise TypeError("np.linalg.norm on posit arrays: ord= needs a vector or a matrix (or axis=)")
    axes = x._axes(axis)
    if len(axes) == 1:
        return x._along(axes, lambda v: _vec_norm(v, ord), keepdims)
    if len(axes) != 2:
        raise ValueError("np.linalg.norm: axis of one or two axes")
    r_ax, c_ax = (axis if np.ndim(axis) else axes)
    r_ax, c_ax = r_ax % x.ndim, c_ax % x.ndim
    if ord in (None, "fro"):
        return x._along(axes, lambda v: _vec_norm(v, None), keepdims)
    if ord in (1, -1, np.inf, -np.inf):
        along = r_ax if ord in (1, -1) else c_ax                   # 1: column sums (over rows), inf: row sums
        s = abs(x).sum(axis=along, keepdims=True)
        other = c_ax if ord in (1, -1) else r_ax
        r = s.max(axis=other, keepdims=True) if ord in (1, np.inf) else s.min(axis=other, keepdims=True)
        return r if keepdims else r.reshape([n for i, n in enumerate(r.shape) if i not in (r_ax, c_ax)])
    raise TypeError(f"np.linalg.norm on posit arrays: ord={ord!r} not supported for matrices ('fro', 1, -1, inf, "
                    "-inf; 2 and 'nuc' need an SVD)")


# ---- constructors ------------------------------------------------------------------------------------------------

def full(shape, fill_value, config: PositConfig = P32) -> PositArray:
    v = _posit(fill_value, config).astype(config) if isinstance(fill_value, PositArray) else _posit(fill_value, config)
    return PositArray(np.broadcast_to(v.bits, shape), config)


def ones(shape, config: PositConfig = P32) -> PositArray:
    return PositArray(np.full(shape, A._one_bits(config), np.int64), config)


def empty(shape, config: PositConfig = P32) -> PositArray:
    """Like zeros (no uninitialised memory)."""
    return A.zeros(shape, config)


def eye(N: int, M: Optional[int] = None, k: int = 0, config: PositConfig = P32) -> PositArray:
    return PositArray(np.eye(N, M, k, dtype=np.int64) * A._one_bits(config), config)


def identity(n: int, config: PositConfig = P32) -> PositArray:
    return eye(n, config=config)


def arange(*args, config: PositConfig = P32) -> PositArray:
    """np.arange(...) rounded to posits (exact for the integers the format holds)."""
    return A.array(np.arange(*args), config)


def linspace(start, stop, num: int = 50, endpoint: bool = True, config: PositConfig = P32) -> PositArray:
    """np.linspace in float64, then rounded to posits."""
    return A.array(np.linspace(start, stop, num, endpoint), config)


# ---- registration ---------------------------------------------------------------------------------------------

def _real(val):
    return val.copy()


def _imag(val):
    return A.zeros(val.shape, val.config)


FUNCTIONS = {
    np.sum: _sum_like("sum"), np.prod: _sum_like("prod"),
    np.max: _max_like("max"), np.min: _max_like("min"),
    np.mean: _mean, np.var: _var_like(False), np.std: _var_like(True),
    np.argmax: _arg_like("argmax"), np.argmin: _arg_like("argmin"),
    np.any: _any_all("any"), np.all: _any_all("all"),
    np.cumsum: _cumsum, np.sort: _sort, np.argsort: _argsort, np.reshape: _reshape,
    np.ravel: lambda a, order="C": a.ravel(order=order),
    np.copy: lambda a, order="K", subok=False: a.copy(),
    np.concatenate: _concatenate, np.stack: _stack, np.where: _where, np.dot: _dot,
    # shape
    np.flip: _on_bits(np.flip), np.fliplr: _on_bits(np.fliplr), np.flipud: _on_bits(np.flipud),
    np.roll: _on_bits(np.roll), np.rot90: _on_bits(np.rot90), np.tile: _on_bits(np.tile),
    np.repeat: _on_bits(np.repeat), np.squeeze: _on_bits(np.squeeze), np.expand_dims: _on_bits(np.expand_dims),
    np.swapaxes: _on_bits(np.swapaxes), np.moveaxis: _on_bits(np.moveaxis), np.rollaxis: _on_bits(np.rollaxis),
    np.broadcast_to: _on_bits(np.broadcast_to), np.diag: _on_bits(np.diag), np.diagonal: _on_bits(np.diagonal),
    np.triu: _on_bits(np.triu), np.tril: _on_bits(np.tril), np.take: _on_bits(np.take),
    np.take_along_axis: _on_bits(np.take_along_axis), np.resize: _on_bits(np.resize), np.delete: _on_bits(np.delete),
    np.trim_zeros: _on_bits(np.trim_zeros), np.atleast_1d: _on_bits(np.atleast_1d),
    np.atleast_2d: _on_bits(np.atleast_2d), np.atleast_3d: _on_bits(np.atleast_3d),
    np.split: _split(np.split), np.array_split: _split(np.array_split), np.hsplit: _split(np.hsplit),
    np.vsplit: _split(np.vsplit), np.dsplit: _split(np.dsplit),
    np.hstack: _joined(np.hstack), np.vstack: _joined(np.vstack), np.dstack: _joined(np.dstack),
    np.column_stack: _joined(np.column_stack), np.append: _append, np.insert: _insert, np.pad: _pad,
    np.broadcast_arrays: _broadcast_arrays,
    np.zeros_like: _like(0.0), np.ones_like: _like(1.0), np.empty_like: _like(0.0), np.full_like: _like(None),
    # selection
    np.unique: _unique, np.searchsorted: _searchsorted, np.partition: _partition, np.argpartition: _argpartition,
    np.nonzero: lambda a: np.nonzero(a.bits), np.flatnonzero: lambda a: np.flatnonzero(a.bits),
    np.argwhere: lambda a: np.argwhere(a.bits), np.count_nonzero: _count_nonzero,
    # values
    np.isclose: _isclose, np.allclose: _allclose, np.array_equiv: _array_equiv,
    np.median: _median, np.quantile: _quantile, np.percentile: _percentile,
    np.nansum: _nansum, np.nanmean: _nanmean, np.nanvar: _nanvar_like(False), np.nanstd: _nanvar_like(True),
    np.nanmax: _nanmax_like(True), np.nanmin: _nanmax_like(False),
    np.average: _average, np.diff: _diff, np.trace: _trace,
    np.real: _real, np.imag: _imag, np.angle: lambda z, deg=False: _angle_real(z, deg),
    np.iscomplexobj: lambda x: False, np.isrealobj: lambda x: True,
    np.linalg.norm: _norm,
}


def _angle_real(x, deg):
    r = arctan2(A.zeros(x.shape, x.config), x)
    return r * (180 / math.pi) if deg else r


for _f, _g in FUNCTIONS.items():
    A._FUNCTIONS[_f] = _g
from . import fft as _F                                         # noqa: E402

for _f, _g in {np.fft.fft: _F.fft, np.fft.ifft: _F.ifft, np.fft.rfft: _F.rfft, np.fft.irfft: _F.irfft,
               np.fft.fft2: _F.fft2, np.fft.ifft2: _F.ifft2, np.fft.fftn: _F.fftn, np.fft.ifftn: _F.ifftn,
               np.fft.fftshift: _F.fftshift, np.fft.ifftshift: _F.ifftshift}.items():
    A._FUNCTIONS[_f] = _g
for _alias, _src in (("amax", np.max), ("amin", np.min)):
    if hasattr(np, _alias):
        A._FUNCTIONS[getattr(np, _alias)] = FUNCTIONS[_src]

__all__ = ["full", "ones", "empty", "eye", "identity", "arange", "linspace", "hypot", "arctan2", "sign", "copysign",
           "sqrt_exact"]
