"""examples/formats.py runs and posit32 beats float32 where it should (more fraction bits near 1..16)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import formats  # noqa: E402


def test_horner_and_sum():
    _, _, rows = formats.experiment_horner(201)
    med = {k: float(np.median(v)) for k, v in rows.items()}
    assert med["float64"] > med["posit32"] > med["float32"] > med["posit16"]
    sums = {k: float(formats.digits(np.array([s]), ex)[0]) for k, (s, ex) in formats.experiment_sum(1 << 12).items()}
    assert sums["float64"] > sums["posit32"] > sums["float32"] > sums["posit16"]


def test_quire_dot_example():
    import quire_dot
    rows = quire_dot.experiment(scales=(1e0, 1e8, 1e15), n=200)
    for _, cond, rel in rows:
        assert rel["posit32, quire"] < 4e-9                      # half an ulp of posit32 near the result, any cond
    assert rows[-1][2]["float32"] > 1 and rows[-1][2]["float64"] > 1e-3
