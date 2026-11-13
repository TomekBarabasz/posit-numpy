"""Stage 2 on the KV260: arrays through AXI DMA (PositVecHw) against ref.py, bit for bit.

Runs only where the posit<n>_vec overlay is loaded (UIO posit_vec and posit_dma, u-dma-buf
udmabuf-posit); elsewhere every test is skipped. From sw/, after
scripts/build_udmabuf.sh (once) and scripts/install_overlay.sh posit32_vec:
    sudo python3 -m pytest -v -s tests/test_board_vec.py
"""

import time
from pathlib import Path

import numpy as np
import pytest

from positnp import ref, regs
from positnp.backend_hw import DMA_NAME, VEC_NAME, PositVecHw, find_uio
from positnp.backend_sw import PositSw
from positnp.udmabuf import DEFAULT_NAME, SYS_CLASS

if find_uio(VEC_NAME) is None or find_uio(DMA_NAME) is None or not (SYS_CLASS / DEFAULT_NAME).exists():
    pytest.skip("stage 2 overlay not loaded (UIO posit_vec / posit_dma, u-dma-buf)", allow_module_level=True)

LENGTHS = (1, 2, 3, 1000, 4097, 1_000_001)


@pytest.fixture(scope="module")
def hw():
    v = PositVecHw.open()
    v.soft_reset()
    return v


@pytest.fixture(scope="module")
def sw(hw):
    return PositSw(hw.config)


def check(hw, sw, name, a, b=None, scalar=None):
    """Every element against PositSw (vecops, pinned to PositRef by the shared digests)."""
    got = hw.array_op(name, a, b, scalar=scalar)
    assert got.shape == np.shape(a)
    want = sw.array_op(name, a, b, scalar=scalar)
    bad = np.nonzero(got != want)[0]
    assert bad.size == 0, f"{name}: {bad.size} mismatches, first at {bad[0]}: " \
                          f"0x{int(got[bad[0]]):x} vs 0x{int(want[bad[0]]):x}"


def test_id_and_config(hw):
    assert hw.read(regs.ID_VERSION) == regs.ID << 16 | regs.VERSION_VEC
    print(f"\n{hw}, buffer {hw.buf.size >> 20} MB @ 0x{hw.buf.phys_addr:x}, {hw.chunk_elements()} elements/chunk")


@pytest.mark.parametrize("name", ref.OPS)
@pytest.mark.parametrize("size", LENGTHS)
def test_array_matches_ref(hw, sw, name, size):
    rnd = np.random.default_rng(size)
    a = rnd.integers(0, 1 << hw.config.n, size)
    b = rnd.integers(0, 1 << hw.config.n, size)
    check(hw, sw, name, a, b)


@pytest.mark.parametrize("name", ref.OPS)
def test_array_scalar(hw, sw, name):
    a = np.random.default_rng(7).integers(0, 1 << hw.config.n, 10_000)
    check(hw, sw, name, a, scalar=ref.encode(hw.config, (3, -1)))   # 1.5


def test_two_calls_and_counters(hw):
    one = ref.encode(hw.config, (1, 0))
    hw.clear_stats()
    r1 = hw.array_op("div", np.array([one, one]), np.array([0, one]))
    r2 = hw.array_op("mul", np.array([hw.config.maxpos]), np.array([hw.config.maxpos]))
    assert r1.tolist() == [hw.config.nar, one] and r2.tolist() == [hw.config.maxpos]
    s = hw.stats()
    assert (s["nar"], s["sat"], s["inexact"], s["elements"]) == (1, 1, 1, 3)


def test_positarray_on_hw(hw):
    import positnp as pn
    x = pn.array(np.linspace(-3, 3, 100_001), hw.config)
    want = x * x - x / 3 + 1
    with pn.using(hw):
        got = x * x - x / 3 + 1
    assert got.equal_bits(want)


def test_throughput(hw, sw):
    size = 1 << 22
    rows = []
    phases = {}
    for name in ("add", "div"):
        rows.append((f"hw {name}", hw.time_array(name, size)))
        phases[name] = hw.last_timing
    a = np.random.default_rng(0).integers(0, 1 << hw.config.n, size)
    t0 = time.perf_counter()
    sw.array_op("add", a, a)
    rows.append(("sw add (vecops)", a.size / (time.perf_counter() - t0)))
    f = np.random.default_rng(0).random(size, dtype=np.float32)
    t0 = time.perf_counter()
    _ = f + f
    rows.append(("numpy float32 add", size / (time.perf_counter() - t0)))
    print("\n" + "\n".join(f"{k:>20}: {v / 1e6:9.3f} M elem/s" for k, v in rows))
    for name, t in phases.items():
        print(f"hw {name}, {size} elements: " + ", ".join(f"{k} {v * 1e3:.1f} ms" for k, v in t.items()))


# ---- stage 4: quire ---------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["sum", "dot", "sumsq"])
@pytest.mark.parametrize("size", (1, 2, 3, 1000, 1_000_001))
def test_reduce_matches_sw(hw, sw, mode, size):
    if not hw.has_quire():
        pytest.skip("bitstream without quire (stage 4)")
    rnd = np.random.default_rng(size + len(mode))
    a = rnd.integers(0, 1 << hw.config.n, size)
    b = rnd.integers(0, 1 << hw.config.n, size)
    assert hw.reduce(mode, a, b) == sw.reduce(mode, a, b)


def test_quire_cancellation_on_hw(hw):
    if not hw.has_quire():
        pytest.skip("bitstream without quire (stage 4)")
    import positnp as pn
    with pn.using(hw):
        x = pn.array([1e8, 1.0, -1e8], hw.config)
        assert float(np.sum(x)) == 1.0
        v = pn.array(np.random.default_rng(1).standard_normal(100_000), hw.config)
        assert np.linalg.norm(v).equal_bits(_sw_norm(v))


def _sw_norm(v):
    import positnp as pn
    with pn.using(None):
        return np.linalg.norm(v)


def test_reduce_throughput(hw):
    if not hw.has_quire():
        pytest.skip("bitstream without quire (stage 4)")
    size = 1 << 22
    a = np.random.default_rng(0).integers(0, 1 << hw.config.n, size).astype(hw.out_dtype)
    t0 = time.perf_counter()
    hw.reduce("dot", a, a)
    dt = time.perf_counter() - t0
    f = np.random.default_rng(0).random(size, dtype=np.float32)
    t1 = time.perf_counter()
    np.dot(f, f)
    print(f"\nquire dot hw: {size / dt / 1e6:.2f} M elem/s; numpy float32 dot: {size / (time.perf_counter() - t1) / 1e6:.1f} M elem/s")
