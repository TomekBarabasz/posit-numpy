"""Posits vs floats of the same width: precision where the numbers are, range where they are not.

    python3 examples/30_tapered_precision.py [--plot precision.png]

For every binade 2**s .. 2**(s+1): the median relative rounding error of random values in it, for
posit16 / float16 / bfloat16, posit8 / float8 (E4M3, E5M2) and posit32 / float32.
Floats spend the same number of bits on precision everywhere and stop at a hard range limit (then inf,
or 0 / subnormals); posits put more precision near 1 and less far from it, and cover a much wider
range. Most data in practice - normalized signals, probabilities, weights, activations, values scaled
to "order 1" - lives near 1, where the posit is the more precise format.
"""

import numpy as np

import _common
import positnp as pn


def formats():
    """name -> rounding function float64 -> float64, grouped by width."""
    p = lambda c: (lambda x: pn.array(x, c).to_float())                                 # noqa: E731
    with_err = lambda f: (lambda x: _quiet(f, x))                                       # noqa: E731
    return {
        8: {"posit8": p(pn.P8), "float8 E4M3": lambda x: _common.round_minifloat(x, 4, 3, saturate=True),
            "float8 E5M2": lambda x: _common.round_minifloat(x, 5, 2)},
        16: {"posit16": p(pn.P16), "float16": with_err(lambda x: np.float16(x).astype(float)),
             "bfloat16": lambda x: _common.round_minifloat(x, 8, 7)},
        32: {"posit32": p(pn.P32), "float32": with_err(lambda x: np.float32(x).astype(float))},
    }


def _quiet(f, x):
    with np.errstate(all="ignore"):
        return f(x)


def median_error(rnd_fn, x) -> float:
    y = rnd_fn(x)
    with np.errstate(all="ignore"):
        e = np.abs(y - x) / np.abs(x)
    e = np.where(np.isfinite(y), e, np.inf)       # overflow to inf: no precision at all
    return float(np.median(e))


def main(argv=None):
    ap = _common.parser(__doc__)
    ap.add_argument("--plot", metavar="PNG", help="save a plot (needs matplotlib)")
    args = ap.parse_args(argv)
    _common.setup(args)
    rnd = np.random.default_rng(0)
    F = formats()
    scales = list(range(-40, 41, 4))
    curves = {}

    for width, fmts in F.items():
        _common.section(f"{width} bits: median relative error of values in [2**s, 2**(s+1))")
        rows = []
        for s in scales:
            x = 2.0 ** (s + rnd.random(4000))
            errs = [median_error(f, x) for f in fmts.values()]
            for name, e in zip(fmts, errs):
                curves.setdefault(name, []).append(e)
            if s % 8 == 0:
                rows.append([f"2**{s}"] + ["lost" if e >= 0.5 else f"{e:.1e}" for e in errs])
        _common.table(rows, ["s"] + list(fmts))
        names = list(fmts)
        pe, fe = (np.array(curves[k][-len(scales):]) for k in names[:2])
        i0 = scales.index(0)
        lo = hi = i0
        while lo > 0 and pe[lo - 1] <= fe[lo - 1] * 1.02:
            lo -= 1
        while hi < len(scales) - 1 and pe[hi + 1] <= fe[hi + 1] * 1.02:
            hi += 1
        only = [s for s, a, b in zip(scales, pe, fe) if a < 0.5 <= b]
        print(f"  {names[0]} at least as precise as {names[1]} in [2**{scales[lo]}, 2**{scales[hi] + 4})"
              + (f"; {names[1]} lost where {names[0]} still works: {len(only)} of these {len(scales)} binades"
                 if only else ""))

    _common.section("on typical data: normal(0, sigma) samples, median relative error")
    rows = []
    for sigma in (1e-3, 1e-1, 1.0, 10.0, 1e3):
        x = rnd.standard_normal(20000) * sigma
        rows.append([f"sigma = {sigma:g}"] + [f"{median_error(f, x):.1e}" for w in (8, 16) for f in F[w].values()])
    _common.table(rows, ["data"] + [n for w in (8, 16) for n in F[w]])
    print("  near sigma = 1 posit16 has half the error of float16 (11 vs 10 fraction bits) and ~15x less than")
    print("  bfloat16, and it never overflows; far from 1 the floats keep their precision longer.")

    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 4.5))
        color = {8: "tab:blue", 16: "tab:red", 32: "tab:green"}
        styles = ["-", "--", ":"]
        for width, fmts in F.items():
            for i, name in enumerate(fmts):
                e = np.where(np.array(curves[name]) >= 0.5, np.nan, curves[name])
                ax.semilogy(scales, e, styles[i], color=color[width], marker="." if i == 0 else None,
                            lw=2 if i == 0 else 1.3, label=name)
        ax.set_xlabel("binade s (values 2**s .. 2**(s+1))")
        ax.set_ylabel("median relative rounding error")
        ax.set_title("Tapered precision: posits vs floats")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(ncol=3, fontsize=8)
        fig.tight_layout()
        fig.savefig(args.plot, dpi=120)
        print(f"\nplot: {args.plot}")


if __name__ == "__main__":
    main()
