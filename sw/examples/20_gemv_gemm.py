"""Matrix products: gemv (A @ x) and gemm (A @ B), every element exact in the quire.

    python3 examples/20_gemv_gemm.py [--sim | --hw [APP]]

A @ B, np.matmul, np.dot of matrices and pn.matmul (also matrix @ vector and batched (..., M, K) @ (K, N))
compute every element as an exact dot product in the quire, rounded once: each element of C is the
posit nearest to the exact sum - not just "accurate on average". On the KV260 the columns of B go to
the engine's x buffers (OP_XLOAD) and A streams past P of them at a time (OP_GEMV): P multiply-adds
per clock, about 390 M MAC/s per engine with P = 4.
"""

import time
from fractions import Fraction

import numpy as np

import _common
import positnp as pn


def exact_matmul(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """A @ B in exact rational arithmetic, returned as float64 (only the final value is rounded)."""
    Af = [[Fraction(v) for v in row] for row in A.tolist()]
    Bf = [[Fraction(v) for v in row] for row in B.T.tolist()]
    return np.array([[float(sum(a * b for a, b in zip(r, c))) for c in Bf] for r in Af])


def elementwise(C, ref) -> tuple:
    """(median, max) of |C - ref| / |ref| over the elements."""
    e = np.abs(_common.f64(C) - ref) / np.abs(ref)
    return float(np.median(e)), float(np.max(e))


def main(argv=None):
    args = _common.start(__doc__, argv)
    rnd = np.random.default_rng(7)

    _common.section("the API")
    A = pn.array(rnd.standard_normal((3, 4)))
    B = pn.array(rnd.standard_normal((4, 2)))
    x = pn.array([1.0, 2.0, 3.0, 4.0])
    print(f"  (A @ B).shape = {(A @ B).shape},  (A @ x).shape = {(A @ x).shape},  np.matmul == @: "
          f"{np.matmul(A, B).equal_bits(A @ B)},  np.dot: {np.dot(A, B).equal_bits(A @ B)}")
    batch = pn.array(rnd.standard_normal((5, 3, 4)))
    print(f"  batched: {batch.shape} @ {B.shape} -> {(batch @ B).shape}")
    C = A @ B
    print(f"  C[1, 0] is the quire dot of a row and a column: {C[1, 0].equal_bits(np.dot(A[1], B[:, 0]))}")

    _common.section("every element correctly rounded (48 x 48, random normal)")
    n = 48
    Af = rnd.standard_normal((n, n))
    Bf = rnd.standard_normal((n, n))
    P32 = pn.array(Af), pn.array(Bf)
    a32, b32 = P32[0].to_float(), P32[1].to_float()      # the same inputs for every method
    F32 = a32.astype(np.float32), b32.astype(np.float32)
    exact = exact_matmul(a32, b32)
    rows = [("posit32, quire", *elementwise(P32[0] @ P32[1], exact)),
            ("float32 (BLAS sgemm)", *elementwise(F32[0] @ F32[1], exact))]
    P16 = pn.array(a32, pn.P16), pn.array(b32, pn.P16)
    a16, b16 = P16[0].to_float(), P16[1].to_float()
    exact16 = exact_matmul(a16, b16)
    with np.errstate(all="ignore"):
        h = np.zeros((n, n), np.float16)                  # float16 with float16 accumulation
        for k in range(n):
            h = h + np.float16(a16[:, k:k + 1]) * np.float16(b16[k:k + 1, :])
    rows += [("posit16, quire", *elementwise(P16[0] @ P16[1], exact16)),
             ("float16, float16 sums", *elementwise(h, exact16))]
    _common.table(rows, ["C = A @ B", "median rel. error", "max rel. error"])
    print("  posit: every element within half an ulp of its own exact value; floats: the error of a small")
    print("  element is set by the large partial sums it came from")

    _common.section("gemv: y = A x")
    xv = pn.array(rnd.standard_normal(n))
    y = P32[0] @ xv
    ex = exact_matmul(a32, xv.to_float()[:, None])[:, 0]
    print(f"  posit32 max rel. error {elementwise(y, ex)[1]:.2e}; float32 "
          f"{elementwise(F32[0] @ xv.to_float32(), ex)[1]:.2e}")

    if args.engines and 32 in args.engines and getattr(args.engines[32], "gemv_max_k", 0) and args.hw is not None:
        _common.section("speed on the KV260")
        hw = args.engines[32]
        mac = hw.time_matmul(256, 256, 256)
        a = np.float32(rnd.standard_normal((256, 256)))
        t = time.perf_counter()
        for _ in range(10):
            a @ a
        f = 10 * 256 ** 3 / (time.perf_counter() - t)
        print(f"  posit32 engine: {mac / 1e6:.0f} M MAC/s;  NumPy float32 on the A53: {f / 1e6:.0f} M MAC/s")


if __name__ == "__main__":
    main()
