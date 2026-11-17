"""16-bit arithmetic without overflow and underflow: posit16 + quire vs float16 and bfloat16.

    python3 examples/31_16bit_range.py [--sim | --hw [APP]]

float16 has 10 fraction bits but stops at 65504 and goes subnormal below 6e-5; bfloat16 has the range
of float32 but only 7 fraction bits. posit16 has 11 fraction bits near 1, reaches 7e16 / 1e-17, and its
quire sums any number of products exactly. Three everyday computations in 16 bits:

1. norm of a vector of moderately large values (pixel or 16-bit audio levels): the sum of squares
   overflows float16;
2. energy of a quiet signal: the squares fall into float16 subnormals and lose their precision;
3. softmax of logits up to ~15 computed directly: exp overflows float16 (inf / inf = nan).

(Careful float code avoids these with scaling, the max-subtraction trick or wider accumulators; the point
is that posit16 + quire needs none of it.)
"""

import numpy as np

import _common
import positnp as pn


def bf16(x):
    return _common.round_minifloat(x, 8, 7)


def sumsq_f16(x):
    """Sum of squares in float16, every operation rounded (a float16 MAC loop)."""
    h = np.float16(x)
    s = np.float16(0)
    with np.errstate(all="ignore"):
        for v in h:
            s = np.float16(s + v * v)
    return float(s)


def sumsq_bf16(x):
    s = 0.0
    for v in bf16(x):
        s = float(bf16(s + float(bf16(v * v))))
    return s


def sumsq_posit16_loop(x):
    """posit16 without the quire: squares and running sum, each rounded."""
    p = pn.array(x, pn.P16)
    sq = p * p
    s = pn.zeros((), pn.P16)
    for v in sq:
        s = s + v
    return float(s)


def report(title, x, exact_fn, fns):
    _common.section(title)
    rows = []
    exact = exact_fn(x)
    for name, f in fns.items():
        with np.errstate(all="ignore"):
            v = f(x)
        err = abs(v - exact) / abs(exact) if np.isfinite(v) else float("inf")
        rows.append((name, f"{v:.6g}", "inf / nan" if not np.isfinite(err) else f"{err:.1e}"))
    _common.table(rows, ["method", "value", "relative error"])
    print(f"  exact (float64): {exact:.6g}")


def main(argv=None):
    _common.start(__doc__, argv)
    rnd = np.random.default_rng(4)

    # 1. norm of values ~ 100 .. 300
    x = rnd.uniform(100, 300, 2000)
    xr = np.float16(x).astype(float)          # the same input values for every method (exact in posit16 too)
    report("1. norm of 2000 values in [100, 300]", xr, lambda v: float(np.sqrt(np.sum(v * v))), {
        "float16": lambda v: float(np.sqrt(np.float16(sumsq_f16(v)))),
        "float16, np.linalg.norm": lambda v: float(np.linalg.norm(np.float16(v))),
        "bfloat16": lambda v: float(np.sqrt(sumsq_bf16(v))),
        "posit16, no quire": lambda v: float(np.sqrt(pn.array(sumsq_posit16_loop(v), pn.P16))),
        "posit16, quire": lambda v: float(np.linalg.norm(pn.array(v, pn.P16))),
    })

    # 2. energy of a quiet signal
    q = 2e-3 * np.sin(np.linspace(0, 40 * np.pi, 4000)) + 3e-4 * rnd.standard_normal(4000)
    qr = pn.array(q, pn.P16).to_float()
    report("2. energy sum(x**2) of a quiet signal, |x| ~ 2e-3", qr, lambda v: float(np.sum(v * v)), {
        "float16": sumsq_f16,
        "bfloat16": sumsq_bf16,
        "posit16, no quire": sumsq_posit16_loop,
        "posit16, quire": lambda v: float(np.dot(pn.array(v, pn.P16), pn.array(v, pn.P16))),
    })
    print("  float16: x**2 ~ 4e-6 is subnormal (spacing 6e-8) - the squares keep only a few bits;")
    print("  without the quire, the running sums of float16 and posit16 stop growing once they are large")
    print("  next to a single term")

    # 3. softmax
    _common.section("3. softmax of logits [15, 12, 9, 0], computed directly: exp(x) / sum(exp(x))")
    z = np.array([15.0, 12.0, 9.0, 0.0])
    exact = np.exp(z) / np.exp(z).sum()
    with np.errstate(all="ignore"):
        e16 = np.exp(np.float16(z))
        f16 = (e16 / np.sum(e16)).astype(float)
    ep = np.exp(pn.array(z, pn.P16))
    p16 = (ep / np.sum(ep)).to_float()
    eb = bf16(np.exp(bf16(z)))
    b16 = bf16(eb / bf16(np.sum(eb)))
    rows = [(name, *[f"{v:.4g}" for v in vals]) for name, vals in
            (("exact", exact), ("float16", f16), ("bfloat16", b16), ("posit16 (quire sum)", p16))]
    _common.table(rows, ["method", "p0", "p1", "p2", "p3"])
    print(f"  float16 exp(15) = {float(e16[0])}; posit16 exp(15) = {float(ep[0]):.6g}")


if __name__ == "__main__":
    main()
