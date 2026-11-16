"""Fused multiply-add, axpy, square root and elementary functions (exp, log, sin, tanh, sigmoid, ...).

    python3 examples/13_fma_sqrt_functions.py [--sim | --hw [APP]]

- pn.fma(a, b, c) = a * b + c with one rounding; pn.axpy(alpha, x, y) = alpha * x + y (BLAS axpy).
  On the KV260: constant c -> OP_FMA, scalar alpha -> OP_AXPY, three arrays -> OP_FMA3, one stream pass.
- np.sqrt: correctly rounded.
- np.exp, np.log, np.sin, np.tanh, pn.sigmoid and 15 more: posit8 / posit16 from correctly rounded
  tables (the engine looks them up in BRAM, up to L elements per clock), posit32 from float64 libm
  rounded once. Posit rules: overflow saturates, underflow stays at minpos, poles give NaR.
"""

import numpy as np

import _common
import positnp as pn


def main(argv=None):
    _common.start(__doc__, argv)

    _common.section("fma: one rounding instead of two")
    a = pn.array([1.0 + 2.0 ** -20])
    c = pn.array([-(1.0 + 2.0 ** -19)])
    exact = (1 + 2.0 ** -20) ** 2 - (1 + 2.0 ** -19)        # = 2**-40
    print(f"  a * a + c, two roundings: {float(a * a + c):.6g}")
    print(f"  pn.fma(a, a, c):          {float(pn.fma(a, a, c)):.6g}   (exact: {exact:.6g})")
    x, y = pn.array(np.arange(4.0)), pn.array(np.ones(4))
    print(f"  pn.axpy(0.5, x, y) = {pn.axpy(0.5, x, y)}")
    print(f"  pn.fma(x, x, 1.0)  = {pn.fma(x, x, 1.0)}   (c a scalar)")

    _common.section("sqrt")
    v = pn.array([2.0, 1e-30, 1e30, -1.0])
    print(f"  np.sqrt({v.to_float()}) = {np.sqrt(v)}   (negative -> NaR)")

    _common.section("elementary functions")
    t = pn.array([-2.0, 0.0, 0.5, 3.0])
    for f in (np.exp, np.log, np.sin, np.tanh, np.arctan, np.cbrt):
        print(f"  np.{f.__name__:<7}{t.to_float()} = {f(t)}")
    print(f"  pn.sigmoid       = {pn.sigmoid(t)}")
    print(f"  pn.func('log1p') = {pn.func('log1p', t)}")
    print(f"  exp(+-1000) in posit16: {np.exp(pn.array([1000.0, -1000.0], pn.P16))}  (maxpos / minpos, never inf / 0)")
    with np.errstate(over="ignore"):
        print(f"  float16: {np.exp(np.float16([1000.0, -1000.0]))}")

    _common.section("accuracy in 16 bits: posit16 vs float16, both correctly rounded")
    xs = np.arange(-6.0, 6.0, 2.0 ** -8)          # exact in both formats: only the result is rounded
    rows = []
    for name, f in (("exp", np.exp), ("tanh", np.tanh), ("sigmoid", lambda z: 1 / (1 + np.exp(-z))),
                    ("sin", np.sin), ("log (x > 0)", np.log)):
        x = xs[xs > 0] if name.startswith("log") else xs
        ref = f(x)
        p16 = pn.func(name.split()[0], pn.array(x, pn.P16)).to_float()
        f16 = np.float16(ref).astype(np.float64)   # float16 rounded once from float64: the best float16 can do
        ok = ref != 0
        e_p = np.abs(p16 - ref)[ok] / np.abs(ref[ok])
        e_f = np.abs(f16 - ref)[ok] / np.abs(ref[ok])
        rows.append((name, float(np.median(e_p)), float(np.median(e_f)), float(np.max(e_p)), float(np.max(e_f))))
    _common.table(rows, ["f(x), x in [-6, 6)", "posit16 median", "float16 median", "posit16 max", "float16 max"])
    print("  (relative error vs float64; posit16 has 11 fraction bits in [1/16, 16), float16 has 10 everywhere,")
    print("   so posit16 wins on results of moderate size and loses on small or large ones: exp(-6), exp(6), sigmoid(-6))")

if __name__ == "__main__":
    main()
