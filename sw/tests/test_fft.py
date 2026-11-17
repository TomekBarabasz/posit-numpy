"""Stage 7: FFT on posits (positnp.fft) - exact DFT blocks, four-step accuracy, NumPy-like API."""

import numpy as np
import pytest

import positnp as pn
from positnp import fft as F, ref, regs
from positnp.backend_hw import PositVecHw
from positnp.fakes import FakeVecMmio
from positnp.ref import PositConfig
from positnp.udmabuf import FakeUDmaBuf


@pytest.mark.parametrize("c", [pn.P8, pn.P16, pn.P32])
@pytest.mark.parametrize("N", [1, 2, 3, 5, 8, 12])
def test_exact_dft_is_one_rounding(c, N):
    """exact=True: every output == the quire sum over the posit twiddles, rounded once."""
    rnd = np.random.default_rng(N)
    x = pn.array(rnd.standard_normal(N), c)
    y = pn.array(rnd.standard_normal(N), c)
    X = pn.fft.fft((x, y), exact=True)
    M = F._dft_matrix(c, N)
    row = np.concatenate([x.bits, y.bits]).astype(np.int64)
    for k in range(2 * N):
        q, nar = ref.quire_accumulate(c, "dot", row.tolist(), M[:, k].tolist())
        want = ref.quire_round(c, q, nar)[0]
        got = int((X.real if k < N else X.imag).bits[k % N])
        assert got == want, (N, k)


def _rel_err(X, ref_c):
    return np.max(np.abs(X.to_complex() - ref_c)) / max(np.max(np.abs(ref_c)), 1e-300)


@pytest.mark.parametrize("c,tol", [(pn.P16, 2e-3), (pn.P32, 3e-8)])
@pytest.mark.parametrize("N", [64, 96, 128, 1000, 1024])
def test_four_step_accuracy(c, tol, N):
    rnd = np.random.default_rng(N)
    x = pn.array(rnd.standard_normal(N), c)
    X = pn.fft.fft(x)
    assert X.shape == (N,) and F.levels(N) == (1 if N <= 64 else 2)
    assert _rel_err(X, np.fft.fft(x.to_float())) < tol
    if N <= F.EXACT_MAX and N <= 128:
        Xe = pn.fft.fft(x, exact=True)
        assert _rel_err(Xe, np.fft.fft(x.to_float())) <= _rel_err(X, np.fft.fft(x.to_float())) + tol / 4


def test_inverse_rfft_batch_and_numpy_dispatch():
    c = pn.P32
    rnd = np.random.default_rng(1)
    z = rnd.standard_normal(128) + 1j * rnd.standard_normal(128)
    X = pn.fft.fft(z, config=c)
    x2 = pn.fft.ifft(X)
    assert np.max(np.abs(x2.to_complex() - pn.array(z.real, c).to_float() - 1j * pn.array(z.imag, c).to_float())) < 1e-7
    r = pn.array(rnd.standard_normal(96), c)
    R = pn.fft.rfft(r)
    assert R.shape == (49,) and np.allclose(R.to_complex(), np.fft.rfft(r.to_float()), atol=1e-6)
    re, im = np.fft.fft(r)                                           # __array_function__, unpacks
    assert re.equal_bits(pn.fft.fft(r).real) and im.equal_bits(pn.fft.fft(r).imag)
    B = pn.array(rnd.standard_normal((3, 64)), c)
    XB = pn.fft.fft(B)
    assert XB.shape == (3, 64)
    for i in range(3):
        assert XB.real[i].equal_bits(pn.fft.fft(B[i]).real)
    XT = pn.fft.fft(B.T, axis=0)                                    # other axis
    assert XT.real.T.equal_bits(XB.real)
    P = pn.fft.fft(r, n=128)                                         # zero padding like numpy
    assert np.allclose(P.to_complex(), np.fft.fft(r.to_float(), 128), atol=1e-6)
    O = np.fft.fft(r, norm="ortho")                                  # norm= now supported
    assert np.allclose(O.to_complex(), np.fft.fft(r.to_float(), norm="ortho"), atol=1e-6)
    with pytest.raises(ValueError, match="norm"):
        np.fft.fft(r, norm="bogus")
    with pytest.raises(ValueError, match="small prime"):
        pn.fft.fft(pn.array(np.ones(4099), c))                       # prime > EXACT_MAX


def test_special_values():
    c = pn.P16
    X = pn.fft.fft(pn.array(np.ones(64), c))                         # DC only: exact
    assert float(X.real[0]) == 64.0 and not X.real.bits[1:].any() and not X.imag.bits.any()
    d = np.zeros(128)
    d[0] = 1.0
    X = pn.fft.fft(pn.array(d, c))                                   # impulse: all ones, exact
    assert np.all(X.real.to_float() == 1.0) and not X.imag.bits.any()
    n = pn.array(np.r_[np.nan, np.zeros(15)], c)
    assert np.isnan(pn.fft.fft(n).real.to_float()).all()             # NaR spreads to every output


def test_fft_on_fake_engine_matches_software():
    mem = FakeUDmaBuf(1 << 20)
    vec = FakeVecMmio(PositConfig(16), mem=mem, gemv_k=4096, gemv_cols=4, fft_max_s=0)   # no FFT core: blocks
    hw = PositVecHw(vec, None, mem)
    x = pn.array(np.random.default_rng(3).standard_normal(256), pn.P16)
    want = pn.fft.fft(x)
    with pn.using(hw):
        vec.mem_log.clear()
        got = pn.fft.fft(x)
    assert got.real.equal_bits(want.real) and got.imag.equal_bits(want.imag)
    assert any(cmd[3] >> 2 & 1 for cmd in vec.mem_log)              # gemv / FMA3 ran on the engine
    assert hw.stats()["fallback"] == 0


# ---- the hardware FFT core (5.5) --------------------------------------------------------------------------

@pytest.mark.parametrize("c,tol", [(pn.P16, 2e-3), (pn.P32, 3e-8)])
@pytest.mark.parametrize("N", [16, 64, 256, 1024])
def test_core_model_accuracy(c, tol, N):
    rnd = np.random.default_rng(N)
    z = rnd.standard_normal(N) + 1j * rnd.standard_normal(N)
    X = F.fft_core_model(z, config=c)
    x = pn.array(z.real, c).to_float() + 1j * pn.array(z.imag, c).to_float()
    assert _rel_err(X, np.fft.fft(x)) < tol
    assert X.real.equal_bits(pn.fft.fft(z, config=c, method="core").real)   # PositSw.fft_core == the model


@pytest.mark.parametrize("n", [16, 32])
def test_core_on_fake_engine(n):
    c = PositConfig(n)
    mem = FakeUDmaBuf(1 << 20)
    vec = FakeVecMmio(c, mem=mem, fft_max_s=5)
    hw = PositVecHw(vec, None, mem)
    assert hw.fft_max_s == 5
    rnd = np.random.default_rng(n)
    for N, B in ((16, 1), (64, 5), (1024, 2)):
        x = pn.array(rnd.standard_normal((B, N)), c)
        want = F.fft_core_model(x)
        with pn.using(hw):
            got = pn.fft.fft(x)                                     # auto: the core
            inv = pn.fft.ifft(got)
        assert got.real.equal_bits(want.real) and got.imag.equal_bits(want.imag), N
        assert np.max(np.abs(inv.to_complex() - x.to_float())) < (1e-2 if n == 16 else 1e-7)   # ~2 log4 N + 1 roundings
    assert vec.fft_tw_loads == 3 and hw.stats()["fallback"] == 0      # one table per N, reused by ifft
    with pn.using(hw):
        b = pn.fft.fft(pn.array(rnd.standard_normal(96), c))        # not 4^S: blocks
        assert b.shape == (96,)
        big = pn.fft.fft(pn.array(rnd.standard_normal(4096), c))    # 4^6 > 4^5: blocks too
        assert big.shape == (4096,)
    assert vec.regs[regs.FFT_CTRL] == 0
    with pytest.raises(ValueError, match="4\\^S"):
        pn.fft.fft(pn.array(np.ones(32), c), method="core")


def test_core_device_memory_chain():
    """PositComplex.to_device(): the core reads the (B, 2, N) buffer in place and leaves its result there;
    fft -> ifft on the device without copies, bit for bit the software model."""
    c = PositConfig(32)
    mem = FakeUDmaBuf(1 << 21)
    vec = FakeVecMmio(c, mem=mem, fft_max_s=5)
    hw = PositVecHw(vec, None, mem)
    rnd = np.random.default_rng(21)
    z = rnd.standard_normal((4, 256)) + 1j * rnd.standard_normal((4, 256))
    x = pn.fft.PositComplex(pn.array(z.real, c), pn.array(z.imag, c))
    want_f = F.fft_core_model(x)
    want_i = pn.fft.ifft(want_f, method="core")                    # PositSw: conj(fft(conj)) / N
    with pn.using(hw):
        xd = x.to_device()
        assert xd.on_device and xd.real.equal_bits(x.real)
        src = hw.fft_device_offset(xd.real.bits, xd.imag.bits)
        vec.mem_log.clear()
        Xd = pn.fft.fft(xd)
        assert Xd.on_device                                        # result stays on the device
        fft_cmds = [cmd for cmd in vec.mem_log if cmd[3] >> 2 & 1]
        assert fft_cmds and fft_cmds[0][1][0] == mem.phys_addr + src   # read in place, no staging copy
        xi = pn.fft.ifft(Xd)
        assert xi.on_device
    assert Xd.real.equal_bits(want_f.real) and Xd.imag.equal_bits(want_f.imag)
    assert xi.real.equal_bits(want_i.real) and xi.imag.equal_bits(want_i.imag)
    assert np.max(np.abs(xi.to_complex() - x.to_complex())) < 1e-7
    h = xi.to_host()
    assert not h.on_device and h.real.equal_bits(xi.real)
    del xd, Xd, xi
    import gc
    gc.collect()
    assert hw.heap.stats()["blocks"] == 0
