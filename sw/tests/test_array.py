"""PositArray v0 and float <-> posit conversion."""

import math

import numpy as np
import pytest

import positnp as pn
from positnp import ref
from positnp.array import float_to_bits
from positnp.backend_hw import PositVecHw
from positnp.fakes import FakeDma, FakeVecMmio
from positnp.ref import PositConfig
from positnp.udmabuf import FakeUDmaBuf

P16 = PositConfig(16)
P32 = PositConfig(32)


def test_float_roundtrip_posit16_all():
    for raw in range(1 << 16):
        f = ref.to_float(P16, raw)
        assert float_to_bits(P16, f) == (raw if not math.isnan(f) else P16.nar)


def test_float_rounding_ties_to_even():
    # posit<16,2> in [1, 2): 11 fraction bits; 1 + 2**-12 lies halfway between 1 (even) and 1 + 2**-11
    assert float_to_bits(P16, 1 + 2 ** -12) == 0x4000
    assert float_to_bits(P16, 1 + 3 * 2 ** -12) == 0x4002     # halfway 0x4001/0x4002 -> even
    assert float_to_bits(P16, 1 + 2 ** -12 + 2 ** -30) == 0x4001
    assert float_to_bits(P32, float("nan")) == P32.nar and float_to_bits(P32, float("inf")) == P32.nar
    assert float_to_bits(P32, 1e300) == P32.maxpos and float_to_bits(P32, 1e-300) == 1


def test_operators_sw():
    x = pn.PositArray.from_float([1.0, 2.5, -3.0], P32)
    assert np.array_equal((x * x + x).to_float(), [2.0, 8.75, 6.0])
    assert np.array_equal((x / 2).to_float(), [0.5, 1.25, -1.5])
    assert np.array_equal((x - x).to_float(), [0.0, 0.0, 0.0])
    with pytest.raises(TypeError):
        x + pn.PositArray.from_float([1.0, 2.0, 3.0], P16)


def test_backend_switch_matches_sw():
    mem = FakeUDmaBuf(1 << 18)
    vec = FakeVecMmio(P32)
    hw = PositVecHw(vec, FakeDma(mem, vec), mem)
    x = pn.PositArray.from_float(np.linspace(-4, 4, 33), P32)
    want = (x * x - x / 3).bits
    pn.set_backend(hw)
    try:
        got = (x * x - x / 3).bits
        assert vec.counters["elements"] == 3 * 33
    finally:
        pn.set_backend(None)
    assert np.array_equal(got, want)


def test_repr_and_indexing():
    x = pn.PositArray.from_float(np.arange(10.0), P16)
    assert "10 elements" in repr(x) and len(x) == 10
    assert x[3].to_float()[0] == 3.0
