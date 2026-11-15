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
    only_add = make(16, ops=("add",))              # missing operation: PositSw, counted in stats()
    assert only_add.array_op("div", [0x4800], [0x4000]).tolist() == [0x4800]
    assert only_add.stats()["fallback"] == 1
    assert hw.array_op("add", [], []).size == 0


# ---- stage 5 ------------------------------------------------------------------------------------

def test_stage5_ops_match_sw():
    hw = make(32)
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(5)
    a, b, c = (rnd.integers(0, 1 << 32, 700) for _ in range(3))
    s = int(rnd.integers(0, 1 << 32))
    assert np.array_equal(hw.array_op("sqrt", a, scalar=0), sw.array_op("sqrt", a))
    assert np.array_equal(hw.array_op("f2p", a, scalar=0), sw.array_op("f2p", a))
    assert np.array_equal(hw.array_op("p2f", a, scalar=0), sw.array_op("p2f", a))
    assert np.array_equal(hw.array_op("fma", a, b, c=s), sw.array_op("fma", a, b, c=s))          # OP_FMA
    assert np.array_equal(hw.array_op("fma", a, scalar=s, c=c), sw.array_op("fma", a, scalar=s, c=c))  # OP_AXPY
    assert hw.stats()["fallback"] == 0
    assert hw.read(regs.OP) == regs.OP_AXPY
    assert np.array_equal(hw.array_op("fma", a, b, c=c), sw.array_op("fma", a, b, c=c))          # 3 arrays: sw
    assert hw.stats()["fallback"] == 700


def test_stage5_absent_falls_back():
    from positnp.backend_hw import PositVecHw
    mem = FakeUDmaBuf(1 << 16)
    vec = FakeVecMmio(PositConfig(16), stage5=False)
    hw = PositVecHw(vec, FakeDma(mem, vec), mem)
    a = np.arange(1, 100)
    assert np.array_equal(hw.array_op("sqrt", a, scalar=0), PositSw(hw.config).array_op("sqrt", a))
    assert hw.stats()["fallback"] == 99 and vec.counters["elements"] == 0


# ---- several engines in one overlay: discovery, selection, close ------------------------------------

def _uio_tree(tmp_path, names):
    for i, nm in enumerate(names):
        (tmp_path / f"uio{i}").mkdir()
        (tmp_path / f"uio{i}" / "name").write_text(nm + "\n")
    return tmp_path


def test_vec_engines_found_by_name(tmp_path, monkeypatch):
    from positnp import backend_hw as bh
    monkeypatch.setattr(bh, "UIO_CLASS", _uio_tree(tmp_path, [
        "posit_dma_p8", "posit_vec_p8", "posit_vec_p32", "posit_dma_p32", "posit_vec_p16", "posit_dma_p16", "other"]))
    es = bh.vec_engines()
    assert [(e.name, e.dma, e.buf, e.n) for e in es] == [
        ("posit_vec_p32", "posit_dma_p32", "udmabuf-posit-p32", 32),
        ("posit_vec_p16", "posit_dma_p16", "udmabuf-posit-p16", 16),
        ("posit_vec_p8", "posit_dma_p8", "udmabuf-posit-p8", 8)]
    assert bh.select_engine(es, 16).n == 16
    with pytest.raises(PositAluError, match="posit_vec_p32, posit_vec_p16, posit_vec_p8"):
        bh.select_engine(es)                       # several: n is needed
    with pytest.raises(PositAluError, match="no posit<12>"):
        bh.select_engine(es, 12)


def test_vec_engines_legacy_overlay(tmp_path, monkeypatch):
    from positnp import backend_hw as bh
    monkeypatch.setattr(bh, "UIO_CLASS", _uio_tree(tmp_path, ["posit_vec", "posit_dma"]))
    es = bh.vec_engines()
    assert [(e.name, e.dma, e.buf, e.n) for e in es] == [("posit_vec", "posit_dma", "udmabuf-posit", None)]
    assert bh.select_engine(es) is es[0] and bh.select_engine(es, 32) is es[0]   # format checked after open
    monkeypatch.setattr(bh, "UIO_CLASS", tmp_path / "missing")
    with pytest.raises(PositAluError, match="overlay loaded"):
        bh.select_engine(bh.vec_engines())


def test_close_is_idempotent_and_blocks_use():
    hw = make()
    a = np.arange(10)
    assert hw.array_op("add", a, a).size == 10
    with hw:
        pass                                       # __exit__ closes
    hw.close()
    with pytest.raises(PositAluError, match="closed"):
        hw.array_op("add", a, a)


def test_engine_cfg():
    mem = FakeUDmaBuf(1 << 16)
    vec = FakeVecMmio(PositConfig(32), k=4, units=4)
    assert PositVecHw(vec, FakeDma(mem, vec), mem).engine_cfg() == {"k": 4, "units": 4, "lanes": 1}
    vec2 = FakeVecMmio(PositConfig(32), k=4, units=2, lanes=2, mem=mem)
    assert PositVecHw(vec2, None, mem).engine_cfg() == {"k": 4, "units": 2, "lanes": 2}


# ---- stage 6: engine with its own AXI4 master (MEM_* commands, no DMA) ---------------------------------

def make_master(n=32, buf_size=1 << 16, lazy=True, chunk=None):
    mem = FakeUDmaBuf(buf_size)
    vec = FakeVecMmio(PositConfig(n), ops=("add", "sub", "mul", "div"), mem=mem, lazy_mem=lazy)
    hw = PositVecHw(vec, None, mem)
    if chunk is not None:
        hw.MEM_CHUNK_BYTES = chunk
    return hw, vec, mem


@pytest.mark.parametrize("n", [8, 16, 32])
def test_master_ops_match_sw(n):
    hw, vec, mem = make_master(n)
    assert hw.master and hw.dma is None
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(n)
    size = 20_001                                   # several commands (small buffer)
    a, b, c = (rnd.integers(0, 1 << n, size) for _ in range(3))
    for name in ("add", "mul", "div", "sqrt"):
        kw = {} if name == "sqrt" else dict(b=b)
        assert np.array_equal(hw.array_op(name, a, **kw), sw.array_op(name, a, **kw)), name
    k = int(rnd.integers(0, 1 << n))
    for kw in (dict(b=b, c=k), dict(scalar=k, c=c), dict(b=b, c=c)):        # FMA, AXPY, FMA3
        assert np.array_equal(hw.array_op("fma", a, **kw), sw.array_op("fma", a, **kw)), sorted(kw)
    assert np.array_equal(hw.array_op("add", a, scalar=k), sw.array_op("add", a, scalar=k))
    assert hw.stats()["fallback"] == 0                                       # FMA3 in hardware
    assert len(vec.mem_log) >= 8 and all(cmd[1][0] % 4096 == 0 for cmd in vec.mem_log)
    assert vec.regs[regs.MEM_LO] == mem.phys_addr and vec.regs[regs.MEM_HI] == mem.phys_addr + mem.size


def test_master_reduce_matches_sw():
    hw, vec, mem = make_master(32)
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(5)
    a, b = (rnd.integers(0, 1 << 32, 30_000) for _ in range(2))
    for mode in ("sum", "dot", "sumsq"):
        assert hw.reduce(mode, a, b) == sw.reduce(mode, a, b), mode
    assert all(cmd[3] >> regs.MEM_CFG_WRITE & 1 == 0 for cmd in vec.mem_log)


def test_master_rejected_command_raises():
    hw, vec, mem = make_master(32)
    vec.regs[regs.MEM_HI] = vec.regs[regs.MEM_LO] + 64      # window smaller than the buffer
    with pytest.raises(PositAluError, match="outside the buffer"):
        hw.array_op("add", np.arange(1000), np.arange(1000))


@pytest.mark.parametrize("double", [True, False])
@pytest.mark.parametrize("chunk", [4096, 1 << 20])
def test_master_double_buffer(double, chunk):
    """Many chunks through both halves; the lazy fake runs each command at the wait, so any write
    into the half the engine still reads (or a start before the previous done) shows up here."""
    hw, vec, mem = make_master(32, buf_size=1 << 16, chunk=chunk)
    hw.double_buffer = double
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(chunk + double)
    a, b, c = (rnd.integers(0, 1 << 32, 10_007) for _ in range(3))
    def halves_used():
        starts = {cmd[1][0] for cmd in vec.mem_log}       # SRC_A of every command: half 0 or half 1
        vec.mem_log.clear()
        return len(starts)
    want = lambda arrays: 2 if double and hw._layout(arrays, a.size)[0] < a.size else 1   # noqa: E731
    for kw in (dict(b=b), dict(b=b, c=c)):
        name = "fma" if "c" in kw else "add"
        assert np.array_equal(hw.array_op(name, a, **kw), sw.array_op(name, a, **kw)), (name, double, chunk)
        assert halves_used() == want(len(kw) + 1), name
    assert hw.reduce("dot", a, b) == sw.reduce("dot", a, b)
    assert halves_used() == want(2)


def test_udmabuf_sync_writes_only_changes(tmp_path):
    """UDmaBuf._sync: persistent descriptors, unchanged attributes not rewritten, the trigger always."""
    from positnp.udmabuf import UDmaBuf
    for a in ("sync_offset", "sync_size", "sync_direction", "sync_for_device", "sync_for_cpu"):
        (tmp_path / a).write_text("")
    buf = UDmaBuf.__new__(UDmaBuf)
    buf.sys, buf.size, buf._fds, buf._last = tmp_path, 1 << 20, {}, {}
    writes = []
    import positnp.udmabuf as ub
    real = ub.os.pwrite
    ub.os.pwrite = lambda fd, data, off: (writes.append(data), real(fd, data, off))[1]
    try:
        buf.sync_for_device(0, 4096, 1)
        buf.sync_for_device(8192, 4096, 1)
        buf.sync_for_cpu(8192, 4096, 2)
    finally:
        ub.os.pwrite = real
        for fd in buf._fds.values():
            ub.os.close(fd)
    assert writes == [b"0", b"4096", b"1", b"1",     # first sync: everything
                      b"8192", b"1",                  # same size and direction: offset + trigger
                      b"2", b"1"]                     # same offset and size: direction + trigger
    assert (tmp_path / "sync_offset").read_text() == "8192" and (tmp_path / "sync_direction").read_text() == "2"


# ---- stage 7a: gemv / matmul on the quire (OP_XLOAD, OP_GEMV) -----------------------------------------

def make_gemv(n=32, buf_size=1 << 18, gemv_k=64, lazy=True, cols=4):
    mem = FakeUDmaBuf(buf_size)
    vec = FakeVecMmio(PositConfig(n), mem=mem, lazy_mem=lazy, gemv_k=gemv_k, gemv_cols=cols)
    return PositVecHw(vec, None, mem), vec, mem


@pytest.mark.parametrize("cols", [1, 4])
@pytest.mark.parametrize("n", [8, 16, 32])
@pytest.mark.parametrize("shape", [(1, 1, 1), (3, 5, 2), (7, 64, 3), (33, 9, 17), (2, 1, 40)])
def test_matmul_matches_sw(n, shape, cols):
    hw, vec, mem = make_gemv(n, cols=cols)
    assert hw.gemv_max_k == 64 and hw.gemv_cols == cols
    sw = PositSw(hw.config)
    m, k, nn = shape
    rnd = np.random.default_rng(m * 1000 + k * 10 + nn)
    a = rnd.integers(0, 1 << n, (m, k))
    b = rnd.integers(0, 1 << n, (k, nn))
    vec.mem_log.clear()
    assert np.array_equal(hw.matmul(a, b), sw.matmul(a, b)), shape
    ops = [cmd for cmd in vec.mem_log]
    assert len(ops) == 2 * -(-nn // cols)                   # one XLOAD and one GEMV per group of P columns
    assert np.array_equal(hw.gemv(a, b[:, 0]), sw.gemv(a, b[:, 0]))


def test_matmul_blocks_when_heap_small():
    hw, vec, mem = make_gemv(32, buf_size=1 << 15, gemv_k=64)   # heap: 16 KB = 4 blocks
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(3)
    a = rnd.integers(0, 1 << 32, (50, 60))                      # 12 KB alone: row blocks
    b = rnd.integers(0, 1 << 32, (60, 20))                      # B^T 5 KB: column blocks
    vec.mem_log.clear()
    assert np.array_equal(hw.matmul(a, b), sw.matmul(a, b))
    gemvs = [cmd for cmd in vec.mem_log if cmd[3] >> regs.MEM_CFG_WRITE & 1]
    assert len(gemvs) > 5 and {cmd[0] % 60 for cmd in gemvs} == {0}    # several row blocks per column group
    assert hw.heap.stats()["blocks"] == 0
    tiny, vec2, _ = make_gemv(32, buf_size=1 << 14, gemv_k=64)        # heap: 2 blocks, no gemv layout fits
    assert np.array_equal(tiny.matmul(a[:3], b[:, :2]), sw.matmul(a[:3], b[:, :2]))


def test_matmul_device_input_in_place():
    hw, vec, mem = make_gemv(16)
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(4)
    a = rnd.integers(0, 1 << 16, (20, 30))
    b = rnd.integers(0, 1 << 16, (30, 4))
    ad = hw.to_device(a)
    vec.mem_log.clear()
    assert np.array_equal(hw.matmul(ad, b), sw.matmul(a, b))
    gemv_src = {cmd[1][0] for cmd in vec.mem_log if cmd[3] >> regs.MEM_CFG_WRITE & 1}
    assert gemv_src == {mem.phys_addr + hw.heap.offset_of(ad)}     # GEMV read A where it is


def test_matmul_k_beyond_buffer_uses_dots():
    hw, vec, mem = make_gemv(32, gemv_k=16)
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(6)
    a = rnd.integers(0, 1 << 32, (3, 40))
    b = rnd.integers(0, 1 << 32, (40, 2))
    vec.mem_log.clear()
    assert np.array_equal(hw.matmul(a, b), sw.matmul(a, b))
    assert len(vec.mem_log) == 6                                   # 6 quire dots, no gemv (nothing written)
    assert not any(cmd[3] >> regs.MEM_CFG_WRITE & 1 for cmd in vec.mem_log)


def test_gemv_shape_error_raises():
    hw, vec, mem = make_gemv(32, gemv_k=16, lazy=False)
    hw.write(regs.OP, regs.OP_GEMV)
    hw.write(regs.GEMV_K, 4)
    hw.write(regs.MEM_WLEN, 3)
    with pytest.raises(PositAluError, match="gemv shape"):
        hw._mem_cmd(11, [0, 0, 0], 4096, False, False)


def test_positarray_matmul_on_fake():
    import positnp as pn
    hw, vec, mem = make_gemv(32)
    rnd = np.random.default_rng(7)
    A = pn.array(rnd.normal(size=(6, 5)))
    B = pn.array(rnd.normal(size=(5, 3)))
    want = (A @ B)                                       # PositSw
    x = pn.array(rnd.normal(size=5))
    with pn.using(hw):
        vec.mem_log.clear()
        got = A @ B
        assert len(vec.mem_log) == 2 and got.equal_bits(want)          # 3 columns of B: one pass of A
        assert np.matmul(A, B).equal_bits(want) and np.dot(A, B).equal_bits(want)
        assert (A @ x).shape == (6,) and (x @ B).shape == (3,)
        assert (A.to_device() @ B).equal_bits(want)
    assert (A @ x).equal_bits(pn.matmul(A, x))
