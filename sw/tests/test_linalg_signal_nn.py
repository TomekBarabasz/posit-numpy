"""PR B: mixed-precision and batched linalg, cholesky / lstsq / slogdet, einsum / tensordot / outer, complex
convolution, pn.signal (lfilter, convolve2d) and pn.nn (linear, conv2d, activations).

Exactness is checked against rationals (fractions.Fraction) rounded once by the reference model."""

import itertools
from fractions import Fraction as F

import numpy as np
import pytest

import positnp as pn
from positnp import ref
from positnp.fft import PositComplex

P8, P16, P32 = pn.P8, pn.P16, pn.P32
rng = np.random.default_rng(77)


def val(c, b):
    d = ref.decode(c, int(b))
    return None if d is None else F(d[0]) * F(2) ** d[1]


def rnd(c, q):
    if q is None:
        return c.nar
    if q == 0:
        return 0
    x, inexact = ref._div((q.numerator, 0), (q.denominator, 0))
    return ref.round_(c, x, inexact)[0]


def vals(x):
    return np.vectorize(lambda b: val(x.config, b), otypes=[object])(x.bits)


# ---- linalg ------------------------------------------------------------------------------------------------

def test_mixed_precision_solve_reaches_posit32():
    A = pn.array(rng.standard_normal((12, 12)) + 5 * np.eye(12))
    b = pn.array(rng.standard_normal(12))
    xf = np.linalg.solve(A.to_float(), b.to_float())
    full = np.abs(pn.linalg.solve(A, b).to_float() - xf).max()
    mixed = pn.linalg.solve(A, b, lu=P16)
    plain16 = pn.linalg.solve(A, b, lu=P16, refine=0)
    assert mixed.config == P32
    assert np.abs(mixed.to_float() - xf).max() <= 4 * full + 1e-12
    assert np.abs(plain16.to_float() - xf).max() > 100 * np.abs(mixed.to_float() - xf).max()


def test_residual_is_rounded_once():
    from positnp.linalg import _residual_mm
    c = P16
    A = pn.array(rng.standard_normal((5, 5)), c)
    X = pn.array(rng.standard_normal((5, 2)), c)
    B = pn.array(rng.standard_normal((5, 2)), c)
    R = _residual_mm(A, X, B)
    a, x, b = vals(A), vals(X), vals(B)
    for i, j in itertools.product(range(5), range(2)):
        assert int(R.bits[i, j]) == rnd(c, b[i, j] - sum(a[i, k] * x[k, j] for k in range(5)))


def test_batched_solve_inv_det():
    A = pn.array(rng.standard_normal((3, 4, 4)) + 4 * np.eye(4))
    B = pn.array(rng.standard_normal((3, 4, 2)))
    X = np.linalg.solve(A, B)
    assert X.shape == (3, 4, 2)
    for i in range(3):
        assert X[i].equal_bits(np.linalg.solve(A[i], B[i]))
        assert np.linalg.inv(A)[i].equal_bits(np.linalg.inv(A[i]))
        assert np.linalg.det(A)[i].equal_bits(np.linalg.det(A[i]))
    assert np.linalg.solve(A, pn.array(np.ones(4))).shape == (3, 4)


def test_cholesky_lstsq_slogdet():
    c = P32
    M = pn.array(rng.standard_normal((6, 6)), c)
    S = M @ M.T + pn.array(6 * np.eye(6), c)
    L = np.linalg.cholesky(S)
    assert np.allclose((L @ L.T).to_float(), S.to_float(), rtol=1e-6) and np.all(np.triu(L.to_float(), 1) == 0)
    s, l = vals(S), vals(L)
    for j in range(6):                                      # diagonal: sqrt of the exact remainder, rounded once
        q = s[j, j] - sum(l[j, k] ** 2 for k in range(j))
        r = int(L.bits[j, j])
        lo, mid, hi = val(c, r - 1), val(c, r), val(c, r + 1)
        assert ((mid + lo) / 2) ** 2 <= q <= ((mid + hi) / 2) ** 2
    assert np.linalg.cholesky(S, upper=True).equal_bits(L.T)
    with pytest.raises(np.linalg.LinAlgError):
        np.linalg.cholesky(pn.array([[1.0, 2.0], [2.0, 1.0]]))
    Am = pn.array(rng.standard_normal((10, 3)))
    y = pn.array(rng.standard_normal(10))
    x, res, rank, sv = np.linalg.lstsq(Am, y)
    xr, rr, _, _ = np.linalg.lstsq(Am.to_float(), y.to_float(), rcond=None)
    assert np.allclose(x.to_float(), xr, rtol=1e-6, atol=1e-8) and np.allclose(res.to_float(), rr, rtol=1e-6)
    assert rank == 3 and sv is None
    A = pn.array(rng.standard_normal((5, 5)))
    sg, la = np.linalg.slogdet(A)
    want = np.linalg.slogdet(A.to_float())
    assert float(sg) == want[0] and float(la) == pytest.approx(want[1], rel=1e-6)


# ---- contractions ------------------------------------------------------------------------------------------

def test_einsum_patterns_match_numpy():
    a = pn.array(rng.standard_normal((3, 4)))
    b = pn.array(rng.standard_normal((4, 5)))
    v = pn.array(rng.standard_normal(4))
    T = pn.array(rng.standard_normal((2, 3, 4)))
    U = pn.array(rng.standard_normal((2, 4, 5)))
    E = pn.array(rng.standard_normal((3, 3)))
    f = lambda x: x.to_float()                                              # noqa: E731
    cases = [("ij,jk->ik", (a, b)), ("ij,jk", (a, b)), ("bij,bjk->bik", (T, U)), ("ii", (E,)), ("ii->i", (E,)),
             ("ij->ji", (a,)), ("ij->", (a,)), ("ij->j", (a,)), ("i,i", (v, v)), ("i,j->ij", (v, v)),
             ("ij,j->i", (a, v)), ("bij->bji", (T,)), ("ij,ij->", (a, a)), ("ij,kj->ik", (a, a))]
    for spec, ops in cases:
        got = np.einsum(spec, *ops)
        assert np.allclose(f(got), np.einsum(spec, *[f(o) for o in ops]), rtol=1e-6, atol=1e-6), spec
    with pytest.raises(TypeError, match=r"\.\.\."):
        np.einsum("...ij,jk", a, b)


def test_contractions_rounded_once():
    c = P16
    a = pn.array(rng.standard_normal((3, 4)), c)
    b = pn.array(rng.standard_normal((4, 2)), c)
    av, bv = vals(a), vals(b)
    for got in (np.einsum("ij,jk->ik", a, b), np.tensordot(a, b, axes=1)):
        for i, k in itertools.product(range(3), range(2)):
            assert int(got.bits[i, k]) == rnd(c, sum(av[i, j] * bv[j, k] for j in range(4)))
    o = np.outer(a[0], b[:, 0])
    assert all(int(o.bits[i, j]) == rnd(c, av[0, i] * bv[j, 0]) for i in range(4) for j in range(4))
    T = pn.array(rng.standard_normal((2, 3, 4)))
    assert np.allclose(np.tensordot(T, T, axes=([1, 2], [1, 2])).to_float(),
                       np.tensordot(T.to_float(), T.to_float(), axes=([1, 2], [1, 2])), rtol=1e-6)


# ---- complex convolution -----------------------------------------------------------------------------------

def test_complex_convolution_rounded_once_and_modes():
    c = P16
    z = PositComplex(pn.array(rng.standard_normal(9), c), pn.array(rng.standard_normal(9), c))
    h = PositComplex(pn.array(rng.standard_normal(4), c), pn.array(rng.standard_normal(4), c))
    y = np.convolve(z, h)
    zr, zi, hr, hi = (vals(p) for p in (z.real, z.imag, h.real, h.imag))
    for n in range(12):
        ks = [k for k in range(4) if 0 <= n - k < 9]
        assert int(y.real.bits[n]) == rnd(c, sum(zr[n - k] * hr[k] - zi[n - k] * hi[k] for k in ks))
        assert int(y.imag.bits[n]) == rnd(c, sum(zr[n - k] * hi[k] + zi[n - k] * hr[k] for k in ks))
    zc, hc = z.to_complex(), h.to_complex()
    for m in ("full", "same", "valid"):
        assert np.allclose(np.convolve(z, h, m).to_complex(), np.convolve(zc, hc, m), atol=1e-2)
        assert np.allclose(np.correlate(z, h, m).to_complex(), np.correlate(zc, hc, m), atol=1e-2)
        assert np.allclose(pn.convolve(z, h.real, m).to_complex(), np.convolve(zc, h.real.to_float(), m), atol=1e-2)


# ---- pn.signal -------------------------------------------------------------------------------------------------

def test_lfilter_each_output_rounded_once():
    c = P32
    x = pn.array(rng.standard_normal(60), c)
    b, a = pn.array([0.25, 0.5, 0.25], c), pn.array([1.0, -0.6, 0.2], c)
    y = pn.signal.lfilter(b, a, x)
    xv, bv, av = vals(x), vals(b), vals(a)
    yv = []
    for n in range(60):
        s = sum(bv[k] * xv[n - k] for k in range(3) if n - k >= 0)
        s -= sum(av[k] * yv[n - k] for k in range(1, 3) if n - k >= 0)
        assert int(y.bits[n]) == rnd(c, s)
        yv.append(val(c, y.bits[n]))
    m = pn.array(rng.standard_normal((3, 40)), c)
    yy = pn.signal.lfilter(b, a, m, axis=1)
    assert all(yy[i].equal_bits(pn.signal.lfilter(b, a, m[i])) for i in range(3))
    assert pn.signal.lfilter(b, a, m.T, axis=0).T.equal_bits(yy)
    y2 = pn.signal.lfilter(b, pn.array([2.0, -0.6], c), x)
    assert y2.shape == (60,)


def conv2_ref(x, k, mode):
    H, W = len(x), len(x[0])
    kh, kw = len(k), len(k[0])
    full = [[F(0)] * (W + kw - 1) for _ in range(H + kh - 1)]
    for i, j, m, n in itertools.product(range(H), range(W), range(kh), range(kw)):
        full[i + m][j + n] += x[i][j] * k[m][n]
    if mode == "full":
        return full
    if mode == "valid":
        return [row[kw - 1:W] for row in full[kh - 1:H]]
    r0, c0 = (kh - 1) // 2, (kw - 1) // 2
    return [row[c0:c0 + W] for row in full[r0:r0 + H]]


@pytest.mark.parametrize("mode", ["full", "same", "valid"])
def test_convolve2d_rounded_once(mode):
    c = P16
    x = pn.array(rng.standard_normal((5, 6)), c)
    k = pn.array(rng.standard_normal((3, 2)), c)
    got = pn.signal.convolve2d(x, k, mode)
    want = conv2_ref(vals(x).tolist(), vals(k).tolist(), mode)
    assert got.bits.astype(np.int64).tolist() == [[rnd(c, v) for v in row] for row in want]
    kk = pn.array(k.to_float()[::-1, ::-1], c)
    assert pn.signal.correlate2d(x, kk, mode).equal_bits(got)


# ---- pn.nn -----------------------------------------------------------------------------------------------------

def test_linear_and_conv2d_bias_inside_the_quire():
    c = P16
    x = pn.array(rng.standard_normal((2, 5)), c)
    W = pn.array(rng.standard_normal((3, 5)), c)
    b = pn.array(rng.standard_normal(3), c)
    y = pn.nn.linear(x, W, b)
    xv, wv, bv = vals(x), vals(W), vals(b)
    for i, o in itertools.product(range(2), range(3)):
        assert int(y.bits[i, o]) == rnd(c, sum(xv[i, k] * wv[o, k] for k in range(5)) + bv[o])
    X = pn.array(rng.standard_normal((1, 2, 4, 4)), c)
    K = pn.array(rng.standard_normal((2, 2, 3, 3)), c)
    Y = pn.nn.conv2d(X, K, b[:2], padding=1)
    Xv = np.pad(vals(X), ((0, 0), (0, 0), (1, 1), (1, 1)), constant_values=F(0))
    Kv = vals(K)
    for o, i, j in itertools.product(range(2), range(4), range(4)):
        s = sum(Xv[0, ch, i + m, j + n] * Kv[o, ch, m, n] for ch in range(2) for m in range(3) for n in range(3))
        assert int(Y.bits[0, o, i, j]) == rnd(c, s + bv[o])
    assert pn.nn.conv2d(X, K, stride=2).shape == (1, 2, 1, 1)


def test_activations_and_pooling():
    x = pn.array(rng.standard_normal((2, 3, 4, 4)), P16)
    assert np.array_equal(pn.nn.relu(pn.array([-1.0, np.nan, 2.0])).to_float()[[0, 2]], [0.0, 2.0])
    assert np.isnan(pn.nn.relu(pn.array([np.nan])).to_float()[0])
    s = pn.nn.softmax(x, axis=1)
    assert np.allclose(np.sum(s, axis=1).to_float(), 1, atol=3e-3)
    mp = pn.nn.max_pool2d(x, 2)
    assert np.array_equal(mp.to_float(), x.to_float().reshape(2, 3, 2, 2, 2, 2).max(axis=(3, 5)))
    assert pn.nn.layer_norm(x).shape == x.shape and pn.nn.gelu(x).shape == x.shape
    assert np.allclose(pn.nn.log_softmax(x[0, 0]).to_float(),
                       np.log(pn.nn.softmax(x[0, 0]).to_float()), atol=3e-3)
