"""PositArray: operators, broadcasting, level 0, NaR semantics, NumPy protocols, backends, stats."""

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

import positnp as pn
from positnp import vecops
from positnp.backend_hw import PositVecHw
from positnp.fakes import FakeDma, FakeVecMmio
from positnp.udmabuf import FakeUDmaBuf


def fake_hw(config):
    mem = FakeUDmaBuf(1 << 16)               # small: several DMA rounds for longer arrays
    vec = FakeVecMmio(config)
    return PositVecHw(vec, FakeDma(mem, vec), mem)


# ---- conversions -----------------------------------------------------------------------------

def test_roundtrip_posit16_all():
    bits = np.arange(1 << 16)
    x = pn.from_bits(bits, pn.P16)
    back = pn.array(x.to_float(), pn.P16)
    assert np.array_equal(back.bits, np.where(bits == pn.P16.nar, pn.P16.nar, bits))


def test_roundtrip_posit32_random():
    bits = np.random.default_rng(0).integers(0, 1 << 32, 200_000)
    assert np.array_equal(pn.array(pn.from_bits(bits).to_float()).bits, bits)


def test_array_basics():
    x = pn.array([1.0, 2.5, -3.0])
    assert x.shape == (3,) and x.config == pn.P32 and x.bits.dtype == np.uint32
    assert np.array_equal((x * x + x).to_float(), [2.0, 8.75, 6.0])
    assert np.array_equal((x / 2).to_float(), [0.5, 1.25, -1.5])
    assert np.array_equal((1 - x).to_float(), [0.0, -1.5, 4.0])
    assert np.array_equal((6 / x).to_float(), [6.0, 2.4000000059604645, -2.0])
    assert float(x[1]) == 2.5 and "NaR" in repr(pn.nar(3))
    with pytest.raises(TypeError):
        x + pn.array([1.0, 2.0, 3.0], pn.P16)


# ---- broadcasting and views -------------------------------------------------------------------

def test_broadcast_and_slices():
    a = pn.array(np.arange(12.0).reshape(3, 4))
    row = pn.array([1.0, 2.0, 3.0, 4.0])
    col = pn.array([[10.0], [20.0], [30.0]])
    assert np.array_equal((a * row).to_float(), np.arange(12.0).reshape(3, 4) * [1, 2, 3, 4])
    assert np.array_equal((a + col).to_float(), np.arange(12.0).reshape(3, 4) + [[10], [20], [30]])
    v = a[:, ::2]                                   # non-contiguous
    assert np.array_equal((v - 1).to_float(), np.arange(12.0).reshape(3, 4)[:, ::2] - 1)
    a[0, 0] = 7.0
    assert float(a[0, 0]) == 7.0
    assert (a.T).shape == (4, 3) and a.reshape(12).shape == (12,)


# ---- level 0 and NaR ----------------------------------------------------------------------------

def test_nar_semantics():
    x = pn.array([3.0, np.nan, -1.0, 0.0])
    assert np.isnan(x.to_float()[1])
    assert np.isnan((x + 1).to_float()[1])                    # NaR propagates
    s = np.sort(x)
    assert np.isnan(s.to_float()[0]) and np.array_equal(s.to_float()[1:], [-1.0, 0.0, 3.0])
    assert np.isnan(float(np.min(x))) and float(np.max(x)) == 3.0
    assert np.argmax(x) == 0 and np.argmin(x) == 1
    assert (x == x).all()                                     # NaR == NaR (posit standard)
    assert (x < 0).tolist() == [False, True, True, False]     # NaR below every real
    assert np.array_equal(np.isnan(x), [False, True, False, False])


def test_neg_abs_compare_match_float():
    c = pn.P16
    x = pn.from_bits(np.arange(1 << 16), c)
    real = ~x.isnar()
    f = x.to_float()
    assert np.array_equal((-x).to_float()[real], -f[real])
    assert np.array_equal(abs(x).to_float()[real], np.abs(f[real]))
    y = pn.array(0.5, c)
    assert np.array_equal((x < y)[real], (f < 0.5)[real])
    assert np.array_equal(np.maximum(x, y).to_float()[real], np.maximum(f, 0.5)[real])


# ---- NumPy protocols ----------------------------------------------------------------------------

def test_ufuncs_and_functions():
    x = pn.array([1.0, -2.0, 4.0])
    y = pn.array([0.5, 0.5, 0.5])
    for uf, op in ((np.add, "add"), (np.subtract, "sub"), (np.multiply, "mul"), (np.divide, "div")):
        assert uf(x, y).equal_bits(x._binary(op, y))
    assert np.square(x).equal_bits(x * x) and np.negative(x).equal_bits(-x)
    assert np.concatenate([x, y]).shape == (6,) and np.stack([x, y]).shape == (2, 3)
    assert np.where(x > 0, x, y).to_float().tolist() == [1.0, 0.5, 4.0]
    assert np.argsort(x).tolist() == [1, 0, 2] and np.array_equal(x, x.copy())
    with pytest.raises(TypeError, match="stage 4"):
        np.sum(x)
    with pytest.raises(TypeError, match="stage 7"):
        np.sin(x)
    with pytest.raises(TypeError):
        np.cumprod(x)                                          # not implemented at all


# ---- backends and stats ---------------------------------------------------------------------------

def test_using_hw_and_stats():
    hw = fake_hw(pn.P32)
    x = pn.array(np.linspace(-4, 4, 3000))
    pn.clear_stats()
    want = x * x - x / 3 + 1
    with pn.using(hw):
        got = x * x - x / 3 + 1
        s = pn.stats()
        assert s["elements"] == 4 * 3000
    assert got.equal_bits(want)
    assert pn.get_backend(pn.P32) is not hw
    assert pn.stats()["elements"] == 4 * 3000                 # the sw backend computed `want`
    with pn.using(hw):
        y = pn.array([1.0, 2.0], pn.P16) * 3                    # other format: sw, not the hw
    assert hw.stats()["elements"] == 4 * 3000 and y.to_float().tolist() == [3.0, 6.0]


def test_stats_flags():
    pn.clear_stats(pn.P16)
    one = pn.array([1.0], pn.P16)
    _ = one / 0
    _ = pn.from_bits([0x7FFF], pn.P16) * pn.from_bits([0x7FFF], pn.P16)
    s = pn.stats(pn.P16)
    assert (s["nar"], s["sat"], s["inexact"], s["elements"]) == (1, 1, 1, 2)


# ---- hypothesis: hardware path (fakes) == software path -------------------------------------------

@settings(max_examples=40, deadline=None)
@given(n=st.sampled_from([8, 16, 32]), name=st.sampled_from(["add", "sub", "mul", "div"]),
       data=st.data())
def test_hw_equals_sw(n, name, data):
    c = pn.PositConfig(n)
    size = data.draw(st.integers(0, 3000))
    a = data.draw(hnp.arrays(np.int64, size, elements=st.integers(0, (1 << n) - 1)))
    b = data.draw(hnp.arrays(np.int64, size, elements=st.integers(0, (1 << n) - 1)))
    x, y = pn.from_bits(a, c), pn.from_bits(b, c)
    want = x._binary(name, y)
    with pn.using(fake_hw(c)):
        got = x._binary(name, y)
        got_s = x._binary(name, pn.from_bits(b[:1] if size else [0], c))
    assert got.equal_bits(want)
    assert got_s.equal_bits(x._binary(name, pn.from_bits(b[:1] if size else [0], c)))


@settings(max_examples=60, deadline=None)
@given(hnp.arrays(np.float64, st.integers(1, 200), elements=st.floats(allow_nan=True, allow_infinity=True)))
def test_float_conversion_matches_ref(x):
    from positnp import ref
    c = pn.P32
    got = pn.array(x, c).bits.astype(np.int64)
    want = []
    for v in x.tolist():
        if v != v or v in (float("inf"), float("-inf")):
            want.append(c.nar)
        elif v == 0:
            want.append(0)
        else:
            num, den = v.as_integer_ratio()
            want.append(ref.encode(c, (num, -(den.bit_length() - 1))))
    assert got.tolist() == want


def test_rsub_is_negated_sub_exhaustive_p8():
    c = pn.P8
    a, b = np.meshgrid(np.arange(256), np.arange(256), indexing="ij")
    assert np.array_equal(vecops.op(c, "sub", b, a).bits, vecops.neg(c, vecops.op(c, "sub", a, b).bits))
