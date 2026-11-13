"""Ill-conditioned dot products: float32, float64, posit32 without and with the quire - plan, stage 4.

    python3 examples/quire_dot.py            # PositSw
    sudo python3 examples/quire_dot.py --hw  # posit32 on the KV260 (posit32_vec overlay, stage 4 bitstream)

x = [u, u, d], y = [v, -v, 1] in random order: the exact dot product is sum(d) (tiny), while the
terms u*v are large, so the condition number sum|x*y| / |x.y| grows with the scale of u*v.
float sums lose sum(d) in the rounding of partial sums; the quire adds every product exactly and
rounds once, so its error stays at half an ulp of posit32 for any condition number, as long as
the terms fit in the quire (|x*y| <= maxpos**2).
"""

from __future__ import annotations

import argparse
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # sw/, without pip install
import positnp as pn  # noqa: E402


def make(n: int, scale: float, rnd: np.random.Generator):
    """Vectors (in posit32-exact float64 values) with exact dot = sum(d)."""
    u = pn.array(rnd.standard_normal(n) * scale).to_float()
    v = pn.array(rnd.standard_normal(n)).to_float()
    d = pn.array(rnd.standard_normal(4)).to_float()
    x = np.concatenate([u, u, d])
    y = np.concatenate([v, -v, np.ones(4)])
    perm = rnd.permutation(x.size)
    return x[perm], y[perm]


def exact_dot(x, y) -> Fraction:
    return sum((Fraction(a) * Fraction(b) for a, b in zip(x.tolist(), y.tolist())), Fraction(0))


def methods(x, y):
    px, py = pn.array(x), pn.array(y)
    prod = px * py                                         # posit32 products, rounded
    while prod.size > 1:                                   # tree sum without the quire
        if prod.size % 2:
            prod = np.concatenate([prod, pn.zeros(1)])
        prod = prod[0::2] + prod[1::2]
    return {
        "float32": float(np.dot(x.astype(np.float32), y.astype(np.float32))),
        "float64": float(np.dot(x, y)),
        "posit32, bez quire": float(prod[0]),
        "posit32, quire": float(np.dot(px, py)),
    }


def experiment(scales=(1e0, 1e2, 1e4, 1e6, 1e8, 1e10, 1e12, 1e15), n: int = 1000, seed: int = 0):
    rnd = np.random.default_rng(seed)
    rows = []
    for s in scales:
        x, y = make(n, s, rnd)
        ex = exact_dot(x, y)
        cond = float(sum(abs(Fraction(a) * Fraction(b)) for a, b in zip(x.tolist(), y.tolist())) / abs(ex))
        rel = {k: abs(Fraction(v) - ex) / abs(ex) if np.isfinite(v) else float("inf") for k, v in methods(x, y).items()}
        rows.append((s, cond, {k: float(v) for k, v in rel.items()}))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", action="store_true", help="posit32 on the KV260 (PositVecHw)")
    args = ap.parse_args()
    if args.hw:
        from positnp.backend_hw import PositVecHw
        pn.set_backend(PositVecHw.open(32))
    rows = experiment()
    names = list(rows[0][2])
    print(f"{'cond':>10}  " + "  ".join(f"{k:>18}" for k in names) + "   (relative error vs exact)")
    for _, cond, rel in rows:
        print(f"{cond:10.1e}  " + "  ".join(f"{rel[k]:18.2e}" for k in names))


if __name__ == "__main__":
    main()
