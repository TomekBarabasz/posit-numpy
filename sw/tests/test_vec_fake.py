"""PositVecHw driver (stage 2) against FakeVecMmio + FakeDma + FakeUDmaBuf: runs on any PC."""

import numpy as np
import pytest

from positnp import ref, regs
from positnp import dma as d
from positnp.backend_hw import PositAluError, PositVecHw
from positnp.backend_sw import PositSw
from positnp.dma import DmaError
from positnp.fakes import FakeDma, FakeVecMmio
from positnp.ref import PositConfig
from positnp.udmabuf import FakeUDmaBuf


def make(n=32, buf_size=1 << 20, fault=None, ops=("add", "sub", "mul", "div")):
    mem = FakeUDmaBuf(buf_size)
    vec = FakeVecMmio(PositConfig(n), ops=ops)
    hw = PositVecHw(vec, FakeDma(mem, vec, fault), mem)
    hw.dma.timeout_s = 0.05
    return hw


@pytest.mark.parametrize("n", [8, 16, 32])
@pytest.mark.parametrize("name", ref.OPS)
def test_arrays_match_sw(n, name):
    hw = make(n)
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(n)
    for size in (1, 2, 3, 257):
        a = rnd.integers(0, 1 << n, size)
        b = rnd.integers(0, 1 << n, size)
        assert np.array_equal(hw.array_op(name, a, b), sw.array_op(name, a, b))


def test_scalar_and_shape():
    hw = make(16)
    sw = PositSw(hw.config)
    a = np.random.default_rng(1).integers(0, 1 << 16, (4, 5))
    r = hw.array_op("mul", a, scalar=0x4800)
    assert r.shape == (4, 5) and r.dtype == np.uint16
    assert np.array_equal(r, sw.array_op("mul", a, scalar=0x4800))
    assert hw.read(regs.STREAM_CFG) == 1 << regs.STREAM_CFG_SCALAR_B


def test_chunking_and_cache_protocol():
    hw = make(32, buf_size=3 * 4096)          # tiny buffer: several DMA rounds
    k = hw.chunk_elements()
    assert k == (3 * 4096 - 4096) // 12
    a = np.arange(5 * k + 7)
    b = np.arange(5 * k + 7)[::-1]
    assert np.array_equal(hw.array_op("add", a, b), PositSw(hw.config).array_op("add", a, b))
    assert len(hw.dma.mmio.transfers) == 6
    # every round: device sync over the input and over the result before DMA, cpu sync over the result after
    syncs = hw.buf.syncs
    assert [s[0] for s in syncs] == ["device", "device", "cpu"] * 6
    rounds = zip(syncs[::3], syncs[1::3], syncs[2::3])
    for (src, in_len, dst, out_len), (dev_in, dev_out, cpu) in zip(hw.dma.mmio.transfers, rounds):
        assert src == 0 and dst % 4096 == 0 and dst >= in_len
        assert dev_in[1:] == (0, in_len) and dev_out[1:] == (dst, out_len)
        assert cpu[1:] == (dst, out_len)
    assert set(hw.last_timing) == {"pack", "sync_device", "dma", "sync_cpu", "copy_out", "total"}


def test_counters_and_elements():
    hw = make(16)
    hw.clear_stats()
    one, maxpos = 0x4000, 0x7FFF
    hw.array_op("mul", [maxpos, one, maxpos], [maxpos, one, one])
    hw.array_op("div", [one], [0])
    s = hw.stats()
    assert (s["nar"], s["sat"], s["inexact"], s["elements"]) == (1, 1, 1, 4)


def test_single_ops_still_work():
    hw = make(32)
    assert hw.op("div", 0x40000000, 0x4C000000) == ref.op(hw.config, "div", 0x40000000, 0x4C000000)


def test_dma_errors():
    with pytest.raises(DmaError, match="DMASlvErr"):
        make(32, fault="slverr").array_op("add", [1], [2])
    with pytest.raises(DmaError, match="no completion"):
        make(32, fault="hang").array_op("add", [1], [2])


def test_rejects_stage1_engine():
    from positnp.backend_hw import FakeAluMmio
    mem = FakeUDmaBuf(1 << 16)
    with pytest.raises(PositAluError, match="stream"):
        PositVecHw(FakeAluMmio(PositConfig(32)), FakeDma(mem, FakeVecMmio()), mem)


def test_input_validation():
    hw = make(16)
    with pytest.raises(TypeError):
        hw.array_op("add", np.array([1.5]), np.array([1.0]))
    with pytest.raises(ValueError):
        hw.array_op("add", [1, 2], [1])
    with pytest.raises(PositAluError):
        make(16, ops=("add",)).array_op("div", [1], [1])
    assert hw.array_op("add", [], []).size == 0
