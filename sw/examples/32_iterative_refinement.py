"""Solving A x = b to (almost) posit32 precision: iterative refinement with an exact residual.

    python3 examples/32_iterative_refinement.py [--sim | --hw [APP]]

Iterative refinement: solve once in low precision, then repeat
    r = b - A x          (residual)
    solve A d = r        (low precision is fine: d only needs a few correct digits)
    x = x + d
It converges to the precision of x - if the residual is computed accurately. In float32 the residual
itself is rounded garbage once x is good (b and A x agree in most digits: catastrophic cancellation),
so float32 refinement stalls near cond(A) * 1e-8. With posits the residual is one row of
[A | b] @ [-x; 1]: an exact dot product in the quire, rounded once - so x converges to what posit32 can
hold (~1e-9 near 1), with every number stored in 32 bits and the matmul on the engine.

All three methods share one float32 LU factorization (written out here: np.linalg.solve may compute
float32 systems in double precision internally, which would hide the effect).
"""

import numpy as np

import _common
import positnp as pn


def test_matrix(n: int, cond: float, rnd) -> np.ndarray:
    """Random matrix with condition number ~cond, on a grid of 2**-20 (exact in float32 and posit32)."""
    q1, _ = np.linalg.qr(rnd.standard_normal((n, n)))
    q2, _ = np.linalg.qr(rnd.standard_normal((n, n)))
    A = q1 @ np.diag(np.logspace(0, -np.log10(cond), n)) @ q2 * 4
    return np.round(A * 2 ** 20) / 2 ** 20


def lu32(A: np.ndarray):
    """LU with partial pivoting, every operation in float32 (np.linalg.solve may work in float64 inside)."""
    LU = np.array(A, dtype=np.float32)
    n = LU.shape[0]
    piv = np.arange(n)
    for k in range(n - 1):
        p = k + int(np.argmax(np.abs(LU[k:, k])))
        LU[[k, p]], piv[[k, p]] = LU[[p, k]], piv[[p, k]]
        LU[k + 1:, k] /= LU[k, k]
        LU[k + 1:, k + 1:] -= np.outer(LU[k + 1:, k], LU[k, k + 1:])
    return LU, piv


def solve32(lu, b) -> np.ndarray:
    """Forward and back substitution in float32."""
    LU, piv = lu
    y = np.array(b, dtype=np.float32)[piv]
    n = y.size
    for i in range(1, n):
        y[i] -= LU[i, :i] @ y[:i]
    for i in range(n - 1, -1, -1):
        y[i] = (y[i] - LU[i, i + 1:] @ y[i + 1:]) / LU[i, i]
    return y


def err(x, x_ref) -> float:
    return float(np.max(np.abs(_common.f64(x) - x_ref) / np.abs(x_ref)))


def main(argv=None):
    _common.start(__doc__, argv)
    rnd = np.random.default_rng(11)
    n, iters = 64, 6
    final = {}

    for cond in (1e3, 1e5):
        A = test_matrix(n, cond, rnd)
        x_true = 1 + rnd.random(n)                       # unknowns in [1, 2)
        b = np.round(A @ x_true * 2 ** 20) / 2 ** 20     # on the grid too: exact in both formats
        x_ref = np.linalg.solve(A, b)                    # float64: ~1e-16 * cond, far below what we measure
        A32, b32 = np.float32(A), np.float32(b)
        Ab = pn.array(np.hstack([A, b[:, None]]))        # [A | b] in posit32 (exact: same grid)

        _common.section(f"n = {n}, cond(A) = {np.linalg.cond(A):.0e}: max relative error of x per iteration")
        lu = lu32(A32)                                   # one float32 LU, used by all three methods
        x_f = solve32(lu, b32)
        x_m = x_f.copy()
        x_p = pn.array(x_f)
        rows = [(0, err(x_f, x_ref), err(x_m, x_ref), err(x_p, x_ref))]
        for it in range(1, iters + 1):
            r_f = b32 - A32 @ x_f                                         # residual in float32
            x_f = x_f + solve32(lu, r_f)
            r_m = (b - A @ x_m.astype(np.float64)).astype(np.float32)     # residual in float64, x in float32
            x_m = np.float32(x_m + solve32(lu, r_m))
            r_p = Ab @ pn.array(np.append(-x_p.to_float(), 1.0))          # exact in the quire, rounded once
            x_p = x_p + pn.array(solve32(lu, r_p.to_float32()))          # correction from the float32 LU
            rows.append((it, err(x_f, x_ref), err(x_m, x_ref), err(x_p, x_ref)))
        _common.table(rows, ["iteration", "float32", "float32 + float64 residual", "posit32 + quire residual"],
                      fmt="{:.1e}")
        final[cond] = dict(zip(("float32", "mixed", "posit32"), rows[-1][1:]))
    print("\n  float32: stuck at ~cond * 1e-8. Classic mixed precision (float64 residual) reaches float32's")
    print("  own limit, ~6e-8. posit32 with the quire residual reaches posit32's limit near 1 (2**-28 ~ 4e-9)")
    print("  without any 64-bit arithmetic: 10x more accurate in the same 32 bits per number.")
    return final


if __name__ == "__main__":
    main()
