"""The same algorithms in float32, posit32, posit16, float64 (reference) - plan, stage 3.

    python3 examples/formats.py            # PositSw (vecops), on a PC or the board
    sudo python3 examples/formats.py --hw  # posit32 on the KV260 (posit<32>_vec overlay)

1. Horner evaluation of (x - 1)**7 written out as a polynomial, x in [1.1, 2]: cancellation,
   the result (2e-7 .. 1) is far below the size of the terms (up to 35). Posits carry more
   fraction bits than floats of the same width near 1 (posit32: 27, float32: 23).
2. Tree sum of 1/k for k = 1 .. 2**16 (log2 N array additions): precision of a long reduction
   without a quire (the exact quire sum comes in stage 4).

Errors are relative to float64 evaluated the same way: digits = -log10(|y - ref| / |ref|).
"""

from __future__ import annotations

import argparse
import sys
from math import comb
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # sw/, without pip install
import positnp as pn  # noqa: E402

FORMATS = {"float64": np.float64, "float32": np.float32, "posit32": pn.P32, "posit16": pn.P16}


def to_format(x, fmt):
    return pn.array(x, fmt) if isinstance(fmt, pn.PositConfig) else np.asarray(x, dtype=fmt)


def to_f64(y) -> np.ndarray:
    return y.to_float() if isinstance(y, pn.PositArray) else np.asarray(y, dtype=np.float64)


def horner(coeffs, x, fmt) -> np.ndarray:
    """p(x) = coeffs[0] * x**d + ... + coeffs[d], every step rounded in `fmt`."""
    xs = to_format(x, fmt)
    p = to_format(np.full(np.shape(x), coeffs[0]), fmt)
    for c in coeffs[1:]:
        p = p * xs + (to_format(c, fmt) if not isinstance(fmt, pn.PositConfig) else float(c))
    return to_f64(p)


def tree_sum(values, fmt) -> float:
    """Pairwise sum: log2(N) element-wise additions of halves."""
    v = to_format(values, fmt)
    while v.size > 1:
        if v.size % 2:
            v = np.concatenate([v, to_format([0.0], fmt)])
        v = v[0::2] + v[1::2]
    return float(to_f64(v)[0])


def digits(y, ref) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(y - ref) / np.abs(ref)
        return np.where(rel == 0, 17.0, -np.log10(rel))


def experiment_horner(points: int = 2001):
    coeffs = [(-1) ** k * comb(7, k) for k in range(8)]        # (x - 1)**7
    x = np.linspace(1.1, 2.0, points)
    exact = (x - 1) ** 7
    rows = {name: digits(horner(coeffs, x, fmt), exact) for name, fmt in FORMATS.items()}
    return x, exact, rows


def experiment_sum(n: int = 1 << 16):
    values = 1.0 / np.arange(1, n + 1)
    exact = float(np.sum(values.astype(np.longdouble)))
    return {name: (tree_sum(values, fmt), exact) for name, fmt in FORMATS.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", action="store_true", help="posit32 on the KV260 (PositVecHw)")
    args = ap.parse_args()
    if args.hw:
        from positnp.backend_hw import PositVecHw
        pn.set_backend(PositVecHw.open(32))

    x, exact, rows = experiment_horner()
    print("Horner (x - 1)**7, x in [1.1, 2]: correct digits (median / min) vs exact (x - 1)**7")
    for name, d in rows.items():
        print(f"  {name:>8}: {np.median(d):5.2f} / {np.min(d):5.2f}")

    print("\nTree sum of 1/k, k = 1 .. 65536: value and correct digits vs long double")
    for name, (s, ex) in experiment_sum().items():
        print(f"  {name:>8}: {s:.17g}  {float(digits(np.array([s]), ex)[0]):5.2f}")
    print(f"\nstats: {pn.stats()}")


if __name__ == "__main__":
    main()
