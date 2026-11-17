"""The quire: exact sums and dot products, rounded once.

    python3 examples/12_quire_reductions.py [--sim | --hw [APP]]

The quire is a wide fixed-point accumulator (512 bits for posit32) that holds any sum of posit
products exactly. np.sum, np.dot, np.mean and np.linalg.norm on posit arrays accumulate in the quire
and round once at the end, so the result is the correctly rounded value of the exact sum - no matter
how long the sum is, in which order the terms come, or how much they cancel.
pn.quire(x) returns that exact value as a Python Fraction.
"""


import numpy as np

import _common
import positnp as pn


def running_sum(x: pn.PositArray) -> pn.PositArray:
    """What a plain loop does: s = s + x[i], every step rounded (no quire)."""
    s = pn.zeros((), x.config)
    for v in x:
        s = s + v
    return s


def main(argv=None):
    _common.start(__doc__, argv)

    _common.section("sum of 1, then 1000 x 1e-9, then -1")
    terms = pn.array([1.0] + [1e-9] * 1000 + [-1.0])
    exact = pn.quire(terms)
    print(f"  exact (pn.quire)       = {float(exact):.10g}   (a Fraction: {exact.numerator}/{exact.denominator};"
          " 1e-9 itself is rounded to posit32)")
    print(f"  np.sum (quire)         = {float(np.sum(terms)):.10g}")
    print(f"  running sum, posit32   = {float(running_sum(terms)):.10g}   (1 + 1e-9 rounds back to 1, 1000 times)")
    f32 = np.float32(0)
    for v in terms.to_float32():
        f32 = np.float32(f32 + v)
    print(f"  running sum, float32   = {float(f32):.10g}")

    _common.section("dot, mean, norm")
    rnd = np.random.default_rng(0)
    x = pn.array(rnd.standard_normal(10_000))
    y = pn.array(rnd.standard_normal(10_000))
    q = pn.quire(x, y)
    print(f"  np.dot(x, y)       = {float(np.dot(x, y)):.10g}, exact {float(q):.17g}")
    print(f"  np.mean(x)         = {float(np.mean(x)):.10g}   (exact sum / N, rounded once)")
    print(f"  np.linalg.norm(x)  = {float(np.linalg.norm(x)):.10g}   (sqrt of the exact sum of squares)")
    print(f"  pn.quire(x, mode='sumsq') = {float(pn.quire(x, mode='sumsq')):.17g}")

    _common.section("order does not matter")
    perm = rnd.permutation(x.size)
    same = np.dot(x, y).equal_bits(np.dot(x[perm], y[perm]))
    f = x.to_float32(), y.to_float32()
    f_perm = np.dot(f[0][perm], f[1][perm])
    print(f"  posit32 quire dot, shuffled terms: same bits = {same}")
    print(f"  float32 np.dot, shuffled terms: {float(np.dot(*f)):.10g} vs {float(f_perm):.10g}")
    print("  (more in examples/33_reproducible_sums.py)")

    _common.section("posit16 and posit8 have quires too (256 and 128 bits)")
    for c in (pn.P16, pn.P8):
        v = pn.array(np.full(4096, 0.01), c)
        print(f"  {str(c):<12} sum of 4096 x {float(v[0]):.6g}: quire {float(np.sum(v)):.6g},"
              f" running sum {float(running_sum(v[:512])):.6g} after only 512 terms (exact: {float(pn.quire(v[:512])):.6g})")


if __name__ == "__main__":
    main()
