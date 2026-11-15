"""Device memory (no copies): DeviceHeap and PositVecHw / PositArray on fake hardware."""

import gc

import numpy as np
import pytest

import positnp as pn
from positnp.backend_hw import PositVecHw
from positnp.backend_sw import PositSw
from positnp.devmem import DeviceHeap
from positnp.fakes import FakeVecMmio
from positnp.ref import PositConfig
from positnp.udmabuf import FakeUDmaBuf


def make(n=32, buf_size=4 << 20):
    mem = FakeUDmaBuf(buf_size)
    vec = FakeVecMmio(PositConfig(n), ops=("add", "sub", "mul", "div"), mem=mem, lazy_mem=True)
    return PositVecHw(vec, None, mem), vec, mem


def test_heap_alloc_free_coalesce():
    mem = FakeUDmaBuf(1 << 20)
    h = DeviceHeap(mem, 64 << 10, (1 << 20) - (64 << 10))
    a, b, c = h.alloc(100), h.alloc(5000), h.alloc(4096)
    assert (a, b, c) == (64 << 10, (64 << 10) + 4096, (64 << 10) + 3 * 4096)     # 4 KB blocks, first fit
    h.free(b)
    assert h.alloc(8192) == b                                                   # hole reused
    h.free(a); h.free(b); h.free(c)
    assert h.stats()["used"] == 0 and h.stats()["largest_free"] == h.size       # coalesced back
    assert h.alloc(h.size + 1) is None


def test_empty_frees_when_last_view_dies():
    hw, vec, mem = make()
    x = hw.heap.empty(10_000, np.uint32)
    v = x.reshape(100, 100)[::2]                       # a view keeps the block alive
    del x
    gc.collect()
    assert hw.heap.stats()["blocks"] == 1
    del v
    gc.collect()
    assert hw.heap.stats()["blocks"] == 0


@pytest.mark.parametrize("n", [8, 16, 32])
def test_device_ops_no_copies(n):
    hw, vec, mem = make(n)
    sw = PositSw(hw.config)
    rnd = np.random.default_rng(n)
    size = 50_001
    a, b, c = (rnd.integers(0, 1 << n, size) for _ in range(3))
    ad, bd, cd = hw.to_device(a), hw.to_device(b), hw.to_device(c)
    assert hw.on_device(ad) and not hw.on_device(a)
    for name, kw in (("add", dict(b=bd)), ("div", dict(b=bd)), ("sqrt", {}), ("fma", dict(b=bd, c=cd)),
                     ("fma", dict(scalar=7, c=cd)), ("mul", dict(scalar=3))):
        vec.mem_log.clear()
        r = hw.array_op(name, ad, **kw)
        want = sw.array_op(name, a, **{k: (v if np.ndim(v) == 0 else np.asarray(v)) for k, v in kw.items()})
        assert np.array_equal(r, want), name
        assert hw.on_device(r) and hw.last_timing.get("device") and hw.last_timing["pack"] == 0.0, name
        assert len(vec.mem_log) == 1                                             # one command, whole array
        assert vec.mem_log[0][1][0] == mem.phys_addr + hw.heap.offset_of(ad)    # engine reads ad in place
    assert hw.reduce("dot", ad, bd) == sw.reduce("dot", a, b)


def test_positarray_chain_stays_on_device():
    hw, vec, mem = make(32)
    x = pn.array(np.linspace(-3, 3, 20_001))
    want = x * x - x / 3 + 1
    with pn.using(hw):
        xd = x.to_device()
        assert xd.on_device and not x.on_device
        vec.mem_log.clear()
        y = xd * xd - xd / 3 + 1
        assert y.on_device
        assert len(vec.mem_log) == 4                     # four commands, no staging copies
        assert all(cmd[1][0] - mem.phys_addr >= hw.staging_size for cmd in vec.mem_log)
        assert y.equal_bits(want)
        assert np.sum(y).equal_bits(np.sum(want))
        h = y.to_host()
        assert not h.on_device and h.equal_bits(y)
    del xd, y
    gc.collect()
    assert hw.heap.stats()["blocks"] == 0


def test_mixed_inputs_and_full_heap_fall_back():
    hw, vec, mem = make(32, buf_size=1 << 20)           # heap 512 KB
    sw = PositSw(hw.config)
    a = np.arange(30_000, dtype=np.uint32)
    ad = hw.to_device(a)
    r = hw.array_op("add", ad, a)                        # host b copied to a temporary device block
    assert hw.on_device(r) and np.array_equal(r, sw.array_op("add", a, a))
    big = np.arange(200_000, dtype=np.uint32)            # 800 KB does not fit the heap
    with pytest.raises(Exception, match="device memory full"):
        hw.to_device(big)
    hw.device_results = True
    r2 = hw.array_op("add", big, big)                    # no room: the copy path (host result)
    assert not hw.on_device(r2) and np.array_equal(r2, sw.array_op("add", big, big))


def test_unaligned_view_is_host():
    hw, vec, mem = make(32)
    ad = hw.to_device(np.arange(1000, dtype=np.uint32))
    sl = ad[1:]                                          # 4 B off: not 16-byte aligned -> copied
    assert not hw.on_device(sl)
    r = hw.array_op("add", sl, sl)
    assert np.array_equal(r, PositSw(hw.config).array_op("add", sl, sl))
