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

from . import ref as _ref, vecops
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


def _residual_mm(A, X, B):
    """B - A X as PositArrays, every element the exact sum rounded once: one matmul [B | A] @ [I; -X] (on a
    KV260 engine with gemv: the engine, P columns per pass)."""
    Am = _arr()
    c = A.config
    r = B.shape[1]
    left = np.concatenate([B, A], axis=1)
    right = np.concatenate([Am.PositArray(np.eye(r, dtype=np.int64) * _one(c), c), -X], axis=0)
    return left @ right


def _square(a, batch: bool = False):
    A = a if isinstance(a, _arr().PositArray) else _arr().array(a)
    if A.ndim < 2 or A.shape[-1] != A.shape[-2] or (A.ndim > 2 and not batch):
        raise LinAlgError(f"a square matrix expected, got shape {A.shape}")
    return A


def _solve2(A, B2, refine: int, lu_config):
    """One system: A (n x n), B2 (n x r), PositArrays of one format."""
    Am = _arr()
    c = A.config
    lc = lu_config or c
    L, U, perm, _ = lu(lc, A.astype(lc).bits)
    X = Am.PositArray(_lu_solve(lc, L, U, perm, B2.astype(lc).bits.astype(np.int64)), lc).astype(c)
    for _ in range(refine):
        R = _residual_mm(A, X, B2)
        D = Am.PositArray(_lu_solve(lc, L, U, perm, R.astype(lc).bits.astype(np.int64)), lc).astype(c)
        X = X + D
    return X


def solve(a, b, refine: int = None, lu=None):
    """x with a @ x = b, see the module docstring.

    b: a vector (n,) or right-hand sides (..., n, k); a may be a stack (..., n, n) (like NumPy 2).
    lu: the format of the factorisation, e.g. pn.P16 for a posit32 system (mixed precision): LU and the
    corrections in that format, the residuals exact in the quire of a's format. refine: steps of iterative
    refinement (default 1, with lu= 4)."""
    Am = _arr()
    A, B = Am._unify(a, b)
    A = _square(A, batch=True)
    c, n = A.config, A.shape[-1]
    steps = (1 if lu is None else 4) if refine is None else int(refine)
    vector = B.ndim == 1
    if (vector and B.shape[0] != n) or (not vector and B.shape[-2] != n):
        raise ValueError(f"solve: b of shape {B.shape} does not match a of shape {A.shape}")
    B3 = B.reshape(n, 1) if vector else B
    batch = np.broadcast_shapes(A.shape[:-2], B3.shape[:-2])
    Ab = Am.PositArray(np.broadcast_to(A.bits, batch + (n, n)), c)
    Bb = Am.PositArray(np.broadcast_to(B3.bits, batch + B3.shape[-2:]), c)
    out = np.zeros(Bb.shape, np.int64)
    for idx in np.ndindex(*batch):
        Ai, Bi = Ab[idx], Bb[idx]
        if Ai.isnar().any() or Bi.isnar().any():
            out[idx] = c.nar
            continue
        out[idx] = _solve2(Ai, Bi, steps, lu).bits
    X = Am.PositArray(out, c)
    return X.reshape(batch + (n,)) if vector else X


def inv(a, refine: int = None, lu=None):
    """The inverse: solve(a, I) (a stack of matrices too)."""
    Am = _arr()
    A = _square(a, batch=True)
    n = A.shape[-1]
    I = Am.PositArray(np.broadcast_to(np.eye(n, dtype=np.int64) * _one(A.config), A.shape), A.config)
    return solve(A, I, refine, lu)


def _det2(A) -> Tuple[int, list, int]:
    """(swaps, diagonal of U, status): status 0 ok, 1 singular, 2 NaR."""
    if A.isnar().any():
        return 0, [], 2
    try:
        _, U, _, swaps = lu(A.config, A.bits)
    except LinAlgError:
        return 0, [], 1
    return swaps, np.diag(U).copy(), 0


def det(a):
    """The determinant: the exact product of U's diagonal (each pivot rounded once), rounded once."""
    Am = _arr()
    A = _square(a, batch=True)
    c = A.config
    out = np.zeros(A.shape[:-2], np.int64)
    for idx in np.ndindex(*A.shape[:-2]):
        Ai = A[idx]
        if Ai.shape[0] == 0:
            out[idx] = _one(c)
            continue
        swaps, d, st = _det2(Ai)
        if st:
            out[idx] = c.nar if st == 2 else 0
            continue
        if swaps % 2:
            d[0] = vecops.neg(c, d[0])
        out[idx] = Am._prod_bits(c, d)
    return Am.PositArray(out, c)


def slogdet(a):
    """(sign, log|det|): the sign exact, log|det| = sum of log|u_kk| in float64 (exact pivots), rounded once;
    a singular matrix: (0, NaR) (a posit has no -inf)."""
    import math as _m
    from fractions import Fraction
    Am = _arr()
    A = _square(a, batch=True)
    c = A.config
    sg = np.zeros(A.shape[:-2], np.int64)
    la = np.zeros(A.shape[:-2])
    for idx in np.ndindex(*A.shape[:-2]):
        swaps, d, st = _det2(A[idx])
        if st:
            sg[idx], la[idx] = (c.nar if st == 2 else 0), np.nan
            continue
        s, tot = (-1) ** swaps, 0.0
        for b in d:
            m, e = _ref.decode(c, int(b))
            s = -s if m < 0 else s
            tot += _m.log(abs(m)) + e * _m.log(2)
        sg[idx] = _one(c) if s > 0 else vecops.neg(c, _one(c))
        la[idx] = tot
    return Am.PositArray(sg, c), Am.array(la, c)


def cholesky(a, upper: bool = False):
    """L with a = L L^T (a symmetric positive definite; the lower triangle is used, as in NumPy).

    Diagonal: the square root of the exact a[j, j] - sum L[j, k]**2, rounded once; below it: the exact sum
    a[i, j] - sum L[i, k] L[j, k] rounded once, then divided by L[j, j]. Not positive definite: LinAlgError."""
    Am = _arr()
    A = _square(a)
    c, n = A.config, A.shape[0]
    if A.isnar().any():
        return Am.nar(A.shape, c)
    one = _one(c)
    neg = lambda b: vecops.neg(c, b).astype(np.int64)      # noqa: E731
    L = np.zeros((n, n), np.int64)
    Ab = A.bits.astype(np.int64)
    for j in range(n):
        q, nar = vecops.quire(c, "sumsq", L[j, :j]) if j else (0, False)
        t = _ref.quire_term(c, int(Ab[j, j]))
        q = t - q
        if nar or q <= 0:
            raise LinAlgError("Matrix is not positive definite")
        L[j, j] = Am._sqrt_bits(c, q, False)
        if j + 1 < n:
            rows = n - j - 1
            s = _dot(c, [Ab[j + 1:, j]] + [L[j + 1:, k] for k in range(j)],
                     [np.full(rows, one)] + [np.full(rows, neg(L[j, k])) for k in range(j)])
            L[j + 1:, j] = _div(c, s, int(L[j, j]))
    R = Am.PositArray(L, c)
    return R.T.copy() if upper else R


def lstsq(a, b, rcond=None):
    """Least squares min |b - a x| for a of full column rank, through the augmented system
    [[I, a], [a^T, 0]] [r; x] = [b; 0] (Bjorck), solved with the quire LU and one refinement step.

    Returns (x, residuals, rank, s) like NumPy; s (singular values) is None (no SVD here); residuals: the exact
    sum of squares of b - a x, rounded once (m > n), else an empty array."""
    Am = _arr()
    A, B = Am._unify(a, b)
    if A.ndim != 2:
        raise LinAlgError("lstsq: a 2-D matrix expected")
    c = A.config
    m, n = A.shape
    vector = B.ndim == 1
    B2 = B.reshape(m, 1) if vector else B
    k = B2.shape[1]
    I = Am.PositArray(np.eye(m, dtype=np.int64) * _one(c), c)
    Z = Am.zeros((n, n), c)
    M = np.concatenate([np.concatenate([I, A], axis=1), np.concatenate([A.T, Z], axis=1)], axis=0)
    rhs = np.concatenate([B2, Am.zeros((n, k), c)], axis=0)
    sol = solve(M, rhs)
    X = sol[m:]
    res = _residual_mm(A, X, B2)
    if m > n:
        resid = Am.PositArray([Am._quire(res[:, j], "sumsq")[0] for j in range(k)], c)
    else:
        resid = Am.zeros((0,), c)
    return (X.reshape(n) if vector else X), resid, n, None


__all__ = ["solve", "inv", "det", "slogdet", "cholesky", "lstsq", "lu", "LinAlgError"]
