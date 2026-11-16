"""FFT on posits (plan, stage 7): pn.fft.fft / ifft / rfft, np.fft.fft on PositArray.

Built from what the engine already does, no FFT core:

- DFT blocks of size R through matmul: every output of a block is the exact quire sum
  sum_n x[n] w^(nk) over the posit-rounded twiddles, rounded once. Complex data in real form:
  a row [re(R), im(R)] @ M (2R x 2R) = [Re X, Im X], M = [[C, -S], [S, C]] (C = cos, S = sin of 2 pi nk / R).
  Rows are the signals (or the columns of the four-step matrix) and M sits in the engine's x buffers,
  so on the KV260 a pass of A gives P = 4 columns: ~4 multiply-adds per clock with few commands.
- Large N: four-step Cooley-Tukey, N = N1 * N2 (N1 <= radix): N2 DFTs of size N1, twiddles
  W_N^(n2 k1), N1 DFTs of size N2 (recursively), with transposes on the host.
- Twiddles: complex multiply as fma(xr, wr, -(xi * wi)) and fma(xr, wi, xi * wr) - FMA3 on the engine,
  two roundings per component (the product, then the fused multiply-add).

So an output of the default transform has 1 rounding per DFT level plus 2 per twiddle level
(log_64 N levels: 2 for N <= 4096, 3 for N <= 262144). exact=True: one direct DFT (N <= 2048 on the
engine, gemv K = 2N <= 4096): every output the correctly rounded value of the exact sum - no other
error than the rounding of the twiddles themselves.

    X = pn.fft.fft(x)                 # PositComplex: X.real, X.imag (PositArray), X.to_complex()
    re, im = pn.fft.fft(x)            # unpacks
    x2 = pn.fft.ifft(X)               # 1/N: one more rounding (division)
    np.fft.fft(x)                     # the same, via __array_function__
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from . import vecops
from .ref import PositConfig

RADIX = 64            # largest DFT block of the four-step algorithm


def _arr():
    import importlib
    return importlib.import_module(".array", __package__)   # the module (positnp.array is the function)
EXACT_MAX = 2048      # direct DFT: K = 2N must fit the engine's x buffer (gemv_max_k = 4096)


@dataclass
class PositComplex:
    """A complex posit array: real and imaginary parts, both PositArray of the same shape and format."""
    real: "object"
    imag: "object"

    def __iter__(self):
        return iter((self.real, self.imag))

    @property
    def shape(self):
        return self.real.shape

    @property
    def config(self) -> PositConfig:
        return self.real.config

    def to_complex(self) -> np.ndarray:
        return self.real.to_float() + 1j * self.imag.to_float()

    def conj(self) -> "PositComplex":
        return PositComplex(self.real, -self.imag)

    def __repr__(self) -> str:
        return f"PositComplex({self.config}, shape={self.shape})"


# ---- constants ---------------------------------------------------------------------------------------

def _round(c: PositConfig, v: np.ndarray) -> np.ndarray:
    return vecops.from_float(c, v).bits.astype(np.int64)


@functools.lru_cache(maxsize=64)
def _dft_matrix(c: PositConfig, R: int) -> np.ndarray:
    """Real form of the forward DFT of size R for row vectors: [re, im] @ M = [Re X, Im X] (bits, 2R x 2R)."""
    nk = np.outer(np.arange(R), np.arange(R)) % R                 # exact angles: 2 pi (nk mod R) / R
    th = 2 * np.pi * nk / R
    cs, sn = _exact_trig(c, np.cos(th), nk, R, "cos"), _exact_trig(c, np.sin(th), nk, R, "sin")
    neg = lambda b: (-b) & c.mask                                  # noqa: E731
    M = np.empty((2 * R, 2 * R), np.int64)
    M[:R, :R] = cs                    # re -> Re:  cos
    M[R:, :R] = sn                    # im -> Re:  +sin   ((xr + i xi)(cos - i sin))
    M[:R, R:] = neg(sn)               # re -> Im:  -sin
    M[R:, R:] = cs                    # im -> Im:  cos
    M.flags.writeable = False
    return M


def _exact_trig(c: PositConfig, v: np.ndarray, nk: np.ndarray, R: int, which: str) -> np.ndarray:
    """cos / sin of 2 pi nk / R rounded to posits; exact 0 and +-1 where they are (float64 gives 6e-17)."""
    q4 = (4 * nk) % (4 * R)                                        # multiples of a quarter turn: 4nk / R integer
    on_axis = (4 * nk) % R == 0
    quarter = (4 * nk) // R % 4
    exact = {"cos": np.array([1.0, 0.0, -1.0, 0.0]), "sin": np.array([0.0, 1.0, 0.0, -1.0])}[which]
    v = np.where(on_axis, exact[quarter], v)
    del q4
    return _round(c, v)


@functools.lru_cache(maxsize=64)
def _twiddles(c: PositConfig, N1: int, N2: int) -> Tuple[np.ndarray, np.ndarray]:
    """W_N^(n2 k1), N = N1 N2, as (re, im) bits of shape (N2, N1)."""
    N = N1 * N2
    nk = np.outer(np.arange(N2), np.arange(N1)) % N
    th = 2 * np.pi * nk / N
    re = _exact_trig(c, np.cos(th), nk, N, "cos")
    im = (-_exact_trig(c, np.sin(th), nk, N, "sin")) & c.mask     # e^{-i th}
    return re, im


def _factor(N: int, radix: int) -> int:
    """Largest divisor of N that is <= radix and > 1 (N itself when N <= radix); 0 when there is none."""
    if N <= radix:
        return N
    for r in range(radix, 1, -1):
        if N % r == 0:
            return r
    return 0


# ---- the transform on bits (rows) --------------------------------------------------------------------

class _Ctx:
    def __init__(self, c: PositConfig):
        A = _arr()
        self.c = c
        self.A = A
        self.be = A.get_backend(c)
        self.dt = A.bits_dtype(c)
        mm = getattr(self.be, "matmul", None)
        self.matmul = mm if mm is not None else A._sw.setdefault(c, A.PositSw(c)).matmul

    def dft_rows(self, re: np.ndarray, im: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Exact DFT of every row (B, R): one matmul, one rounding per output."""
        B, R = re.shape
        D = np.concatenate([re, im], axis=1).astype(self.dt)
        Y = np.asarray(self.matmul(D, _dft_matrix(self.c, R).astype(self.dt)))
        return Y[:, :R], Y[:, R:]

    def cmul(self, re, im, wre, wim):
        """(re + i im) * (wre + i wim), elementwise: two fused multiply-adds (FMA3 on the engine)."""
        A, c = self.A, self.c
        xr, xi = A.PositArray._wrap(re.astype(self.dt).ravel(), c), A.PositArray._wrap(im.astype(self.dt).ravel(), c)
        wr = A.PositArray._wrap(np.ascontiguousarray(wre, self.dt).ravel(), c)
        wi = A.PositArray._wrap(np.ascontiguousarray(wim, self.dt).ravel(), c)
        nr = A.fma(xr, wr, -(xi * wi))
        ni = A.fma(xr, wi, xi * wr)
        return nr.bits.reshape(re.shape), ni.bits.reshape(re.shape)

    def fft_rows(self, re: np.ndarray, im: np.ndarray, radix: int) -> Tuple[np.ndarray, np.ndarray]:
        B, N = re.shape
        N1 = _factor(N, radix)
        if N1 == N:
            return self.dft_rows(re, im)
        if N1 == 0:
            if N <= EXACT_MAX:
                return self.dft_rows(re, im)                       # prime-ish N: one direct DFT
            raise ValueError(f"fft of length {N}: no factor <= {radix} and too long for a direct DFT "
                             f"(<= {EXACT_MAX}); use a length with small prime factors")
        N2 = N // N1
        # x[b, N2 n1 + n2]: rows (b, n2), DFT over n1
        tr = lambda a, s: a.reshape(s).transpose(0, 2, 1).reshape(-1, s[1])   # noqa: E731
        zr, zi = self.dft_rows(tr(re, (B, N1, N2)), tr(im, (B, N1, N2)))      # (B N2, N1): Z[b, n2, k1]
        wr, wi = _twiddles(self.c, N1, N2)
        zr, zi = self.cmul(zr.reshape(B, N2, N1), zi.reshape(B, N2, N1),
                           np.broadcast_to(wr, (B, N2, N1)), np.broadcast_to(wi, (B, N2, N1)))
        # rows (b, k1), transform over n2 (recursively)
        yr, yi = self.fft_rows(tr(zr, (B, N2, N1)), tr(zi, (B, N2, N1)), radix)   # (B N1, N2): Y[b, k1, k2]
        # X[k1 + N1 k2] = Y[k1, k2]
        back = lambda a: a.reshape(B, N1, N2).transpose(0, 2, 1).reshape(B, N)   # noqa: E731
        return back(yr), back(yi)


def _as_complex(x, config: Optional[PositConfig]):
    A = _arr()
    if isinstance(x, PositComplex):
        return x.real, x.imag
    if isinstance(x, tuple) and len(x) == 2:
        re = x[0] if isinstance(x[0], A.PositArray) else A.array(x[0], config)
        return re, re._coerce(x[1])
    if isinstance(x, A.PositArray):                              # real input: zero imaginary bits, no conversion
        return x, A.PositArray._wrap(np.zeros(x.shape, A.bits_dtype(x.config)), x.config)
    arr = np.asarray(x)
    if np.iscomplexobj(arr):
        re = A.array(arr.real, config)
        return re, A.array(arr.imag, re.config)
    re = A.array(arr, config)
    return re, A.zeros(re.shape, re.config)


def _transform(x, n, axis, inverse: bool, exact: bool, radix: int, config, method: str = "auto") -> PositComplex:
    A = _arr()
    re, im = _as_complex(x, config)
    c = re.config
    rb = np.moveaxis(re.bits, axis, -1)
    ib = np.moveaxis(im.bits, axis, -1)
    N = rb.shape[-1] if n is None else n
    if N < 1:
        raise ValueError(f"fft length {N}")
    if rb.shape[-1] != N:                                       # crop or zero-pad like numpy
        pad = [(0, 0)] * (rb.ndim - 1) + [(0, max(0, N - rb.shape[-1]))]
        rb = np.pad(rb[..., :N], pad)
        ib = np.pad(ib[..., :N], pad)
    lead = rb.shape[:-1]
    R2, I2 = rb.reshape(-1, N), ib.reshape(-1, N)
    if inverse:                                                 # ifft(x) = conj(fft(conj(x))) / N
        I2 = (-I2.astype(np.int64)) & c.mask
    ctx = _Ctx(c)
    if exact:
        method = "exact"
    if method not in ("auto", "core", "blocks", "exact"):
        raise ValueError(f"method {method!r}: 'auto', 'core', 'blocks' or 'exact'")
    S = (N.bit_length() - 1) // 2
    core_ok = N >= 16 and _is_pow4(N)
    if method == "auto":                                        # the hardware core when the bitstream has it
        method = "core" if core_ok and getattr(ctx.be, "fft_max_s", 0) >= S else "blocks"
    if method == "exact":
        if N > EXACT_MAX:
            raise ValueError(f"exact=True: direct DFT up to {EXACT_MAX} points (got {N})")
        yr, yi = ctx.dft_rows(R2, I2)
    elif method == "core":
        if not core_ok:
            raise ValueError(f"method='core': N = 4^S >= 16 (got {N})")
        fc = getattr(ctx.be, "fft_core", None) or ctx.A._sw.setdefault(c, ctx.A.PositSw(c)).fft_core
        yr, yi = fc(R2.astype(ctx.dt, copy=False), I2.astype(ctx.dt, copy=False))
    else:
        yr, yi = ctx.fft_rows(R2, I2, radix)
    if inverse:
        yi = (-np.asarray(yi).astype(np.int64)) & c.mask
    Yr = A.PositArray._wrap(np.ascontiguousarray(np.asarray(yr).reshape(lead + (N,))).astype(ctx.dt, copy=False), c)
    Yi = A.PositArray._wrap(np.ascontiguousarray(np.asarray(yi).reshape(lead + (N,))).astype(ctx.dt, copy=False), c)
    if inverse:
        Yr, Yi = Yr / N, Yi / N
    if axis not in (-1, rb.ndim - 1):
        Yr = A.PositArray._wrap(np.ascontiguousarray(np.moveaxis(Yr.bits, -1, axis)), c)
        Yi = A.PositArray._wrap(np.ascontiguousarray(np.moveaxis(Yi.bits, -1, axis)), c)
    return PositComplex(Yr, Yi)


def fft(x, n: Optional[int] = None, axis: int = -1, *, exact: bool = False, radix: int = RADIX,
        config: Optional[PositConfig] = None, method: str = "auto") -> PositComplex:
    """Discrete Fourier transform along `axis`: X[k] = sum_n x[n] e^(-2 pi i nk / N).

    x: PositArray (real), PositComplex, a (re, im) pair, or anything pn.array accepts (complex too).
    exact=True: one direct DFT (N <= 2048), every output rounded once from the exact sum.
    method: "auto" (the engine's FFT core for N = 4^S when the bitstream has one, else DFT blocks),
    "core" (the core's radix-4 transform, in software when there is no core), "blocks", "exact".
    """
    return _transform(x, n, axis, False, exact, radix, config, method)


def ifft(x, n: Optional[int] = None, axis: int = -1, *, exact: bool = False, radix: int = RADIX,
         config: Optional[PositConfig] = None, method: str = "auto") -> PositComplex:
    """Inverse DFT: x[n] = (1 / N) sum_k X[k] e^(+2 pi i nk / N); the 1 / N is one more rounding."""
    return _transform(x, n, axis, True, exact, radix, config, method)


def rfft(x, n: Optional[int] = None, axis: int = -1, *, exact: bool = False, radix: int = RADIX,
         config: Optional[PositConfig] = None, method: str = "auto") -> PositComplex:
    """DFT of a real signal, the N // 2 + 1 non-negative frequencies (like np.fft.rfft)."""
    X = _transform(x, n, axis, False, exact, radix, config, method)
    A = _arr()
    N = X.real.shape[axis]
    k = N // 2 + 1
    sl = [slice(None)] * X.real.ndim
    sl[axis] = slice(0, k)
    take = lambda a: A.PositArray._wrap(np.ascontiguousarray(a.bits[tuple(sl)]), a.config)   # noqa: E731
    return PositComplex(take(X.real), take(X.imag))


# ---- model of the hardware FFT core (PositVecAxi 5.5, PositFftCore) ---------------------------------
#
# Radix-4 decimation in time, N = 4^S (16 .. 4^maxS): base-4 digit-reversed load, S in-place stages,
# natural-order output. Stage s (Q = 4^s, sub-transform L = 4Q): butterflies on y_p = x[g L + k + p Q],
# twiddles w_p = W_N^(p k N / L) from one table T[e] = W_N^e, e < 3N/4 (loaded by Python, posit-rounded).
# A butterfly is the classic one, every operation rounded (the core is PositMul / PositAdd in a pipeline):
#   P0 = y0, P_p = w_p y_p:  re = wr yr - wi yi,  im = wr yi + wi yr       (p = 1, 2, 3)
#   s0 = P0 + P2, d0 = P0 - P2, s1 = P1 + P3, d1 = P1 - P3
#   X0 = s0 + s1, X2 = s0 - s1, X1 = (d0r + d1i, d0i - d1r), X3 = (d0r - d1i, d0i + d1r)
# fft_fused_model: the same transform with every butterfly output rounded once (reference for accuracy).

def fft_twiddle_table(c: PositConfig, N: int) -> Tuple[np.ndarray, np.ndarray]:
    """(re, im) bits of W_N^e = e^(-2 pi i e / N) for e = 0 .. 3N/4 - 1 (the core's twiddle table)."""
    e = np.arange(3 * N // 4)
    th = 2 * np.pi * e / N
    re = _exact_trig(c, np.cos(th), e, N, "cos")
    im = (-_exact_trig(c, np.sin(th), e, N, "sin")) & c.mask
    return re, im


def _rot(wr, wi, m, mask):
    """(wr + i wi) * (-i)^m on bits: exact (swap / negate)."""
    neg = lambda b: (-b) & mask                                     # noqa: E731
    return [(wr, wi), (wi, neg(wr)), (neg(wr), neg(wi)), (neg(wi), wr)][m % 4]


def _is_pow4(n: int) -> bool:
    return n >= 1 and n & (n - 1) == 0 and (n.bit_length() - 1) % 2 == 0


def _core_rows(c, re, im, N_top, table, fused):
    """Radix-4 DIT of every row of (B, L) bits, L | N_top, twiddles from the N_top table."""
    B, L = re.shape
    if L == 1:
        return re, im
    M = L // 4
    # the 4 subsequences x[p::4] of every row as one batch (B * 4 rows): one recursive call per level
    sub = lambda a: a.reshape(B, M, 4).transpose(0, 2, 1).reshape(B * 4, M)   # noqa: E731
    zr, zi = _core_rows(c, sub(re), sub(im), N_top, table, fused)
    zr, zi = zr.reshape(B, 4, M), zi.reshape(B, 4, M)
    ys = [(zr[:, p], zi[:, p]) for p in range(4)]
    k = np.arange(M)
    tr, ti = table
    out_r = np.empty((B, L), np.int64)
    out_i = np.empty((B, L), np.int64)
    if fused:
        for q in range(4):
            ar, br, ai, bi = [], [], [], []
            for p in range(4):
                e = p * k * (N_top // L)
                cr, ci = _rot(tr[e], ti[e], p * q, c.mask)
                yr, yi = ys[p]
                cr_b, ci_b = np.broadcast_to(cr, (B, M)), np.broadcast_to(ci, (B, M))
                ar += [yr, yi]; br += [cr_b, (-ci_b) & c.mask]
                ai += [yi, yr]; bi += [cr_b, ci_b]
            st = lambda ts: np.stack([t.ravel() for t in ts])          # noqa: E731
            out_r[:, q * M:(q + 1) * M] = vecops.fused_dot(c, st(ar), st(br)).bits.reshape(B, M)
            out_i[:, q * M:(q + 1) * M] = vecops.fused_dot(c, st(ai), st(bi)).bits.reshape(B, M)
        return out_r, out_i
    op = lambda name, a, b: vecops.op(c, name, a, b).bits.astype(np.int64)   # noqa: E731
    P = [ys[0]]
    for p in range(1, 4):
        e = p * k * (N_top // L)
        wr, wi = np.broadcast_to(tr[e], (B, M)), np.broadcast_to(ti[e], (B, M))
        yr, yi = ys[p]
        P.append((op("sub", op("mul", wr, yr), op("mul", wi, yi)), op("add", op("mul", wr, yi), op("mul", wi, yr))))
    (p0r, p0i), (p1r, p1i), (p2r, p2i), (p3r, p3i) = P
    s0r, s0i, d0r, d0i = op("add", p0r, p2r), op("add", p0i, p2i), op("sub", p0r, p2r), op("sub", p0i, p2i)
    s1r, s1i, d1r, d1i = op("add", p1r, p3r), op("add", p1i, p3i), op("sub", p1r, p3r), op("sub", p1i, p3i)
    out_r[:, 0:M], out_i[:, 0:M] = op("add", s0r, s1r), op("add", s0i, s1i)
    out_r[:, 2 * M:3 * M], out_i[:, 2 * M:3 * M] = op("sub", s0r, s1r), op("sub", s0i, s1i)
    out_r[:, M:2 * M], out_i[:, M:2 * M] = op("add", d0r, d1i), op("sub", d0i, d1r)
    out_r[:, 3 * M:], out_i[:, 3 * M:] = op("sub", d0r, d1i), op("add", d0i, d1r)
    return out_r, out_i


def _core(x, config, fused) -> PositComplex:
    A = _arr()
    re, im = _as_complex(x, config)
    c = re.config
    N = re.shape[-1]
    if not _is_pow4(N) or N < 4:
        raise ValueError(f"the FFT core takes N = 4^S (got {N})")
    lead = re.shape[:-1]
    yr, yi = _core_rows(c, re.bits.reshape(-1, N).astype(np.int64), im.bits.reshape(-1, N).astype(np.int64),
                        N, fft_twiddle_table(c, N), fused)
    dt = A.bits_dtype(c)
    return PositComplex(A.PositArray._wrap(yr.astype(dt).reshape(lead + (N,)), c),
                        A.PositArray._wrap(yi.astype(dt).reshape(lead + (N,)), c))


def fft_core_model(x, config: Optional[PositConfig] = None) -> PositComplex:
    """Bit-exact model of the hardware FFT core along the last axis (N = 4^S, classic radix-4 butterflies)."""
    return _core(x, config, fused=False)


def fft_fused_model(x, config: Optional[PositConfig] = None) -> PositComplex:
    """The core's radix-4 transform with every butterfly output rounded once (accuracy reference only)."""
    return _core(x, config, fused=True)


def levels(N: int, radix: int = RADIX) -> int:
    """DFT levels of the four-step transform of length N (twiddle levels = levels - 1)."""
    n = 0
    while True:
        n += 1
        f = _factor(N, radix)
        if f in (0, N):
            return n
        N //= f


__all__ = ["PositComplex", "fft", "ifft", "rfft", "fft_core_model", "fft_fused_model", "fft_twiddle_table",
           "levels", "RADIX", "EXACT_MAX"]
