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
        return x.astype(config or x.config)
    c = config or P32
    if _is_complex(x):                                         # complex numbers: a PositComplex, parts rounded
        from .fft import PositComplex
        z = np.asarray(x)
        return PositComplex(array(z.real, c), array(z.imag, c))
    if isinstance(x, np.ndarray) and x.dtype == np.float32 and c.n == 32 and c.es == 2 and x.size:
        be = get_backend(c)
        if "f2p" in getattr(be, "array_ops", ()):
            return PositArray._wrap(be.array_op("f2p", x.view(np.uint32), scalar=0), c)
    return PositArray(vecops.from_float(c, np.asarray(x, dtype=np.float64)).bits, c)


def _is_complex(x) -> bool:
    if hasattr(x, "to_complex") and hasattr(x, "imag"):         # PositComplex
        return True
    if isinstance(x, (complex, np.complexfloating)):
        return True
    if isinstance(x, np.ndarray):
        return np.iscomplexobj(x)
    if isinstance(x, (list, tuple)):
        try:
            return np.iscomplexobj(np.asarray(x))
        except (TypeError, ValueError):                         # posit arrays inside, ragged lists: not complex
            return False
    return False


def _as_pc(x):
    from .fft import PositComplex
    return PositComplex(x, PositArray(np.zeros(x.shape, np.int64), x.config))


_PC_OPS = {"add": lambda z, o: z + o, "sub": lambda z, o: z - o, "mul": lambda z, o: z * o,
           "div": lambda z, o: z / o}
_PC_ROPS = {"add": lambda z, o: o + z, "sub": lambda z, o: o - z, "mul": lambda z, o: o * z,
            "div": lambda z, o: o / z}


def to_device(x) -> "PositArray":
    """pn.to_device(x): x (PositArray) in the device memory of its format's engine (see PositArray.to_device)."""
    return x.to_device()


def fma(a, b, c) -> "PositArray":
    """a * b + c with one rounding, broadcast like NumPy.

    On the KV260: c a scalar -> OP_FMA (c in OPERAND_C); b a scalar and c an array -> OP_AXPY;
    three arrays -> OP_FMA3 on stage 6 engines (own AXI4 master, a third read stream), on older
    engines PositSw (counted in stats()["fallback"])."""
    A, B, C = _unify(a, b, c)
    cfg = A.config
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
    return PositArray._wrap(np.asarray(r).reshape(shape), cfg)


def matmul(a, b) -> "PositArray":
    """a @ b like np.matmul: every element the exact quire dot product, rounded once.

    On a KV260 engine with gemv (PositVecAxi 5.1): OP_XLOAD / OP_GEMV, 1 multiply-add per clock;
    elsewhere PositSw or one quire dot per element (both exact, slower)."""
    A, B = _unify(a, b)
    if A.ndim == 0 or B.ndim == 0:
        raise ValueError("matmul: scalar operands are not allowed, use *")
    if A.ndim == 1 and B.ndim == 1:
        return A.dot(B)
    Ab = A.bits if A.ndim > 1 else A.bits[None, :]
    Bb = B.bits if B.ndim > 1 else B.bits[:, None]
    if Ab.shape[-1] != Bb.shape[-2]:
        raise ValueError(f"matmul: shapes {A.shape} and {B.shape} not aligned")
    be = get_backend(A.config)
    mm = getattr(be, "matmul", None) or _sw.setdefault(A.config, PositSw(A.config)).matmul
    batch = np.broadcast_shapes(Ab.shape[:-2], Bb.shape[:-2])
    if not batch:
        C = mm(Ab, Bb)
    else:
        Ab = np.broadcast_to(Ab, batch + Ab.shape[-2:])
        Bb = np.broadcast_to(Bb, batch + Bb.shape[-2:])
        C = np.empty(batch + (Ab.shape[-2], Bb.shape[-1]), bits_dtype(A.config))
        for idx in np.ndindex(*batch):
            C[idx] = mm(np.ascontiguousarray(Ab[idx]), np.ascontiguousarray(Bb[idx]))
    if A.ndim == 1:
        C = C[..., 0, :]
    if B.ndim == 1:
        C = C[..., 0]
    return PositArray._wrap(np.ascontiguousarray(C), A.config)


def _conv_slice(A: "PositArray", V: "PositArray", lo: int, n_out: int) -> "PositArray":
    """Exact full convolution of 1-D A and V, elements [lo, lo + n_out); each rounded once.

    The longer array is the signal (convolution commutes). Through the backend's correlate:
    y[k] = sum_j xz[k + j] h[j] with h = taps reversed and xz the signal with T - 1 zeros on each side.
    NaR taps would meet those padding zeros (0 * NaR = NaR) in outputs where they are no term of the
    sum, so they go in as 0 and NaR is set afterwards exactly where it belongs (k - q in [0, N)).
    """
    c = A.config
    if V.size > A.size:
        A, V = V, A
    x, v = A.bits.ravel(), V.bits.ravel()
    N, T = x.size, v.size
    nar_q = np.nonzero(v == c.nar)[0]
    h = np.where(v == c.nar, 0, v)[::-1].copy()
    xp = np.zeros(n_out + T - 1, x.dtype)                  # xz[lo : lo + n_out + T - 1], xz[k] = x[k - (T - 1)]
    s0 = lo - (T - 1)
    a0, a1 = max(0, s0), min(N, s0 + xp.size)
    if a1 > a0:
        xp[a0 - s0:a1 - s0] = x[a0:a1]
    be = get_backend(c)
    corr = getattr(be, "correlate", None) or _sw.setdefault(c, PositSw(c)).correlate
    y = np.asarray(corr(xp, h)).astype(bits_dtype(c))
    if nar_q.size:                                         # full output k is NaR when k - q in [0, N) for a NaR tap q
        d = np.zeros(N + T, np.int64)
        np.add.at(d, nar_q, 1)
        np.add.at(d, nar_q + N, -1)
        hit = np.cumsum(d)[lo:lo + n_out] > 0
        y = np.where(hit, c.nar, y)
    return PositArray._wrap(y, c)


def _conv_args(a, v):
    A, V = _unify(a, v)
    if A.ndim != 1 or V.ndim != 1:
        raise ValueError("convolve / correlate: 1-D arrays only")
    if A.size == 0 or V.size == 0:
        raise ValueError("convolve / correlate: empty array")
    return A, V


def convolve(a, v, mode: str = "full") -> "PositArray":
    """np.convolve on posits: every output the exact sum of products in the quire, rounded once.

    On a 5.4 KV260 engine: FIR through GEMV with a window over the signal (signal read once, P outputs
    per pass); elsewhere PositSw."""
    A, V = _conv_args(a, v)
    n, t = max(A.size, V.size), min(A.size, V.size)
    lo, ln = {"full": (0, n + t - 1), "same": ((t - 1) // 2, n), "valid": (t - 1, n - t + 1)}[_mode(mode)]
    return _conv_slice(A, V, lo, ln)


def correlate(a, v, mode: str = "valid") -> "PositArray":
    """np.correlate on posits (real): c[k] = sum_n a[n + k] v[n], exact in the quire, rounded once."""
    A, V = _conv_args(a, v)
    n, t = max(A.size, V.size), min(A.size, V.size)
    same_lo = (t - 1) // 2 if A.size >= V.size else (t - 1) - (t - 1) // 2
    lo, ln = {"full": (0, n + t - 1), "same": (same_lo, n), "valid": (t - 1, n - t + 1)}[_mode(mode)]
    return _conv_slice(A, PositArray._wrap(V.bits[::-1].copy(), V.config), lo, ln)


def _mode(mode) -> str:
    m = {0: "valid", 1: "same", 2: "full"}.get(mode, mode)
    if m not in ("full", "same", "valid"):
        raise ValueError(f"mode {mode!r}: expected 'full', 'same' or 'valid'")
    return m


def func(name: str, x) -> "PositArray":
    """f(x) for a positnp.funcs function (exp, log, sin, tanh, sigmoid, ...): correctly rounded tables for
    posit8 / posit16 (looked up by the engine on 5.3 KV260 bitstreams), float64 evaluation for posit32."""
    x = x if isinstance(x, PositArray) else array(x)
    return x._func(name)


def sigmoid(x) -> "PositArray":
    """1 / (1 + exp(-x)), rounded once (not a NumPy ufunc)."""
    return func("sigmoid", x)


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


# ---- formats: one format per operation ----------------------------------------------------------
#
# Formats with the same es nest: posit<8,es> is a subset of posit<16,es> and of posit<32,es> (the bits padded
# with zeros), so an operation on mixed formats widens the narrower operands to the widest one without any
# rounding, like float32 + float64 in NumPy. Different es: no exact common format, TypeError.

def _promoted(configs) -> PositConfig:
    cs = set(configs)
    if len(cs) == 1:
        return cs.pop()
    if len({c.es for c in cs}) != 1:
        raise TypeError(f"mixed posit formats {', '.join(sorted(map(str, cs)))}: different es, "
                        "convert with x.astype(config) first")
    return max(cs, key=lambda c: c.n)


def _unify(*xs, default: Optional[PositConfig] = None):
    """The operands as PositArrays of one format; numbers and ndarrays are rounded to it."""
    configs = [x.config for x in xs if isinstance(x, PositArray)]
    c = _promoted(configs) if configs else (default or P32)
    out = []
    for x in xs:
        if isinstance(x, PositArray):
            out.append(x if x.config == c else x.astype(c))
        elif isinstance(x, np.ndarray) and x.dtype == bool:
            raise TypeError("boolean arrays are not posits")
        else:
            out.append(array(x, c))
    return out


# ---- exact values rounded once, in the PS -------------------------------------------------------

def _round_exact(c: PositConfig, ms, es, sticky=None, nar=None) -> np.ndarray:
    """Many exact values m * 2**e (Python ints; sticky: something nonzero below m's last bit) rounded once.

    Each value is cut to 53 bits with round-to-odd (every dropped bit, and the sticky bit, OR-ed into the
    last kept bit). A later round-to-nearest-even to at most 51 bits cannot tell that float64 from the
    exact value, so vecops.from_float rounds it exactly as one rounding of the exact value would."""
    f = np.zeros(len(ms))
    for i, (m, e) in enumerate(zip(ms, es)):
        if m == 0:
            continue
        a = abs(m)
        sh = a.bit_length() - 53                         # exactly 53 bits: drop the excess or pad with zeros
        t = a >> sh if sh > 0 else a << -sh
        e += sh
        if (sh > 0 and (t << sh) != a) or (sticky is not None and sticky[i]):
            t |= 1                                       # round to odd at 53 bits
        top = t.bit_length() + e                        # posits saturate long before float64 does
        e = min(max(e, -1000 - t.bit_length()), 1000 - t.bit_length()) if abs(top) > 1000 else e
        f[i] = math.ldexp(-t if m < 0 else t, e)
    bits = vecops.from_float(c, f).bits.astype(np.int64)
    if nar is not None:
        bits = np.where(np.asarray(nar, bool), c.nar, bits)
    return bits


def _one_bits(c: PositConfig) -> int:
    return ref.round_(c, (1, 0))[0]


def _pown(x: "PositArray", n: int) -> "PositArray":
    """x ** n for an integer n, correctly rounded (exact power in Python integers, one rounding).
    NaR ** n, 0 ** 0 (posit standard: pow(0, 0) is NaR) and 0 ** (n < 0) are NaR."""
    c = x.config
    if abs(n) > 1 << 16:
        raise ValueError(f"x ** {n}: exponent too large (|n| <= 65536)")
    u, inv = np.unique(x.bits.ravel(), return_inverse=True)
    ms, es, st, bad = [], [], [], []
    for b in u.tolist():
        d = ref.decode(c, b)
        if d is None or (d[0] == 0 and n <= 0):
            ms.append(0), es.append(0), st.append(False), bad.append(True)
            continue
        m, e = d
        if m == 0:
            ms.append(0), es.append(0), st.append(False), bad.append(False)
            continue
        tz = (m & -m).bit_length() - 1                   # odd mantissa: smaller powers
        m, e = m >> tz, e + tz
        s = abs(m).bit_length() - 1 + e                  # 2**s <= |x| < 2**(s+1)
        k = abs(n)
        sign = -1 if (m < 0 and k % 2) else 1
        lo, hi = (s * n, (s + 1) * n) if n > 0 else ((s + 1) * n, s * n)   # 2**lo <= |x**n| <= 2**hi
        if n == 0:
            ms.append(1), es.append(0), st.append(False), bad.append(False)
        elif lo > c.max_scale + 2:                         # above maxpos: saturates, no need for the digits
            ms.append(sign), es.append(c.max_scale + 4), st.append(False), bad.append(False)
        elif hi < -c.max_scale - 2:                        # below minpos: minpos
            ms.append(sign), es.append(-c.max_scale - 4), st.append(False), bad.append(False)
        elif n > 0:
            ms.append(m ** k), es.append(e * k), st.append(False), bad.append(False)
        else:
            q, inexact = ref._div((1, 0), (m ** k, e * k))
            ms.append(q[0]), es.append(q[1]), st.append(inexact), bad.append(False)
    bits = _round_exact(c, ms, es, st, bad)
    return PositArray(bits[inv].reshape(x.shape), c)


def _cumsum_bits(c: PositConfig, v: np.ndarray) -> np.ndarray:
    """Exact running sum in quire units (Python ints), every prefix rounded once; NaR from the first NaR on."""
    ms, bad = [], []
    q, nar = 0, False
    for b in v.tolist():
        t = ref.quire_term(c, int(b))
        if t is None:
            nar = True
        elif not nar:
            q += t
        ms.append(0 if nar else q)
        bad.append(nar)
    return _round_exact(c, ms, [-c.quire_frac] * len(ms), None, bad)


def _prod_bits(c: PositConfig, v: np.ndarray) -> int:
    """Exact product (Python ints, pairwise), rounded once; saturates to maxpos / minpos like any posit result."""
    ds = [ref.decode(c, int(b)) for b in v.tolist()]
    if any(d is None for d in ds):
        return c.nar
    if any(d[0] == 0 for d in ds):
        return 0
    ms, E, lo, neg = [], 0, 0, False
    for m, e in ds:
        tz = (m & -m).bit_length() - 1
        m, e = m >> tz, e + tz
        neg ^= m < 0
        ms.append(abs(m))
        E += e
        lo += abs(m).bit_length() - 1 + e                 # 2**lo <= |product| < 2**(lo + len)
    sign = -1 if neg else 1
    if lo > c.max_scale + 1 or lo + len(ds) < -c.max_scale - 1:
        return ref.round_(c, (sign, lo))[0]               # beyond maxpos / below minpos: saturates
    while len(ms) > 1:
        ms = [ms[i] * ms[i + 1] if i + 1 < len(ms) else ms[i] for i in range(0, len(ms), 2)]
    return ref.round_(c, (sign * (ms[0] if ms else 1), E))[0]


def _var_bits(c: PositConfig, x: "PositArray", ddof: int, root: bool) -> int:
    """Variance (root: standard deviation) from two exact quire sums, rounded once.

    S1 = sum x, S2 = sum x**2 (both exact): var = (N S2 - S1**2) / (N (N - ddof)), an exact rational -
    no cancellation, unlike the two-pass float formula; std = sqrt(var) through an integer square root with
    a sticky bit."""
    N = x.size
    if N - ddof <= 0:
        return c.nar
    _, _, q1, nar1 = _quire(x, "sum")
    _, _, q2, nar2 = _quire(x, "sumsq")
    if nar1 or nar2:
        return c.nar
    F = c.quire_frac
    num = ((N * q2) << F) - q1 * q1                       # (N S2 - S1**2) * 2**(2F) >= 0
    den = N * (N - ddof)
    if num == 0:
        return 0
    if not root:
        q, inexact = ref._div((num, -2 * F), (den, 0))
        return ref.round_(c, q, inexact)[0]
    extra = 64 + c.n + den.bit_length()
    t, r = divmod(num << (2 * extra), den)
    s = math.isqrt(t)
    return ref.round_(c, (s, -F - extra), r != 0 or s * s != t)[0]


def _no_out(name, out=None, dtype=None, keepdims=False, **kw):
    if out is not None or dtype is not None or keepdims or kw.get("where", True) is not True:
        raise TypeError(f"np.{name} on posit arrays: out=, dtype=, keepdims= and where= are not supported")


# ---- files ---------------------------------------------------------------------------------------

def save(file, x) -> None:
    """Save a PositArray (or PositComplex) with its format to a .npz file: the bits plus n and es.
    np.save cannot keep the format (a .npy holds plain integers), so it refuses posit arrays."""
    from .fft import PositComplex
    if isinstance(x, PositComplex):
        np.savez(file, real=x.real.bits, imag=x.imag.bits, n=x.config.n, es=x.config.es)
    elif isinstance(x, PositArray):
        np.savez(file, bits=x.bits, n=x.config.n, es=x.config.es)
    else:
        raise TypeError(f"pn.save: PositArray or PositComplex, got {type(x).__name__}")


def load(file):
    """Load what pn.save wrote: a PositArray or a PositComplex in its own format."""
    from .fft import PositComplex
    with np.load(file, allow_pickle=False) as z:
        c = PositConfig(int(z["n"]), int(z["es"]))
        if "bits" in z.files:
            return PositArray(z["bits"], c)
        return PositComplex(PositArray(z["real"], c), PositArray(z["imag"], c))


# ---- the class ----------------------------------------------------------------------------------

_LATER: Dict[str, str] = {}


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

    @classmethod
    def _wrap(cls, bits, config: PositConfig, view: bool = False) -> "PositArray":
        """Result of a backend: no copy when the bits already have the storage dtype (device memory stays
        device memory); otherwise as the constructor. view=True keeps a strided view too (the parts of a
        device PositComplex)."""
        bits = np.asarray(bits)
        dt = bits_dtype(config)
        if bits.dtype == dt and config.n == 8 * dt().itemsize and (bits.flags.c_contiguous or view):
            obj = cls.__new__(cls)
            obj.config = config
            obj.bits = bits
            return obj
        return cls(bits, config)

    # ---- device memory (KV260 engines with their own AXI4 master) --------------------------------

    def to_device(self) -> "PositArray":
        """Copy into the device memory of this format's engine; later operations whose inputs are all
        on the device run without copies and keep their results there. No device: unchanged."""
        be = get_backend(self.config)
        if getattr(be, "heap", None) is None:
            return self
        if be.on_device(self.bits):
            return self
        return PositArray._wrap(be.to_device(self.bits), self.config)

    def to_host(self) -> "PositArray":
        """A copy in ordinary (host) memory."""
        return PositArray(self.bits.copy(), self.config)

    @property
    def on_device(self) -> bool:
        be = get_backend(self.config)
        return bool(getattr(be, "heap", None) is not None and be.on_device(self.bits))

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

    def astype(self, dtype):
        """x.astype(pn.P16): another posit format (always a copy); x.astype(np.float32 / np.float64): floats.

        Widening with the same es (P8 -> P16 -> P32) is exact: the bits padded with zeros. Any other change
        of format is one correct rounding (every posit up to 32 bits is exact in float64)."""
        if isinstance(dtype, PositConfig):
            s = self.config
            if dtype == s:
                return self.copy()
            if dtype.es == s.es and dtype.n >= s.n:
                return PositArray(self.bits.astype(np.int64) << (dtype.n - s.n), dtype)
            return PositArray(vecops.from_float(dtype, self.to_float()).bits, dtype)
        dt = np.dtype(dtype)
        if dt == np.float32:
            return self.to_float32()
        if dt == np.float64:
            return self.to_float()
        raise TypeError(f"PositArray.astype({dt}): a posit format (pn.P16, ...), float32 or float64 (bits: .bits)")

    def _func(self, name: str) -> "PositArray":
        be = get_backend(self.config)
        fn = getattr(be, "func", None) or _sw.setdefault(self.config, PositSw(self.config)).func
        flat = self.bits.reshape(-1)
        return PositArray._wrap(np.asarray(fn(name, flat)).reshape(self.shape), self.config)

    def sqrt(self) -> "PositArray":
        """Correctly rounded square root; negative -> NaR."""
        r = get_backend(self.config).array_op("sqrt", self.bits.ravel(), scalar=0)
        return PositArray._wrap(np.asarray(r).reshape(self.shape), self.config)

    def __float__(self) -> float:
        if self.size != 1:
            raise TypeError("only one-element posit arrays convert to float")
        return float(self.to_float().reshape(()))

    def __int__(self) -> int:
        """int(x) of a one-element array: truncated toward zero, like int(float); NaR: ValueError."""
        f = float(self)
        if math.isnan(f):
            raise ValueError("cannot convert NaR to an integer")
        return int(f)

    def __bool__(self) -> bool:
        if self.size != 1:
            raise ValueError("the truth value of a posit array with more than one element is ambiguous "
                             "(use (x != 0).any() or .all())")
        return bool(self.bits.reshape(()) != 0)          # NaR is true, like nan

    def __array__(self, dtype=None, copy=None):
        raise TypeError("a PositArray does not convert to an ndarray implicitly (its integers are posit bits): "
                        "use x.to_float(), x.to_float32() or x.bits")

    def tolist(self):
        """Nested lists of Python floats (NaR -> nan), like ndarray.tolist()."""
        return self.to_float().tolist()

    def item(self, *args) -> float:
        """A Python float, like ndarray.item(): no argument (size 1), a flat index, or one index per axis."""
        if not args:
            return float(self)
        if len(args) == 1 and np.ndim(args[0]) == 0:
            return float(self.ravel()[int(args[0])])
        return float(self[tuple(args[0]) if len(args) == 1 else args])

    @property
    def nbytes(self) -> int:
        return self.bits.nbytes

    @property
    def itemsize(self) -> int:
        return self.bits.itemsize

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
        v = value.astype(self.config) if isinstance(value, PositArray) else self._coerce(value)   # rounds like NumPy
        self.bits[idx] = v.bits

    def copy(self) -> "PositArray":
        return PositArray(self.bits.copy(), self.config)

    def reshape(self, *shape, order: str = "C") -> "PositArray":
        return PositArray(self.bits.reshape(*shape, order=order), self.config)

    def ravel(self, order: str = "C") -> "PositArray":
        return PositArray(self.bits.ravel(order=order), self.config)

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
        """other in this array's format: numbers are rounded, narrower posits (same es) widened exactly."""
        if isinstance(other, PositArray):
            if other.config != self.config:
                if _promoted([self.config, other.config]) != self.config:
                    raise TypeError(f"{other.config} into {self.config} would round: use x.astype({self.config})")
                return other.astype(self.config)
            return other
        return _unify(other, default=self.config)[0]

    def _binary(self, name: str, other) -> "PositArray":
        if _is_complex(other):                                  # real (op) complex -> PositComplex
            return _PC_OPS[name](_as_pc(self), other)
        s, o = _unify(self, other)
        be = get_backend(s.config)
        if o.size == 1 and o.shape in ((), (1,)) and s.size >= 1:
            r = be.array_op(name, s.bits.ravel(), scalar=int(o.bits.ravel()[0]))
            shape = np.broadcast_shapes(s.shape, o.shape)
            return PositArray._wrap(r.reshape(shape), s.config)
        a, b = np.broadcast_arrays(s.bits, o.bits)
        r = be.array_op(name, np.ascontiguousarray(a).ravel(), np.ascontiguousarray(b).ravel())
        return PositArray._wrap(r.reshape(a.shape), s.config)

    def _rbinary(self, name: str, other) -> "PositArray":
        if _is_complex(other):
            return _PC_ROPS[name](_as_pc(self), other)
        s, o = _unify(self, other)
        return o._binary(name, s)

    def __pow__(self, p):
        """x ** p for a number p: 2 is a multiply, -1 a division, 0.5 the square root (one rounding each, on
        the engine); any other integer the exact power rounded once (in the PS). Other exponents: TypeError."""
        if isinstance(p, (PositArray, bool, np.bool_)) or np.ndim(p) != 0:
            raise TypeError("x ** p on posit arrays: p must be a number (an integer or 0.5)")
        if p == 0.5:
            return self.sqrt()
        if float(p) != int(p):
            raise TypeError(f"x ** {p!r}: integer exponents and 0.5 only (pn.func('exp', y * pn.func('log', x)) "
                            "for others)")
        n = int(p)
        if n == 1:
            return self.copy()
        if n == 2:
            return self._binary("mul", self)
        if n == -1:
            return PositArray(_one_bits(self.config), self.config)._binary("div", self)
        return _pown(self, n)

    # ---- arithmetic: the backend ------------------------------------------------------------

    def __add__(self, other):
        return self._binary("add", other)

    def __sub__(self, other):
        return self._binary("sub", other)

    def __mul__(self, other):
        return self._binary("mul", other)

    def __truediv__(self, other):
        return self._binary("div", other)

    def __matmul__(self, other):
        return matmul(self, other)

    def __rmatmul__(self, other):
        return matmul(self._coerce(other), self)

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
        s, o = _unify(self, other)
        return op(s._signed(), o._signed())

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

    def max(self, axis=None, keepdims: bool = False):
        return self._from_signed(np.max(self._signed(), axis=axis, keepdims=keepdims))

    def min(self, axis=None, keepdims: bool = False):
        return self._from_signed(np.min(self._signed(), axis=axis, keepdims=keepdims))

    def argmax(self, axis=None, keepdims: bool = False):
        return np.argmax(self._signed(), axis=axis, keepdims=keepdims)

    def argmin(self, axis=None, keepdims: bool = False):
        return np.argmin(self._signed(), axis=axis, keepdims=keepdims)

    def any(self, axis=None, keepdims: bool = False):
        return np.any(self.bits != 0, axis=axis, keepdims=keepdims)

    def all(self, axis=None, keepdims: bool = False):
        return np.all(self.bits != 0, axis=axis, keepdims=keepdims)

    # ---- quire reductions (stage 4) -----------------------------------------------------------

    def _axes(self, axis) -> tuple:
        """axis (None, an int or a tuple, negative allowed) as a sorted tuple of axes."""
        if axis is None:
            return tuple(range(self.ndim))
        axes = (axis,) if np.ndim(axis) == 0 else tuple(axis)
        out = set()
        for a in axes:
            a = int(a)
            if not -self.ndim <= a < self.ndim:
                raise np.exceptions.AxisError(a, self.ndim)
            out.add(a % self.ndim)
        if len(out) != len(axes):
            raise ValueError("repeated axis")
        return tuple(sorted(out))

    def _reduce_rows(self, axis, keepdims: bool, rows_fn) -> "PositArray":
        """rows_fn(bits of shape (R, K)) -> R result bits: the reduced axes moved last and flattened to K."""
        axes = self._axes(axis)
        keep = [i for i in range(self.ndim) if i not in axes]
        moved = np.transpose(self.bits, keep + list(axes))
        rest = moved.shape[:len(keep)]
        R, K = int(np.prod(rest, dtype=np.int64)), int(np.prod(moved.shape[len(keep):], dtype=np.int64))
        res = np.asarray(rows_fn(moved.reshape(R, K)), np.int64).reshape(rest)
        if keepdims:
            res = res.reshape([1 if i in axes else n for i, n in enumerate(self.shape)])
        return PositArray(res, self.config)

    def _along(self, axis, fn, keepdims: bool = False) -> "PositArray":
        """fn(1-D PositArray) -> bits, for axis None (all elements), one axis or a tuple of axes."""
        c = self.config
        return self._reduce_rows(axis, keepdims, lambda rows: [fn(PositArray(r, c)) for r in rows])

    def _sum_rows(self, rows: np.ndarray) -> np.ndarray:
        """Exact row sums, rounded once. Many rows: one matmul with a column of ones (each element the same
        exact quire sum; on a KV260 engine with gemv a single command instead of one per row)."""
        c = self.config
        R, K = rows.shape
        if R >= 2 and K >= 1:
            ones = PositArray(np.full((K, 1), _one_bits(c), np.int64), c)
            return matmul(PositArray._wrap(np.ascontiguousarray(rows), c), ones).bits.reshape(R)
        return [_quire(PositArray(r, c), "sum")[0] for r in rows]

    def sum(self, axis=None, keepdims: bool = False) -> "PositArray":
        """Exact sum in the quire, rounded once."""
        return self._reduce_rows(axis, keepdims, self._sum_rows)

    def mean(self, axis=None, keepdims: bool = False) -> "PositArray":
        """Exact sum / N, rounded once."""
        def one(v):
            _, _, q, nar = _quire(v, "sum")
            return _mean_bits(self.config, q, nar, v.size)
        return self._along(axis, one, keepdims)

    def dot(self, other) -> "PositArray":
        """1-D . 1-D: exact sum of products in the quire, rounded once; 2-D: matmul (np.dot of matrices)."""
        s, o = _unify(self, other)
        if s.ndim in (1, 2) and o.ndim in (1, 2) and (s.ndim == 2 or o.ndim == 2):
            return matmul(s, o)
        if s.ndim != 1 or o.ndim != 1:
            raise TypeError(f"dot of shapes {s.shape} and {o.shape}: vectors and matrices only (use matmul)")
        if o.size != s.size:
            raise ValueError(f"dot of {s.size} and {o.size} elements")
        return PositArray(_quire(s, "dot", o)[0], s.config)

    def norm(self) -> "PositArray":
        """Euclidean norm of all elements: exact sum of squares, square root rounded once."""
        _, _, q, nar = _quire(self.ravel(), "sumsq")
        return PositArray(_sqrt_bits(self.config, q, nar), self.config)

    def var(self, axis=None, ddof: int = 0, keepdims: bool = False) -> "PositArray":
        """Variance from two exact quire sums (sum x, sum x**2), rounded once - no cancellation."""
        return self._along(axis, lambda v: _var_bits(self.config, v, ddof, False), keepdims)

    def std(self, axis=None, ddof: int = 0, keepdims: bool = False) -> "PositArray":
        """Standard deviation: the square root of the exact variance, rounded once."""
        return self._along(axis, lambda v: _var_bits(self.config, v, ddof, True), keepdims)

    def prod(self, axis=None, keepdims: bool = False) -> "PositArray":
        """Exact product, rounded once (computed in the PS: the engine has no product reduction)."""
        return self._along(axis, lambda v: _prod_bits(self.config, v.bits), keepdims)

    def cumsum(self, axis=None) -> "PositArray":
        """Running sums, each the exact sum of its prefix rounded once (in the PS); axis None: flattened."""
        c = self.config
        if axis is None:
            return PositArray(_cumsum_bits(c, self.bits.ravel()), c)
        moved = np.moveaxis(self.bits, axis, -1)
        rows = moved.reshape(-1, moved.shape[-1])
        out = np.array([_cumsum_bits(c, r) for r in rows], np.int64).reshape(moved.shape)
        return PositArray(np.moveaxis(out, -1, axis), c)

    # ---- level 0 again: clip and rounding to integers, exact ---------------------------------------

    def clip(self, a_min=None, a_max=None) -> "PositArray":
        """Clamp on the bits (posits order like integers); NaR stays NaR, a NaR bound gives NaR."""
        if a_min is None and a_max is None:
            raise ValueError("clip: a_min and a_max cannot both be None")
        xs = _unify(self, *(b for b in (a_min, a_max) if b is not None))
        x, rest = xs[0], list(xs[1:])
        lo = rest.pop(0) if a_min is not None else None
        hi = rest.pop(0) if a_max is not None else None
        s = x._signed()
        bad = x.isnar()
        if lo is not None:
            s = np.maximum(s, lo._signed())
            bad = bad | lo.isnar()
        if hi is not None:
            s = np.minimum(s, hi._signed())
            bad = bad | hi.isnar()
        r = x._from_signed(s)
        return PositArray(np.where(bad, x.config.nar, r.bits), x.config)

    def _to_integer(self, fn) -> "PositArray":
        # exact: posits up to 32 bits are exact in float64, and an integer obtained by rounding a posit is
        # itself a posit (same binade with an ulp <= 1, or the next power of two)
        return PositArray(vecops.from_float(self.config, fn(self.to_float())).bits, self.config)

    def floor(self) -> "PositArray":
        return self._to_integer(np.floor)

    def ceil(self) -> "PositArray":
        return self._to_integer(np.ceil)

    def trunc(self) -> "PositArray":
        return self._to_integer(np.trunc)

    def rint(self) -> "PositArray":
        """Nearest integer, ties to even (as np.rint and np.round)."""
        return self._to_integer(np.rint)

    def round(self, decimals: int = 0) -> "PositArray":
        if decimals != 0:
            raise TypeError("round on posit arrays: decimals=0 only (10**-d is not a posit, so the result "
                            "would not be exact)")
        return self.rint()

    # ---- NumPy protocols ----------------------------------------------------------------------

    _UFUNCS = {
        np.add: "add", np.subtract: "sub", np.multiply: "mul", np.true_divide: "div",
        np.negative: "neg", np.positive: "pos", np.absolute: "abs", np.square: "square",
        np.less: "lt", np.less_equal: "le", np.greater: "gt", np.greater_equal: "ge",
        np.equal: "eq", np.not_equal: "ne", np.maximum: "maximum", np.minimum: "minimum",
        np.isnan: "isnar", np.sqrt: "sqrt", np.matmul: "matmul",
        # stage 7: elementary functions (positnp.funcs)
        np.exp: "f:exp", np.exp2: "f:exp2", np.expm1: "f:expm1", np.log: "f:log", np.log2: "f:log2",
        np.log10: "f:log10", np.log1p: "f:log1p", np.sin: "f:sin", np.cos: "f:cos", np.tan: "f:tan",
        np.arcsin: "f:arcsin", np.arccos: "f:arccos", np.arctan: "f:arctan", np.sinh: "f:sinh",
        np.cosh: "f:cosh", np.tanh: "f:tanh", np.arcsinh: "f:arcsinh", np.arccosh: "f:arccosh",
        np.arctanh: "f:arctanh", np.cbrt: "f:cbrt",
        # rounding to integers (exact) and powers
        np.floor: "r:floor", np.ceil: "r:ceil", np.trunc: "r:trunc", np.rint: "r:rint",
        np.power: "power", np.reciprocal: "reciprocal",
    }

    _REDUCE = {np.add: "sum", np.multiply: "prod", np.maximum: "max", np.minimum: "min"}

    def __array_ufunc__(self, ufunc, method, *inputs, out=None, **kwargs):
        if method in ("reduce", "accumulate"):
            return self._ufunc_reduce(ufunc, method, inputs, out, kwargs)
        if method != "__call__":
            return NotImplemented
        if kwargs.pop("where", True) is not True or kwargs.pop("casting", "same_kind") != "same_kind" or \
                kwargs.pop("dtype", None) is not None or kwargs.pop("subok", True) is not True or kwargs:
            raise TypeError(f"np.{ufunc.__name__} on posit arrays: where=, dtype=, casting= and the other options "
                            "are not supported")
        return _to_out(self._ufunc_call(ufunc, inputs), out)

    def _ufunc_reduce(self, ufunc, method, inputs, out, kw):
        name = self._REDUCE.get(ufunc)
        if name is None or len(inputs) != 1 or (method == "accumulate" and ufunc is not np.add):
            return NotImplemented
        axis = kw.pop("axis", 0)
        keepdims = kw.pop("keepdims", False) if method == "reduce" else False
        if kw.pop("dtype", None) is not None or kw.pop("where", True) is not True or kw:
            raise TypeError(f"np.{ufunc.__name__}.{method} on posit arrays: axis=, keepdims= and out= only")
        x = inputs[0]
        if method == "accumulate":
            return _to_out(x.cumsum(axis=axis), out)
        return _to_out(getattr(x, name)(axis=axis, keepdims=keepdims), out)

    def _ufunc_call(self, ufunc, inputs):
        if any(_is_complex(x) for x in inputs):
            if ufunc in (np.add, np.subtract, np.multiply, np.true_divide) and len(inputs) == 2:
                name = {np.add: "add", np.subtract: "sub", np.multiply: "mul", np.true_divide: "div"}[ufunc]
                a, b = inputs
                return a._binary(name, b) if isinstance(a, PositArray) else b._rbinary(name, a)
            return NotImplemented
        what = self._UFUNCS.get(ufunc)
        if what is None:
            ext = _npapi().UFUNCS.get(ufunc)
            if ext is not None:
                return ext(*inputs)
            if ufunc.__name__ in _LATER:
                raise TypeError(f"np.{ufunc.__name__} on posit arrays: {_LATER[ufunc.__name__]}")
            return NotImplemented
        if what == "power":
            if not isinstance(inputs[0], PositArray):
                raise TypeError("number ** posit array: the exponent must be a number (x ** p)")
            return inputs[0] ** inputs[1]
        args = _unify(*inputs)
        a = args[0]
        if what == "reciprocal":
            return a ** -1
        if what.startswith("r:"):
            return getattr(a, what[2:])()
        if what in ("add", "sub", "mul", "div"):
            return a._binary(what, args[1])
        if what == "matmul":
            return matmul(a, args[1])
        if what.startswith("f:"):
            return a._func(what[2:])
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


def _sort(a, axis=-1, **kw):
    r = a.copy()
    r.sort(axis=axis)
    return r


def _concatenate(arrays, axis=0, **kw):
    xs = _unify(*arrays)
    return PositArray(np.concatenate([x.bits for x in xs], axis=axis), xs[0].config)


def _stack(arrays, axis=0, **kw):
    xs = _unify(*arrays)
    return PositArray(np.stack([x.bits for x in xs], axis=axis), xs[0].config)


def _where(cond, x, y):
    xv, yv = _unify(x, y)
    return PositArray(np.where(np.asarray(cond), xv.bits, yv.bits), xv.config)


def _var_fn(root):
    def f(a, axis=None, dtype=None, out=None, ddof=0, keepdims=False, **kw):
        _no_out("std" if root else "var", out, dtype, keepdims, **kw)
        return a.std(axis, ddof) if root else a.var(axis, ddof)
    return f


def _clip(a, a_min=None, a_max=None, out=None, *, min=None, max=None, **kw):   # noqa: A002 (NumPy 2.1 names)
    _no_out("clip", out)
    return a.clip(a_min if min is None else min, a_max if max is None else max)


def _round(a, decimals=0, out=None):
    _no_out("round", out)
    return a.round(decimals)


def _cumsum(a, axis=None, dtype=None, out=None):
    _no_out("cumsum", out, dtype)
    return a.cumsum(axis)


def _prod(a, axis=None, dtype=None, out=None, keepdims=False, **kw):
    _no_out("prod", out, dtype, keepdims, **kw)
    return a.prod(axis)


def _np_save(*args, **kw):
    raise TypeError("np.save would drop the posit format (a .npy holds plain integers): "
                    "use pn.save(file, x) and pn.load(file)")


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
    np.convolve: lambda a, v, mode="full": convolve(a, v, mode),
    np.fft.fft: lambda a, n=None, axis=-1, norm=None: _fft("fft", a, n, axis, norm),
    np.fft.ifft: lambda a, n=None, axis=-1, norm=None: _fft("ifft", a, n, axis, norm),
    np.fft.rfft: lambda a, n=None, axis=-1, norm=None: _fft("rfft", a, n, axis, norm),
    np.correlate: lambda a, v, mode="valid": correlate(a, v, mode),
    np.var: _var_fn(False),
    np.std: _var_fn(True),
    np.clip: _clip,
    np.round: _round,
    np.cumsum: _cumsum,
    np.prod: _prod,
    np.save: _np_save,
    np.linalg.solve: lambda a, b: _linalg().solve(a, b),
    np.linalg.inv: lambda a: _linalg().inv(a),
    np.linalg.det: lambda a: _linalg().det(a),
}


if hasattr(np, "around"):
    _FUNCTIONS[np.around] = _round


def _to_out(res, out):
    """Write a result into out= (a PositArray of the right shape, or an ndarray for boolean results)."""
    if out is None or res is NotImplemented:
        return res
    if isinstance(out, tuple):
        if len(out) != 1:
            raise TypeError("one out= array expected")
        out = out[0]
    if isinstance(res, PositArray):
        if not isinstance(out, PositArray):
            raise TypeError("out= of a posit result must be a PositArray")
        out.bits[...] = np.broadcast_to(res.astype(out.config).bits, out.shape)
    else:
        out[...] = res
    return out


def _npapi():
    from . import npapi
    return npapi


def _linalg():
    from . import linalg
    return linalg


def _fft(name, a, n, axis, norm):
    if norm not in (None, "backward"):
        raise TypeError(f"np.fft.{name} on posit arrays: norm={norm!r} not supported (only the default)")
    from . import fft as F
    return getattr(F, name)(a, n, axis)


def _norm(x, ord, axis):
    if ord is not None or axis is not None:
        raise TypeError("np.linalg.norm on posit arrays: only the Euclidean norm of all elements")
    return x.norm()
for _alias in ("amax", "amin"):
    if hasattr(np, _alias) and getattr(np, _alias) not in _FUNCTIONS:
        _FUNCTIONS[getattr(np, _alias)] = _FUNCTIONS[np.max if _alias == "amax" else np.min]


from . import npapi as _npapi_module  # noqa: E402,F401  (registers the rest of the NumPy API)
