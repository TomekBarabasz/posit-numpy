"""Signal processing on posits (scipy.signal-like): IIR filters and 2-D convolution.

    y = pn.signal.lfilter(b, a, x)            # every output: one exact sum in the quire, rounded once
    pn.signal.convolve2d(img, k, mode="same") # every output: an exact dot product (one matmul, gemv on a KV260)

lfilter runs sample by sample in the PS (a recursion cannot be streamed through the FIR engine), but each
output is exact before its single rounding: y[n] = round((sum_k b[k] x[n-k] - sum_{k>=1} a[k] y[n-k]) / a[0])
(a[0] = 1: just the sum; otherwise the rounded sum divided by a[0]). All rows of a batch go through one
vecops.fused_dot call per sample.
"""

from __future__ import annotations

import importlib

import numpy as np

from . import vecops


def _arr():
    return importlib.import_module(".array", __package__)


def lfilter(b, a, x, axis: int = -1, zi=None):
    """IIR / FIR filter along `axis` like scipy.signal.lfilter (no zi: zero initial state)."""
    A = _arr()
    if zi is not None:
        raise TypeError("pn.signal.lfilter: zi= (initial state) is not supported")
    B, Ac, X = A._unify(b, a, x)
    if B.ndim != 1 or Ac.ndim != 1 or not B.size or not Ac.size:
        raise ValueError("lfilter: b and a are non-empty 1-D arrays")
    c = X.config
    a0 = int(Ac.bits[0])
    if a0 == 0:
        raise ValueError("lfilter: a[0] must not be 0")
    one = A._one_bits(c)
    xb = np.moveaxis(X.bits.astype(np.int64), axis, -1)
    lead, N = xb.shape[:-1], xb.shape[-1]
    rows = xb.reshape(-1, N)
    R = rows.shape[0]
    bb = B.bits.astype(np.int64)
    na = vecops.neg(c, Ac.bits[1:].astype(np.int64)).astype(np.int64)     # -a[1:], exact
    y = np.zeros((R, N), np.int64)
    for n in range(N):
        kb = min(len(bb), n + 1)
        ka = min(len(na), n)
        ta = [rows[:, n - k] for k in range(kb)] + [y[:, n - 1 - k] for k in range(ka)]
        tb = [np.full(R, bb[k]) for k in range(kb)] + [np.full(R, na[k]) for k in range(ka)]
        s = vecops.fused_dot(c, np.array(ta), np.array(tb)).bits.astype(np.int64)
        if a0 != one:
            s = vecops.op(c, "div", s, np.full(R, a0)).bits.astype(np.int64)
        y[:, n] = s
    return A.PositArray(np.moveaxis(y.reshape(lead + (N,)), -1, axis), c)


def _windows(Xb, kh, kw):
    return np.lib.stride_tricks.sliding_window_view(Xb, (kh, kw), axis=(-2, -1))


def _corr2d_full(X, K):
    """Full 2-D correlation (zero padding), every output one exact dot of kh * kw products (a matmul)."""
    A = _arr()
    kh, kw = K.shape
    Xp = np.pad(X.bits.astype(np.int64), ((kh - 1, kh - 1), (kw - 1, kw - 1)))
    win = _windows(Xp, kh, kw)
    oh, ow = win.shape[:2]
    cols = A.PositArray(np.ascontiguousarray(win).reshape(oh * ow, kh * kw), X.config)
    return (cols @ K.reshape(kh * kw, 1)).reshape(oh, ow)


def correlate2d(in1, in2, mode: str = "full"):
    """scipy.signal.correlate2d for real 2-D posit arrays (zero boundary): c[i, j] = sum x[i + m, j + n] k[m, n]."""
    A = _arr()
    X, K = A._unify(in1, in2)
    if X.ndim != 2 or K.ndim != 2:
        raise ValueError("correlate2d: 2-D arrays")
    full = _corr2d_full(X, K)
    return _mode2d(full, X.shape, K.shape, mode)


def convolve2d(in1, in2, mode: str = "full"):
    """scipy.signal.convolve2d for real 2-D posit arrays (zero boundary): the correlation with the kernel turned."""
    A = _arr()
    X, K = A._unify(in1, in2)
    if X.ndim != 2 or K.ndim != 2:
        raise ValueError("convolve2d: 2-D arrays")
    full = _corr2d_full(X, A.PositArray(K.bits[::-1, ::-1].copy(), K.config))
    return _mode2d(full, X.shape, K.shape, mode)


def _mode2d(full, xs, ks, mode):
    (H, W), (kh, kw) = xs, ks
    if mode == "full":
        return full
    if mode == "valid":
        if H < kh or W < kw:
            raise ValueError("valid mode: the kernel must fit in the input")
        return full[kh - 1:H, kw - 1:W]
    if mode == "same":
        r0, c0 = (kh - 1) // 2, (kw - 1) // 2
        return full[r0:r0 + H, c0:c0 + W]
    raise ValueError(f"mode {mode!r}: 'full', 'same' or 'valid'")


def convolve(in1, in2, mode: str = "full"):
    """1-D: np.convolve (exact per output); 2-D: convolve2d."""
    A = _arr()
    if np.ndim(getattr(in1, "bits", in1)) == 2:
        return convolve2d(in1, in2, mode)
    return A.convolve(in1, in2, mode)


def correlate(in1, in2, mode: str = "full"):
    """1-D: np.correlate; 2-D: correlate2d (scipy's default mode 'full')."""
    A = _arr()
    if np.ndim(getattr(in1, "bits", in1)) == 2:
        return correlate2d(in1, in2, mode)
    return A.correlate(in1, in2, mode)


__all__ = ["lfilter", "convolve2d", "correlate2d", "convolve", "correlate"]
