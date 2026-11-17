"""Arithmetic: + - * / on posit arrays, scalars, broadcasting, comparisons, rounding.

    python3 examples/10_arithmetic.py [--sim | --hw [APP]]

Every operator is one element-wise pass on the backend (one stream through the engine on the KV260),
correctly rounded: the exact result rounded once to the nearest posit, ties to even. Negation, abs,
comparisons, min / max and sorting are exact and run on the bits (posits order like integers).
"""

import numpy as np

import _common
import positnp as pn


def main(argv=None):
    _common.start(__doc__, argv)

    _common.section("the four operations")
    a = pn.array([1.5, -2.0, 0.1, 1e6])
    b = pn.array([0.5, 3.0, 0.2, 7.0])
    for name, r in (("a + b", a + b), ("a - b", a - b), ("a * b", a * b), ("a / b", a / b)):
        print(f"  {name} = {r}")

    _common.section("scalars and broadcasting")
    print(f"  a * 2      = {a * 2}  (scalar mode of the engine: no second array is sent)")
    print(f"  1 / b      = {1 / b}")
    M = pn.array(np.arange(6.0).reshape(2, 3))
    row = pn.array([10.0, 20.0, 30.0])
    print(f"  M + row    =\n{(M + row).to_float()}")
    print(f"  M * col    =\n{(M * pn.array([[1.0], [-1.0]])).to_float()}")

    _common.section("rounding: each operation rounds once")
    x = pn.array(0.1) + pn.array(0.2)
    print(f"  posit32 0.1 + 0.2 = {float(x):.17g}  (== posit32(0.3): {bool(x == 0.3)})")
    print(f"  float32 0.1 + 0.2 = {float(np.float32(0.1) + np.float32(0.2)):.17g}")
    print(f"  float64 0.1 + 0.2 = {0.1 + 0.2:.17g}")
    third = pn.array(1.0) / 3
    print(f"  posit32 1/3 = {float(third):.17g}, error {abs(float(third) - 1 / 3):.2e} "
          f"(float32: {abs(float(np.float32(1) / np.float32(3)) - 1 / 3):.2e})")

    _common.section("comparisons, min / max, sort (exact, on the bits)")
    v = pn.array([3.0, -1.0, 2.5, np.nan, 0.0])
    print(f"  v          = {v}")
    print(f"  v > 0      = {v > 0}")
    print(f"  -v, abs(v) = {-v}, {abs(v)}")
    print(f"  max, argmax = {np.max(v)}, {np.argmax(v)};  min = {np.min(v)} (NaR is the smallest)")
    print(f"  np.sort(v) = {np.sort(v)}")
    print(f"  np.maximum(v, 1) = {np.maximum(v, 1.0)}")

    _common.section("NaR propagates, nothing overflows")
    print(f"  NaR + 1 = {pn.nar(()) + 1},  0 / 0 = {pn.array(0.0) / 0}")
    big = pn.array([1e30, 1e36])
    print(f"  big * big = {big * big}  (maxpos = 2**120, no inf)")
    print(f"  stats: {pn.stats()}")

    _common.section("other widths")
    for c in (pn.P16, pn.P8):
        print(f"  {str(c):<12} a * b = {pn.array(a, c) * pn.array(b, c)}")


if __name__ == "__main__":
    main()
