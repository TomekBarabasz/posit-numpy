"""Stage 7: elementary functions (positnp.funcs) - tables certified, posit rules, NumPy ufuncs."""

import math

import numpy as np
import pytest

import positnp as pn
from positnp import funcs, ref, vecops
from positnp.ref import PositConfig


@pytest.mark.parametrize("n", [8, 16])
@pytest.mark.parametrize("name", sorted(funcs.FUNCS))
def test_tables_certified(n, name):
    c = PositConfig(n)
    assert funcs.uncertified(c, name) == 0
    t = funcs.table(c, name)
    assert t.shape == (1 << n,) and not t.flags.writeable
    assert t[c.nar] == c.nar                                   # NaR -> NaR


def _f(c, name, x):
    return vecops.to_float(c, funcs.evaluate(c, name, vecops.from_float(c, np.asarray(x, float)).bits))


@pytest.mark.parametrize("n", [8, 16, 32])
def test_posit_rules(n):
    c = PositConfig(n)
    maxpos, minpos = ref.to_float(c, c.nar - 1), ref.to_float(c, 1)
    assert _f(c, "exp", [1e30])[0] == maxpos and _f(c, "exp", [-1e30])[0] == minpos      # saturation
    assert _f(c, "sinh", [-1e30])[0] == -maxpos and _f(c, "sigmoid", [-1e30])[0] == minpos
    assert np.isnan(_f(c, "log", [0.0, -1.0])).all() and np.isnan(_f(c, "arctanh", [1.0])).all()   # poles: NaR
    assert np.isnan(_f(c, "arccosh", [0.5])).all() and np.isnan(_f(c, "log1p", [-1.0])).all()
    assert _f(c, "exp", [0.0])[0] == 1.0 and _f(c, "sin", [0.0])[0] == 0.0 and _f(c, "log", [1.0])[0] == 0.0


def test_correctly_rounded_against_exact_neighbours():
    """posit16: f(x) is the posit nearest to the float64 value, for a sample of x (independent check)."""
    c = PositConfig(16)
    rnd = np.random.default_rng(3)
    allv = vecops.to_float(c, np.arange(1 << 16))
    finite = np.sort(allv[np.isfinite(allv)])
    for name, f in (("exp", math.exp), ("log", math.log), ("sin", math.sin), ("tanh", math.tanh)):
        xs = rnd.choice(finite[(finite > 0) if name == "log" else np.abs(finite) < 40], 300)
        got = _f(c, name, xs)
        for x, g in zip(xs, got):
            v = f(x)
            i = np.searchsorted(finite, v)
            cand = finite[max(0, i - 1):i + 1]
            assert g in cand, (name, x, g, cand)


def test_positarray_ufuncs_and_shapes():
    x = pn.array(np.linspace(-1.5, 1.5, 12).reshape(3, 4), pn.P16)
    for uf, name in ((np.exp, "exp"), (np.log1p, "log1p"), (np.tanh, "tanh"), (np.arctan, "arctan")):
        y = uf(x)
        assert y.shape == (3, 4) and y.config == pn.P16 and y.equal_bits(pn.func(name, x))
    assert np.allclose(np.exp(x).to_float(), np.exp(x.to_float()), rtol=2 ** -10)
    assert np.allclose(pn.sigmoid(x).to_float(), 1 / (1 + np.exp(-x.to_float())), rtol=2 ** -10)
    x32 = pn.array([0.5, 1.0, 2.0])                                        # posit32: float64 evaluation
    assert np.allclose(np.log(x32).to_float(), np.log([0.5, 1.0, 2.0]), rtol=2 ** -26)
    with pytest.raises(ValueError, match="unknown function"):
        pn.func("gamma", x)
