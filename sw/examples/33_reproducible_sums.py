"""Reproducible results: a quire sum does not depend on the order of the terms.

    python3 examples/33_reproducible_sums.py [--sim | --hw [APP]]

Floating-point addition is not associative, so the same sum computed in a different order - another
number of threads, another BLAS, another GPU block size, a shuffled dataset - gives different bits.
That makes parallel results hard to reproduce, test and debug. The quire adds exactly, so a posit sum
or dot product has exactly one possible result (the correctly rounded exact value) whatever the order.
"""

import numpy as np

import _common
import positnp as pn


def float_orders(x: np.ndarray, rnd, threads=(1, 2, 4, 8, 16, 32)):
    """The same float sum in orders that real code uses: sequential, pairwise (np.sum), blocked
    "parallel" reductions with T partial sums, and shuffled data."""
    dt = x.dtype
    out = {"sequential": np.cumsum(x, dtype=dt)[-1], "reversed": np.cumsum(x[::-1], dtype=dt)[-1],
           "np.sum (pairwise)": np.sum(x, dtype=dt)}
    for t in threads[1:]:
        parts = [np.cumsum(c, dtype=dt)[-1] for c in np.array_split(x, t)]
        out[f"{t} threads"] = np.cumsum(np.array(parts, dt), dtype=dt)[-1]
    for i in range(10):
        out[f"shuffled #{i}"] = np.sum(x[rnd.permutation(x.size)], dtype=dt)
    return out


def main(argv=None):
    _common.start(__doc__, argv)
    rnd = np.random.default_rng(8)
    n = 100_000
    data = rnd.standard_normal(n) * np.exp(rnd.uniform(-8, 8, n))     # wide dynamic range, sum near 0
    data -= data.mean()
    p = pn.array(data)
    x = p.to_float()                                                    # the same values in every format
    exact = float(pn.quire(p))

    _common.section(f"sum of {n} terms (|terms| up to {np.max(np.abs(x)):.0f}, exact sum {exact:.6g})")
    rows = []
    for name, dt in (("float32", np.float32), ("float64", np.float64)):
        res = float_orders(x.astype(dt), rnd)
        vals = np.array([float(v) for v in res.values()])
        rows.append((name, len(res), len(set(vals.tolist())), f"{vals.min():.10g}", f"{vals.max():.10g}"))
    sums = [np.sum(p)] + [np.sum(p[rnd.permutation(n)]) for _ in range(10)]
    quires = []                                    # "threads": one quire per chunk, the quires added exactly
    for t in (2, 4, 8, 16, 32):
        edges = np.linspace(0, n, t + 1).astype(int)
        quires.append(sum(pn.quire(p[a:b]) for a, b in zip(edges[:-1], edges[1:])))
    distinct = len({int(s.bits) for s in sums}) + len({q for q in quires} - {pn.quire(p)})
    rows.append(("posit32, quire", len(sums) + len(quires), distinct, f"{float(sums[0]):.10g}", f"{float(sums[0]):.10g}"))
    _common.table(rows, ["format", "orders tried", "different results", "smallest", "largest"])
    distinct_results = {r[0]: r[2] for r in rows}
    print("  posit32 'threads': every thread fills its own quire, the quires are added exactly (here as the")
    print("  Fractions pn.quire returns) and rounded once at the end - the same value for any thread count.")
    print("  (Rounding the per-thread results first and summing those would bring order dependence back.)")

    _common.section("dot products in a different order: np.dot(x, y) vs np.dot(x[perm], y[perm])")
    y = pn.array(rnd.standard_normal(n))
    perm = rnd.permutation(n)
    xf, yf = x.astype(np.float32), y.to_float32()
    print(f"  float32: {float(np.dot(xf, yf)):.10g} vs {float(np.dot(xf[perm], yf[perm])):.10g}")
    print(f"  posit32: {float(np.dot(p, y)):.10g} vs {float(np.dot(p[perm], y[perm])):.10g}  "
          f"(same bits: {np.dot(p, y).equal_bits(np.dot(p[perm], y[perm]))})")
    return distinct_results


if __name__ == "__main__":
    main()
