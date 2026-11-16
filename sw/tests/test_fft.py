"""Stage 7: FFT on posits (positnp.fft) - exact DFT blocks, four-step accuracy, NumPy-like API."""

import numpy as np
import pytest

import positnp as pn
from positnp import fft as F, ref
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
    with pytest.raises(TypeError, match="norm"):
        np.fft.fft(r, norm="ortho")
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
    vec = FakeVecMmio(PositConfig(16), mem=mem, gemv_k=4096, gemv_cols=4)
    hw = PositVecHw(vec, None, mem)
    x = pn.array(np.random.default_rng(3).standard_normal(256), pn.P16)
    want = pn.fft.fft(x)
    with pn.using(hw):
        vec.mem_log.clear()
        got = pn.fft.fft(x)
    assert got.real.equal_bits(want.real) and got.imag.equal_bits(want.imag)
    assert any(cmd[3] >> 2 & 1 for cmd in vec.mem_log)              # gemv / FMA3 ran on the engine
    assert hw.stats()["fallback"] == 0
