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
from positnp.backend_hw import PositVecHw, vec_engines
from positnp.backend_sw import PositSw

ENGINES = vec_engines()
if not ENGINES:
    pytest.skip("no posit_vec engine loaded (UIO posit_vec_p<n> / posit_dma_p<n>, u-dma-buf)", allow_module_level=True)

LENGTHS = (1, 2, 3, 1000, 4097, 1_000_001)


@pytest.fixture(scope="module", params=ENGINES, ids=lambda e: e.name)
def hw(request):
    """Every test runs on every engine of the loaded overlay (posit32_16_8_vec: three times)."""
    v = PositVecHw.open_engine(request.param)
    v.soft_reset()
    yield v
    v.close()


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
    idv = hw.read(regs.ID_VERSION)
    assert idv >> 16 == regs.ID and idv & 0xFFFF >= regs.VERSION_STREAM_MIN, f"ID_VERSION 0x{idv:08x}"
    if hw.engine.n is not None:
        assert hw.config.n == hw.engine.n, f"{hw.engine.name} computes {hw.config}"
    print(f"\n{hw.engine.name}: {hw}, {hw.engine_cfg()}, buffer {hw.buf.size >> 20} MB @ 0x{hw.buf.phys_addr:x}, {hw.chunk_elements()} elements/chunk")


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
        print(f"hw {name}, {size} elements: " + ", ".join(f"{k} {v * 1e3:.1f} ms" for k, v in t.items() if k != "engine_cycles"))


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


# ---- stage 5: sqrt, FMA / AXPY, float32 conversions ---------------------------------------------------

def _need(hw, *names):
    missing = [n for n in names if n not in hw.array_ops]
    if missing:
        pytest.skip(f"bitstream without {', '.join(missing)} (stage 5)")


def test_sqrt_matches_sw(hw, sw):
    _need(hw, "sqrt")
    n = hw.config.n
    a = np.arange(1 << n) if n <= 16 else np.random.default_rng(5).integers(0, 1 << n, 1_000_001)
    got, want = hw.array_op("sqrt", a), sw.array_op("sqrt", a)
    assert np.array_equal(got, want), f"sqrt: {np.count_nonzero(got != want)} mismatches"


@pytest.mark.parametrize("size", (1, 3, 1000, 1_000_001))
def test_fma_and_axpy_match_sw(hw, sw, size):
    _need(hw, "fma")
    rnd = np.random.default_rng(size + 11)
    a, b, y = (rnd.integers(0, 1 << hw.config.n, size) for _ in range(3))
    k = int(rnd.integers(0, 1 << hw.config.n))
    hw.clear_stats()
    for kw in (dict(b=b, c=k), dict(scalar=k, c=y), dict(scalar=k, c=k)):
        got, want = hw.array_op("fma", a, **kw), sw.array_op("fma", a, **kw)
        assert np.array_equal(got, want), f"fma {sorted(kw)}: {np.count_nonzero(got != want)} mismatches"
    assert hw.stats()["fallback"] == 0
    hw.array_op("fma", a, b, c=y)                  # three arrays: FMA3 (stage 6) or software, counted
    assert hw.stats()["fallback"] == (0 if hw.fma3 else size)


def test_float32_conversions_match_sw(hw, sw):
    _need(hw, "f2p", "p2f")
    special = np.array([0.0, -0.0, 1.0, -1.0, np.nan, np.inf, -np.inf, 1e-45, -1e-45, 1e-38, 3.4028235e38,
                        2.0 ** -120, 2.0 ** 120, 0.1], np.float32).view(np.uint32)
    f = np.concatenate([special, np.random.default_rng(13).integers(0, 1 << 32, 1_000_000).astype(np.uint32)])
    p = np.random.default_rng(14).integers(0, 1 << 32, 1_000_000)
    for name, x in (("f2p", f), ("p2f", p)):
        got, want = hw.array_op(name, x), sw.array_op(name, x)
        assert np.array_equal(got, want), f"{name}: {np.count_nonzero(got != want)} mismatches"


def test_stage5_positarray_on_hw(hw):
    _need(hw, "sqrt", "fma", "f2p", "p2f")
    import positnp as pn
    f = np.random.default_rng(15).standard_normal(200_001).astype(np.float32)
    want = [pn.array(f, hw.config)]
    want += [np.sqrt(abs(want[0])), pn.axpy(0.5, want[0], want[0]), pn.fma(want[0], want[0], -1.0)]
    want_f = want[0].to_float32()
    hw.clear_stats()
    with pn.using(hw):
        x = pn.array(f, hw.config)
        got = [x, np.sqrt(abs(x)), pn.axpy(0.5, x, x), pn.fma(x, x, -1.0)]
        got_f = x.to_float32()
    assert all(g.equal_bits(w) for g, w in zip(got, want))
    assert np.array_equal(got_f.view(np.uint32), want_f.view(np.uint32))
    assert hw.stats()["fallback"] == 0


def test_stage5_throughput(hw):
    _need(hw, "sqrt", "fma", "f2p", "p2f")
    size = 1 << 22
    rows = [(f"hw {name}", hw.time_array(name, size)) for name in ("sqrt", "fma", "axpy", "f2p", "p2f")]
    f = np.random.default_rng(0).random(size, dtype=np.float32)
    t0 = time.perf_counter()
    np.sqrt(f)
    rows.append(("numpy float32 sqrt", size / (time.perf_counter() - t0)))
    print("\n" + "\n".join(f"{k:>20}: {v / 1e6:9.3f} M elem/s" for k, v in rows))


# ---- several engines in one overlay ---------------------------------------------------------------------

def test_engines_side_by_side():
    """open_all(): every engine at once, interleaved calls, each bit for bit with PositSw."""
    if len(ENGINES) < 2:
        pytest.skip("overlay with one engine")
    engines = PositVecHw.open_all()
    try:
        assert sorted(engines) == sorted(e.n for e in ENGINES if e.n)
        rnd = np.random.default_rng(21)
        for _ in range(3):
            for n, hw in engines.items():
                a = rnd.integers(0, 1 << n, 100_000)
                b = rnd.integers(0, 1 << n, 100_000)
                assert np.array_equal(hw.array_op("mul", a, b), PositSw(hw.config).array_op("mul", a, b)), hw.engine.name
    finally:
        for hw in engines.values():
            hw.close()


# ---- pn.board on the loaded application (no reload) ---------------------------------------------------

def test_board_open_and_describe():
    import positnp as pn
    try:
        name = pn.board.active()
    except pn.board.BoardError as e:
        pytest.skip(f"xmutil not usable here: {e}")
    app = next((a for a in pn.board.apps() if a.name == name), None)
    try:
        hw = pn.board.open()
        assert len(hw) == len(ENGINES)
        if app is not None and app.engines:
            assert sorted(hw) == sorted(e["n"] for e in app.engines if e.get("n")) or None in [e.get("n") for e in app.engines]
        print("\n" + pn.board.describe())
        for n, h in hw.items():
            assert pn.get_backend(h.config) is h
            x = pn.array([1.0, 2.0, 3.0], h.config)
            assert (x * x).to_float().tolist() == [1.0, 4.0, 9.0]
    finally:
        pn.board.close()


# ---- stage 6: engines with their own AXI4 master -------------------------------------------------------

def _need_master(hw):
    if not hw.master:
        pytest.skip("engine without its own AXI4 master (stage 6, build.tcl -io master)")


@pytest.mark.parametrize("size", (1, 3, 1000, 1_000_001))
def test_master_fma3_and_inplace(hw, sw, size):
    _need_master(hw)
    rnd = np.random.default_rng(size + 31)
    a, b, c = (rnd.integers(0, 1 << hw.config.n, size) for _ in range(3))
    hw.clear_stats()
    got = hw.array_op("fma", a, b, c=c)                              # OP_FMA3: three read streams
    assert np.array_equal(got, sw.array_op("fma", a, b, c=c))
    assert hw.stats()["fallback"] == 0
    assert np.array_equal(hw.array_op("add", a, b), sw.array_op("add", a, b))   # result over a (in place)


def test_master_rejects_outside_buffer(hw):
    _need_master(hw)
    lo, hi = hw.read(regs.MEM_LO), hw.read(regs.MEM_HI)
    try:
        hw.write(regs.MEM_HI, lo + 64)                                # window of 64 B
        with pytest.raises(Exception, match="outside the buffer"):
            hw.array_op("add", np.arange(1000), np.arange(1000))
    finally:
        hw.write(regs.MEM_HI, hi)
    assert hw.array_op("add", np.arange(10), np.arange(10)).size == 10


def test_master_throughput(hw, sw):
    _need_master(hw)
    size = 1 << 22
    rows = []
    for name in ("add", "div", "sqrt", "fma", "fma3", "axpy"):
        rows.append((f"hw {name}", hw.time_array(name, size), dict(hw.last_timing)))
    f = np.random.default_rng(0).random(size, dtype=np.float32)
    t0 = time.perf_counter()
    _ = f + f
    np_add = size / (time.perf_counter() - t0)
    print("")
    for k, v, t in rows:
        print(f"{k:>12}: {v / 1e6:8.3f} M elem/s | " +
              ", ".join(f"{p} {t[p] * 1e3:.1f} ms" for p in ("pack", "sync_device", "dma", "sync_cpu", "copy_out")) +
              f" | engine {t.get('engine_cycles', 0) / size:.2f} cycles/elem")
    print(f"{'numpy f32 add':>12}: {np_add / 1e6:8.3f} M elem/s")


def test_master_double_buffer_gain(hw):
    """add with one buffer (copy, run, copy) and with two halves for several chunk sizes: every phase.

    Phases overlap with double buffering, so 'engine wait' is the time the CPU had nothing to do."""
    _need_master(hw)
    size = 1 << 22
    rows = []
    chunk0 = hw.MEM_CHUNK_BYTES
    try:
        for double, chunk in ((False, None), (True, 256 << 10), (True, 1 << 20), (True, 4 << 20), (True, 8 << 20)):
            hw.double_buffer = double
            if chunk:
                hw.MEM_CHUNK_BYTES = chunk
            hw.time_array("add", size)                          # warm-up: touch every page of this layout
            rows.append((double, chunk, hw.time_array("add", size), dict(hw.last_timing)))
    finally:
        hw.double_buffer = True
        hw.MEM_CHUNK_BYTES = chunk0
    print("")
    for double, chunk, v, t in rows:
        label = f"double {chunk >> 10:5d} KB" if double else "single        "
        print(f"{label}: {v / 1e6:7.2f} M elem/s | total {t['total'] * 1e3:6.1f} ms: copy in {t['pack'] * 1e3:5.1f}, "
              f"sync dev {t['sync_device'] * 1e3:5.1f}, engine wait {t['dma'] * 1e3:5.1f}, "
              f"sync cpu {t['sync_cpu'] * 1e3:5.1f}, copy out {t['copy_out'] * 1e3:5.1f} ms")
    if size >= 1 << 20:                                      # on the board (not the small fake run)
        assert max(r[2] for r in rows[1:]) > rows[0][2], "double buffering slower than a single buffer"
