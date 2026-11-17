"""Linear algebra on posits: solve, inv, det (also np.linalg.solve / inv / det on PositArray).

LU with partial pivoting, every element from the quire (Crout order, column k of L and row k of U per step):

    U[k, j] = round(A[k, j] - sum_{i<k} L[k, i] U[i, j])             one rounding of an exact sum
    L[i, k] = round(A[i, k] - sum_{j<k} L[i, j] U[j, k]) / U[k, k]     exact sum rounded, then a division

The pivot is the largest |candidate| of the column. The substitutions work the same way (an exact sum of
products rounded once, then a division by the diagonal). A float LU rounds after every multiply-add;
here each element has one or two roundings however long its sum is.

refine (default 1): iterative refinement with the residual r = b - A x computed in the quire (each element
exact, rounded once), the correction solved with the same LU and added to x. One step usually brings x to
the posit's own precision for a reasonably conditioned A.

All of it runs in the PS on vecops (fused_dot: many exact short dot products at once), the same bits on
every backend; the engine's gemv is not used yet.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np

from . import vecops
from .ref import PositConfig

LinAlgError = np.linalg.LinAlgError
CHUNK = 1 << 21                  # max T * n per fused_dot call (memory of the limb sums)


def _arr():
    import importlib
    return importlib.import_module(".array", __package__)


def _one(c: PositConfig) -> int:
    return int(vecops.from_float(c, 1.0).bits)


def _dot(c: PositConfig, ta: List[np.ndarray], tb: List[np.ndarray]) -> np.ndarray:
    """out[i] = round(sum_t ta[t][i] * tb[t][i]) for equal-length 1-D bit arrays, exact sums, one rounding each."""
    n = len(ta[0])
    if n == 0:
        return np.zeros(0, np.int64)
    a = np.array([np.broadcast_to(t, (n,)) for t in ta], np.int64)
    b = np.array([np.broadcast_to(t, (n,)) for t in tb], np.int64)
    step = max(1, CHUNK // len(ta))
    out = np.empty(n, np.int64)
    for s in range(0, n, step):
        out[s:s + step] = vecops.fused_dot(c, a[:, s:s + step], b[:, s:s + step]).bits
    return out


def _div(c: PositConfig, a: np.ndarray, d: int) -> np.ndarray:
    return vecops.op(c, "div", a, np.full(np.shape(a), d, np.int64)).bits.astype(np.int64)


def lu(c: PositConfig, A: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """P A = L U on posit bits (n x n): (L with a unit diagonal, U, perm, number of row swaps).
    A zero pivot: LinAlgError("Singular matrix"), as in NumPy."""
    n = A.shape[0]
    A = A.astype(np.int64).copy()
    L = np.zeros((n, n), np.int64)
    U = np.zeros((n, n), np.int64)
    perm = np.arange(n)
    one = _one(c)
    neg = lambda b: vecops.neg(c, b).astype(np.int64)      # noqa: E731  (exact)
    swaps = 0
    for k in range(n):
        m = n - k
        v = _dot(c, [A[k:, k]] + [L[k:, j] for j in range(k)],
                 [np.full(m, one)] + [np.full(m, neg(U[j, k])) for j in range(k)])
        p = int(np.argmax(vecops.to_signed(c, vecops.absolute(c, v))))
        if v[p] == 0:
            raise LinAlgError("Singular matrix")
        if p:
            for M in (A, L):
                M[[k, k + p]] = M[[k + p, k]]
            perm[[k, k + p]] = perm[[k + p, k]]
            v[[0, p]] = v[[p, 0]]
            swaps += 1
        U[k, k] = v[0]
        L[k, k] = one
        L[k + 1:, k] = _div(c, v[1:], int(v[0]))
        if k + 1 < n:
            cols = n - k - 1
            U[k, k + 1:] = _dot(c, [A[k, k + 1:]] + [np.full(cols, L[k, i]) for i in range(k)],
                                [np.full(cols, one)] + [neg(U[i, k + 1:]) for i in range(k)])
    return L, U, perm, swaps


def _lu_solve(c: PositConfig, L, U, perm, B: np.ndarray) -> np.ndarray:
    """X with L U X = P B (B: n x r bits)."""
    n, r = B.shape
    one = _one(c)
    neg = lambda b: vecops.neg(c, b).astype(np.int64)      # noqa: E731
    PB = B[perm].astype(np.int64)
    Y = np.zeros((n, r), np.int64)
    for i in range(n):
        Y[i] = _dot(c, [PB[i]] + [np.full(r, L[i, j]) for j in range(i)],
                    [np.full(r, one)] + [neg(Y[j]) for j in range(i)])
    X = np.zeros((n, r), np.int64)
    for i in reversed(range(n)):
        s = _dot(c, [Y[i]] + [np.full(r, U[i, j]) for j in range(i + 1, n)],
                 [np.full(r, one)] + [neg(X[j]) for j in range(i + 1, n)])
        X[i] = _div(c, s, int(U[i, i]))
    return X


def _residual(c: PositConfig, A: np.ndarray, X: np.ndarray, B: np.ndarray) -> np.ndarray:
    """B - A X, every element the exact sum rounded once."""
    n, r = B.shape
    one = _one(c)
    negX = vecops.neg(c, X).astype(np.int64)
    ta = [B.astype(np.int64).ravel()] + [np.repeat(A[:, j].astype(np.int64), r) for j in range(n)]
    tb = [np.full(n * r, one)] + [np.tile(negX[j], n) for j in range(n)]
    return _dot(c, ta, tb).reshape(n, r)


def _square(a):
    A = a if isinstance(a, _arr().PositArray) else _arr().array(a)
    if A.ndim != 2 or A.shape[0] != A.shape[1]:
        raise LinAlgError(f"a square matrix expected, got shape {A.shape}")
    return A


def solve(a, b, refine: int = 1):
    """x with a @ x = b (b: a vector or a matrix of right-hand sides), see the module docstring."""
    Am = _arr()
    A, B = Am._unify(a, b)
    A = _square(A)
    c, n = A.config, A.shape[0]
    if B.shape[0] != n or B.ndim not in (1, 2):
        raise ValueError(f"solve: b of shape {B.shape} does not match a of shape {A.shape}")
    Bb = B.bits.reshape(n, -1).astype(np.int64)
    if A.isnar().any() or B.isnar().any():
        return Am.nar(B.shape, c)
    L, U, perm, _ = lu(c, A.bits)
    X = _lu_solve(c, L, U, perm, Bb)
    for _ in range(refine):
        D = _lu_solve(c, L, U, perm, _residual(c, A.bits, X, Bb))
        X = vecops.op(c, "add", X, D).bits.astype(np.int64)
    return Am.PositArray(X.reshape(B.shape), c)


def inv(a, refine: int = 1):
    """The inverse: solve(a, I)."""
    A = _square(a)
    n = A.shape[0]
    I = _arr().PositArray(np.eye(n, dtype=np.int64) * _one(A.config), A.config)
    return solve(A, I, refine)


def det(a):
    """The determinant: the exact product of U's diagonal (each pivot rounded once), rounded once."""
    Am = _arr()
    A = _square(a)
    c = A.config
    if A.isnar().any():
        return Am.nar((), c)
    if A.shape[0] == 0:
        return Am.PositArray(_one(c), c)
    try:
        _, U, _, swaps = lu(c, A.bits)
    except LinAlgError:
        return Am.PositArray(0, c)
    d = np.diag(U).copy()
    if swaps % 2:
        d[0] = vecops.neg(c, d[0])
    return Am.PositArray(Am._prod_bits(c, d), c)


__all__ = ["solve", "inv", "det", "lu", "LinAlgError"]
