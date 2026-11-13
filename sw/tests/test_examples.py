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
