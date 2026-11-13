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


def check(hw, sw, name, a, b=None, scalar=None, sample=20_000):
    got = hw.array_op(name, a, b, scalar=scalar)
    assert got.shape == np.shape(a)
    idx = np.arange(a.size) if a.size <= sample else np.random.default_rng(a.size).choice(a.size, sample, replace=False)
    want = sw.array_op(name, a[idx], None if b is None else b[idx], scalar=scalar)
    bad = np.nonzero(got[idx] != want)[0]
    assert bad.size == 0, f"{name}: {bad.size} mismatches, first at {idx[bad[0]]}: " \
                          f"0x{int(got[idx][bad[0]]):x} vs 0x{int(want[bad[0]]):x}"


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


def test_throughput(hw, sw):
    size = 1 << 22
    rows = []
    for name in ("add", "div"):
        rows.append((f"hw {name}", hw.time_array(name, size)))
    a = np.random.default_rng(0).integers(0, 1 << hw.config.n, 20_000)
    t0 = time.perf_counter()
    sw.array_op("add", a, a)
    rows.append(("sw add (ref.py)", a.size / (time.perf_counter() - t0)))
    f = np.random.default_rng(0).random(size, dtype=np.float32)
    t0 = time.perf_counter()
    _ = f + f
    rows.append(("numpy float32 add", size / (time.perf_counter() - t0)))
    print("\n" + "\n".join(f"{k:>20}: {v / 1e6:9.3f} M elem/s" for k, v in rows))
