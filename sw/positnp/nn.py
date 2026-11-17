"""Neural-network layers on posits (posit8 / posit16 on the kv260-ml bitstream, any format elsewhere).

    y = pn.nn.linear(x, W, b)                 # x @ W.T + b, the bias inside the quire: one rounding per output
    y = pn.nn.conv2d(x, w, b, stride=1, padding=1)   # NCHW, every output one exact dot (im2col + matmul)
    pn.nn.relu(y), pn.nn.softmax(y, axis=-1), pn.nn.max_pool2d(y, 2), pn.nn.layer_norm(y)

linear and conv2d are single matmuls (on a KV260 engine with gemv: P outputs per clock): the bias is one more
column of ones in the input, so it is added inside the exact sum instead of after a rounding. The activations
use the engine's function tables for posit8 / posit16 (exp, tanh, sigmoid: correctly rounded).
"""

from __future__ import annotations

import importlib
import math

import numpy as np


def _arr():
    return importlib.import_module(".array", __package__)


def linear(x, W, b=None):
    """x @ W.T + b (x: (..., in), W: (out, in), b: (out,)), every output the exact sum rounded once."""
    A = _arr()
    ops = A._unify(x, W) if b is None else A._unify(x, W, b)
    X, Wt = ops[0], ops[1]
    if b is None:
        return X @ Wt.T
    Bv = ops[2]
    c = X.config
    ones = A.PositArray(np.full(X.shape[:-1] + (1,), A._one_bits(c), np.int64), c)
    X1 = np.concatenate([X, ones], axis=-1)
    W1 = np.concatenate([Wt, Bv.reshape(-1, 1)], axis=1)
    return X1 @ W1.T


def conv2d(x, w, b=None, stride: int = 1, padding: int = 0):
    """2-D convolution as in deep learning (a cross-correlation), NCHW: x (N, C, H, W), w (O, C, kh, kw),
    b (O,). Zero padding, an integer stride. Every output the exact sum of C kh kw products (+ bias), rounded once."""
    A = _arr()
    ops = A._unify(x, w) if b is None else A._unify(x, w, b)
    X, Wt = ops[0], ops[1]
    if X.ndim != 4 or Wt.ndim != 4 or X.shape[1] != Wt.shape[1]:
        raise ValueError(f"conv2d: x (N, C, H, W) and w (O, C, kh, kw), got {X.shape} and {Wt.shape}")
    c = X.config
    N, C, H, Wd = X.shape
    O, _, kh, kw = Wt.shape
    p, s = int(padding), int(stride)
    xp = np.pad(X.bits.astype(np.int64), ((0, 0), (0, 0), (p, p), (p, p)))
    win = np.lib.stride_tricks.sliding_window_view(xp, (kh, kw), axis=(2, 3))[:, :, ::s, ::s]   # N C OH OW kh kw
    oh, ow = win.shape[2], win.shape[3]
    cols = np.ascontiguousarray(win.transpose(0, 2, 3, 1, 4, 5)).reshape(N * oh * ow, C * kh * kw)
    Wm = Wt.reshape(O, C * kh * kw)
    if b is not None:
        cols = np.concatenate([cols, np.full((cols.shape[0], 1), A._one_bits(c), np.int64)], axis=1)
        Wm = np.concatenate([Wm, ops[2].reshape(O, 1)], axis=1)
    Y = A.PositArray(cols, c) @ Wm.T                                    # (N OH OW, O)
    return A.PositArray(np.ascontiguousarray(Y.bits.reshape(N, oh, ow, O).transpose(0, 3, 1, 2)), c)


def relu(x):
    """max(x, 0); NaR stays NaR."""
    A = _arr()
    x = x if isinstance(x, A.PositArray) else A.array(x)
    return A.PositArray(np.where(x._signed() > 0, x.bits, np.where(x.isnar(), x.bits, 0)), x.config)


def softmax(x, axis: int = -1):
    e = np.exp(x - np.max(x, axis=axis, keepdims=True))
    return e / np.sum(e, axis=axis, keepdims=True)


def log_softmax(x, axis: int = -1):
    z = x - np.max(x, axis=axis, keepdims=True)
    return z - np.log(np.sum(np.exp(z), axis=axis, keepdims=True))


def sigmoid(x):
    return _arr().func("sigmoid", x)


def tanh(x):
    return np.tanh(x)


def gelu(x):
    """The tanh approximation 0.5 x (1 + tanh(sqrt(2 / pi) (x + 0.044715 x**3)))."""
    return 0.5 * x * (1 + np.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * x ** 3)))


def layer_norm(x, axis: int = -1, eps: float = 1e-5):
    """(x - mean) / sqrt(var + eps) along `axis`; mean and var exact from the quire, rounded once each."""
    return (x - np.mean(x, axis=axis, keepdims=True)) / np.sqrt(np.var(x, axis=axis, keepdims=True) + eps)


def max_pool2d(x, kernel: int, stride: int = None):
    """Max over kernel x kernel windows of NCHW (on the bits: exact)."""
    A = _arr()
    s = int(stride or kernel)
    k = int(kernel)
    win = np.lib.stride_tricks.sliding_window_view(x._signed(), (k, k), axis=(2, 3))[:, :, ::s, ::s]
    return x._from_signed(win.max(axis=(-2, -1)))


__all__ = ["linear", "conv2d", "relu", "softmax", "log_softmax", "sigmoid", "tanh", "gelu", "layer_norm",
           "max_pool2d"]
