"""PositArray: operators, broadcasting, level 0, NaR semantics, NumPy protocols, backends, stats."""

import math

import numpy as np
import pytest

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


def test_rsub_is_negated_sub_exhaustive_p8():
    c = pn.P8
    a, b = np.meshgrid(np.arange(256), np.arange(256), indexing="ij")
    assert np.array_equal(vecops.op(c, "sub", b, a).bits, vecops.neg(c, vecops.op(c, "sub", a, b).bits))


# ---- quire reductions (stage 4) ----------------------------------------------------------------

from fractions import Fraction  # noqa: E402

from positnp import ref  # noqa: E402


def _val(c, bits):
    d = ref.decode(c, int(bits))
    return Fraction(d[0]) * Fraction(2) ** d[1]


def _neighbours(c, r):
    """Values of r and the posits just below and above it (in posit order), skipping NaR."""
    s = int(vecops.to_signed(c, [r])[0])
    out = []
    for t in (s - 1, s, s + 1):
        b = t & c.mask
        if b != c.nar and -(1 << (c.n - 1)) < t < (1 << (c.n - 1)):
            out.append(_val(c, b))
    return out


def test_sum_is_exact():
    x = pn.array([1e8, 1.0, -1e8])
    assert float(np.sum(x)) == 1.0                                   # float32 / naive posit: 0
    assert float(np.sum(np.array([1e8, 1.0, -1e8], np.float32))) == 0.0
    rnd = np.random.default_rng(0)
    a = pn.from_bits(rnd.integers(0, 1 << 32, 2000))
    a = pn.from_bits(np.where(a.isnar(), 0, a.bits))
    exact = sum(_val(pn.P32, b) for b in a.bits.tolist())
    r = int(np.sum(a).bits)
    assert r == ref.quire_round(pn.P32, int(exact * (1 << pn.P32.quire_frac)))[0]


def test_sum_axis_and_dot():
    a = pn.array(np.arange(12.0).reshape(3, 4))
    assert np.array_equal(np.sum(a, axis=0).to_float(), [12, 15, 18, 21])
    assert np.array_equal(a.sum(axis=1).to_float(), [6, 22, 38])
    v = pn.array([1.0, 2.0, 3.0])
    assert float(np.dot(v, v)) == 14.0 and float(v.dot([1, 1, 1])) == 6.0
    with pytest.raises(TypeError, match="stage 7"):
        np.dot(a, a)


@pytest.mark.parametrize("c", [pn.P16, pn.P32])
def test_mean_and_norm_correctly_rounded(c):
    rnd = np.random.default_rng(c.n)
    for size in (1, 3, 10, 257):
        x = pn.array(rnd.standard_normal(size) * 10, c)
        exact_sum = sum(_val(c, b) for b in x.bits.tolist())
        m = int(np.mean(x).bits)
        target = exact_sum / size
        assert abs(_val(c, m) - target) == min(abs(v - target) for v in _neighbours(c, m))
        sq = sum(_val(c, b) ** 2 for b in x.bits.tolist())
        r = int(np.linalg.norm(x).bits)
        vals = _neighbours(c, r)
        v = _val(c, r)
        lo = max((u for u in vals if u < v), default=None)
        hi = min((u for u in vals if u > v), default=None)
        assert lo is None or ((lo + v) / 2) ** 2 <= sq        # sqrt(sq) is not closer to the posit below
        assert hi is None or ((v + hi) / 2) ** 2 >= sq        # nor to the one above


def test_reductions_hw_equals_sw_and_quire():
    c = pn.P32
    x = pn.array(np.random.default_rng(5).standard_normal(3000), c)
    y = pn.array(np.random.default_rng(6).standard_normal(3000), c)
    want = (np.sum(x), np.dot(x, y), np.mean(x), np.linalg.norm(x), pn.quire(x, y))
    hw = fake_hw(c)
    with pn.using(hw):
        got = (np.sum(x), np.dot(x, y), np.mean(x), np.linalg.norm(x), pn.quire(x, y))
        assert hw.stats()["elements"] == 4 * 3000 + 3000
    for g, w in zip(got[:4], want[:4]):
        assert g.equal_bits(w)
    assert got[4] == want[4] and isinstance(got[4], Fraction)


def test_reduction_nar():
    x = pn.array([1.0, np.nan, 2.0])
    assert np.isnan(float(np.sum(x))) and np.isnan(float(np.mean(x))) and np.isnan(float(np.linalg.norm(x)))
    assert math.isnan(pn.quire(x))


# ---- stage 5: sqrt, fma, float32 ---------------------------------------------------------------

def test_sqrt_matches_ref_and_nar():
    c = pn.P16
    x = pn.from_bits(np.arange(1 << 16), c)
    r = np.sqrt(x)
    want = [ref.sqrt(c, v)[0] for v in range(1 << 16)]
    assert r.bits.astype(np.int64).tolist() == want
    assert np.isnan(float(np.sqrt(pn.array(-4.0)))) and float(np.sqrt(pn.array(9.0))) == 3.0


def test_fma_single_rounding_and_routes():
    c = pn.P32
    rnd = np.random.default_rng(3)
    a, b, y = (pn.array(rnd.standard_normal(2000) * 1e3, c) for _ in range(3))
    s = pn.array(0.75, c)
    want = [ref.fma(c, int(p), int(q), int(r))[0] for p, q, r in zip(a.bits, b.bits, y.bits)]
    assert pn.fma(a, b, y).bits.astype(np.int64).tolist() == want
    assert (pn.fma(a, b, y).bits != (a * b + y).bits).any()          # single rounding differs somewhere
    hw = fake_hw(c)
    with pn.using(hw):
        assert pn.fma(a, b, 1.5).equal_bits(pn.fma(a, b, pn.array(1.5, c)))
        got_axpy = pn.axpy(s, a, y)                                  # OP_AXPY, one stream pass
        assert hw.stats()["fallback"] == 0
        got3 = pn.fma(a, b, y)                                       # three arrays: PositSw
        assert hw.stats()["fallback"] == 2000
    assert got_axpy.equal_bits(pn.fma(a, s, y)) and got3.bits.astype(np.int64).tolist() == want


def test_float32_conversions():
    f = np.array([0.0, -0.0, 1.0, -2.5, np.nan, np.inf, -np.inf, 1e-45, 3.4e38, 1e-38, 0.1], np.float32)
    x = pn.array(f)
    assert x.bits[4] == x.bits[5] == x.bits[6] == pn.P32.nar
    back = x.to_float32()
    assert back.dtype == np.float32 and np.isnan(back[4])
    assert back[:4].tolist() == [0.0, 0.0, 1.0, -2.5] and back[10] == np.float32(0.1)
    hw = fake_hw(pn.P32)
    rnd = np.random.default_rng(9)
    g = rnd.integers(0, 1 << 32, 5000, dtype=np.uint64).astype(np.uint32).view(np.float32)
    want_p = pn.array(g)
    want_f = want_p.to_float32()
    with pn.using(hw):
        got_p = pn.array(g)
        got_f = got_p.to_float32()
        assert hw.stats()["elements"] == 10000                       # F2P and P2F ran on the "hardware"
    assert got_p.equal_bits(want_p) and np.array_equal(got_f.view(np.uint32), want_f.view(np.uint32))
    assert x.astype(np.float64).dtype == np.float64
