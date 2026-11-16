"""The examples run: formats.py / quire_dot.py show what they claim, the numbered tour runs (also on simulated engines)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))   # formats, quire_dot, _common
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


# ---- the numbered tour (examples/NN_*.py, template.py) -----------------------------------------------

import importlib.util  # noqa: E402

import pytest  # noqa: E402

import positnp as pn  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TOUR = sorted(p.name for p in EXAMPLES.glob("[0-9][0-9]_*.py")) + ["template.py"]
SIM_FAST = ["00_hello_posit.py", "02_board_and_features.py", "03_backends_and_stats.py", "10_arithmetic.py",
            "11_numpy_interop.py", "13_fma_sqrt_functions.py", "23_device_memory.py", "32_iterative_refinement.py"]


def _load(name):
    spec = importlib.util.spec_from_file_location(name[:-3], EXAMPLES / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _software_backends():
    yield
    pn.set_backend(None)


@pytest.mark.parametrize("name", TOUR)
def test_tour_runs(name, capsys):
    _load(name).main([])
    assert "Traceback" not in capsys.readouterr().out


@pytest.mark.parametrize("name", SIM_FAST)
def test_tour_runs_on_simulated_engines(name, capsys):
    _load(name).main(["--sim"])
    out = capsys.readouterr().out
    assert "simulated engines" in out or "-> PositVecHw" in out          # 02 sets up quietly, then lists backends


def test_fir_snr():
    snr = _load("21_fir_filter.py").main([])
    assert snr["posit32"] > snr["float32"] + 20 and snr["posit16"] > snr["float16"] + 15


def test_iterative_refinement_reaches_posit32_precision():
    final = _load("32_iterative_refinement.py").main([])
    for res in final.values():
        assert res["posit32"] < 5e-9 < res["mixed"] < res["float32"]


def test_quire_sums_are_reproducible():
    distinct = _load("33_reproducible_sums.py").main([])
    assert distinct["posit32, quire"] == 1 and distinct["float32"] > 1


def test_round_minifloat_matches_float16():
    import _common
    x = np.random.default_rng(0).standard_normal(10000) * np.exp(np.random.default_rng(1).uniform(-12, 12, 10000))
    with np.errstate(over="ignore"):
        assert np.array_equal(_common.round_minifloat(x, 5, 10), np.float16(x).astype(np.float64))
