"""The wider NumPy API (positnp.npapi and PositComplex): reductions with keepdims / out / axis tuples, shape
functions, exact element-wise operations, order statistics, norms, FFT normalisations and complex arrays.

Shape functions are checked against NumPy on the exact float64 values; value results against exact rationals
rounded once by the reference model."""

import math
from fractions import Fraction as F

import numpy as np
import pytest

import positnp as pn
from positnp import ref
from positnp.fft import PositComplex

P8, P16, P32 = pn.P8, pn.P16, pn.P32
rng = np.random.default_rng(2026)


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


def fl(x):
    return x.to_float()


# ---- reductions: keepdims, axis tuples, out=, nothing silently ignored ---------------------------------

def test_keepdims_axis_tuple_and_out():
    m = pn.array(rng.standard_normal((3, 4, 5)))
    for f in (np.sum, np.mean, np.max, np.min, np.prod, np.var, np.std):
        r = f(m, axis=(0, 2), keepdims=True)
        assert r.shape == (1, 4, 1)
        assert r.reshape(4).equal_bits(f(m, axis=(0, 2)))
    assert np.sum(m, axis=-1).equal_bits(np.sum(m, axis=2))
    assert np.argmax(m, axis=1, keepdims=True).shape == (3, 1, 5)
    out = pn.zeros((3, 5))
    r = np.sum(m, axis=1, out=out)
    assert r is out and out.equal_bits(np.sum(m, axis=1))
    o2 = pn.zeros(m.shape)
    np.add(m, 1.0, out=o2)
    assert o2.equal_bits(m + 1)
    assert np.reshape(m, (5, 12), order="F").to_float().tolist() == np.reshape(fl(m), (5, 12), order="F").tolist()
    with pytest.raises(TypeError, match="where"):
        np.sum(m, where=np.ones(m.shape, bool))
    with pytest.raises(TypeError):
        np.max(m, initial=0)


def test_softmax_and_layer_norm_shapes():
    x = pn.array(rng.standard_normal((4, 6)), P16)
    e = np.exp(x - np.max(x, axis=1, keepdims=True))
    s = e / np.sum(e, axis=1, keepdims=True)
    assert s.shape == (4, 6) and np.allclose(fl(np.sum(s, axis=1)), 1, atol=3e-3)
    z = (x - np.mean(x, axis=-1, keepdims=True)) / np.sqrt(np.var(x, axis=-1, keepdims=True) + 1e-5)
    assert z.shape == (4, 6) and np.allclose(fl(np.mean(z, axis=-1)), 0, atol=1e-2)


def test_row_sums_by_matmul_equal_the_quire():
    m = pn.from_bits(rng.integers(0, 1 << 16, (40, 33)), P16)
    want = [int(pn.array(0, P16).bits)] * 0 + [int(np.sum(m[i]).bits) for i in range(40)]   # one quire per row
    assert np.sum(m, axis=1).bits.astype(np.int64).tolist() == want
    assert np.sum(m.T, axis=0).bits.astype(np.int64).tolist() == want


def test_ufunc_reduce_and_accumulate():
    m = pn.array(rng.standard_normal((3, 4)))
    assert np.add.reduce(m).equal_bits(np.sum(m, axis=0))
    assert np.add.reduce(m, axis=1, keepdims=True).equal_bits(np.sum(m, axis=1, keepdims=True))
    assert np.maximum.reduce(m, axis=1).equal_bits(np.max(m, axis=1))
    assert np.multiply.reduce(m, axis=None).equal_bits(np.prod(m))
    assert np.add.accumulate(m).equal_bits(np.cumsum(m, axis=0))


# ---- scalars, conversions ------------------------------------------------------------------------------

def test_scalars_and_conversions():
    x = pn.array([2.75, -1.5, 0.0])
    assert int(x[0]) == 2 and int(x[1]) == -1 and bool(x[0]) and not bool(x[2])
    assert x.tolist() == [2.75, -1.5, 0.0] and x.item(1) == -1.5 and x.nbytes == 12 and x.itemsize == 4
    with pytest.raises(ValueError):
        bool(x)
    with pytest.raises(ValueError, match="NaR"):
        int(pn.nar())
    with pytest.raises(TypeError, match="to_float"):
        np.asarray(x)
    z = pn.array([1 + 2j, -0.5j])
    assert isinstance(z, PositComplex) and z.to_complex().tolist() == [1 + 2j, -0.5j]
    w = x[:2] + 1j
    assert isinstance(w, PositComplex) and w.to_complex().tolist() == [2.75 + 1j, -1.5 + 1j]
    assert isinstance(2j * x, PositComplex) and isinstance(np.multiply(x, 1j), PositComplex)


# ---- shape functions on the bits -----------------------------------------------------------------------------

def test_shape_functions_match_numpy():
    a = pn.array(rng.standard_normal((3, 4)))
    v = pn.array(rng.standard_normal(6))
    f, g = fl(a), fl(v)
    cases = [
        (np.flip(a, 0), np.flip(f, 0)), (np.roll(v, 2), np.roll(g, 2)), (np.rot90(a), np.rot90(f)),
        (np.tile(v, 2), np.tile(g, 2)), (np.repeat(v, 2), np.repeat(g, 2)), (np.squeeze(a[None]), f),
        (np.expand_dims(v, 0), g[None]), (np.swapaxes(a, 0, 1), f.T), (np.moveaxis(a, 0, -1), f.T),
        (np.broadcast_to(v, (2, 6)), np.broadcast_to(g, (2, 6))), (np.diag(v), np.diag(g)), (np.diag(a), np.diag(f)),
        (np.triu(a), np.triu(f)), (np.tril(a, -1), np.tril(f, -1)), (np.take(v, [0, 5, 2]), g[[0, 5, 2]]),
        (np.hstack([v, v]), np.hstack([g, g])), (np.vstack([v, v]), np.vstack([g, g])),
        (np.column_stack([v, v]), np.column_stack([g, g])), (np.append(v, [1.0, 2.0]), np.append(g, [1, 2])),
        (np.insert(v, 1, 3.0), np.insert(g, 1, 3.0)), (np.delete(v, [0, 1]), np.delete(g, [0, 1])),
        (np.pad(v, 2), np.pad(g, 2)), (np.pad(v, (1, 2), constant_values=1.5), np.pad(g, (1, 2), constant_values=1.5)),
        (np.pad(a, 1, mode="edge"), np.pad(f, 1, mode="edge")), (np.pad(v, 2, mode="reflect"), np.pad(g, 2, mode="reflect")),
        (np.zeros_like(a), np.zeros_like(f)), (np.ones_like(v), np.ones_like(g)), (np.full_like(v, 2.5), np.full_like(g, 2.5)),
        (np.concatenate([v, pn.array([1.0], P16)]), np.concatenate([g, [1.0]])),
        (pn.eye(3, k=1), np.eye(3, k=1)), (pn.ones((2, 2)), np.ones((2, 2))), (pn.full(3, 0.75), np.full(3, 0.75)),
        (pn.arange(5), np.arange(5.0)), (pn.linspace(0, 1, 5), np.linspace(0, 1, 5)),
    ]
    for got, want in cases:
        assert isinstance(got, pn.PositArray) and np.array_equal(fl(got), want), (got, want)
    parts = np.split(v, 3)
    assert len(parts) == 3 and all(np.array_equal(fl(p), q) for p, q in zip(parts, np.split(g, 3)))
    assert np.zeros_like(a, dtype=np.float32).dtype == np.float32 and np.zeros_like(a, dtype=P16).config == P16
    with pytest.raises(TypeError, match="mode"):
        np.pad(v, 1, mode="mean")


def test_selection_on_value_order():
    x = pn.array([3.0, -1.0, np.nan, 2.0, -1.0, 0.0])
    u, cnt = np.unique(x, return_counts=True)
    assert np.array_equal(fl(u)[1:], [-1.0, 0.0, 2.0, 3.0]) and np.isnan(fl(u)[0]) and cnt.tolist() == [1, 2, 1, 1, 1]
    s = np.sort(x)
    assert np.searchsorted(s, 1.0) == 4 and np.count_nonzero(x) == 5 and np.nonzero(x)[0].tolist() == [0, 1, 2, 3, 4]
    p = np.partition(pn.array([5.0, 1.0, 4.0, 2.0]), 1)
    assert sorted(fl(p)[:2]) == [1.0, 2.0]


# ---- exact element-wise operations ---------------------------------------------------------------------------

def pair_ref(c, a, b, op):
    x, y = val(c, a), val(c, b)
    if x is None or y is None:
        return c.nar
    if op == "hypot":
        return None
    if y == 0:
        return c.nar
    if op == "fmod":
        q = abs(x) - abs(y) * math.floor(abs(x) / abs(y))
        return rnd(c, q if x >= 0 else -q)
    if op == "remainder":
        return rnd(c, x - y * math.floor(x / y))
    return rnd(c, F(math.floor(x / y)))


@pytest.mark.parametrize("op", ["fmod", "remainder", "floor_divide"])
def test_exact_modulo_posit8_all_pairs(op):
    a, b = np.meshgrid(np.arange(256), np.arange(0, 256, 3))
    x, y = pn.from_bits(a.ravel(), P8), pn.from_bits(b.ravel(), P8)
    got = getattr(np, op)(x, y).bits.astype(np.int64).tolist()
    assert got == [pair_ref(P8, i, j, op) for i, j in zip(a.ravel(), b.ravel())]


def test_hypot_and_arctan2():
    c = P16
    x = pn.array(rng.standard_normal(300) * 5, c)
    y = pn.array(rng.standard_normal(300) * 5, c)
    h = np.hypot(x, y)
    for i in range(300):
        v = val(c, x.bits[i]) ** 2 + val(c, y.bits[i]) ** 2
        r = int(h.bits[i])
        lo, mid, hi = val(c, r - 1), val(c, r), val(c, r + 1)
        assert ((mid + lo) / 2) ** 2 <= v <= ((mid + hi) / 2) ** 2
    t = np.arctan2(y, x)
    assert np.array_equal(t.bits, pn.array(np.arctan2(fl(y), fl(x)), c).bits)
    assert np.isnan(fl(np.arctan2(pn.array(0.0), pn.array(0.0))))


def test_sign_and_friends():
    x = pn.array([-2.0, 0.0, 3.0, np.nan])
    assert np.array_equal(fl(np.sign(x))[:3], [-1, 0, 1]) and np.isnan(fl(np.sign(x))[3])
    assert np.signbit(x).tolist() == [True, False, False, True]
    assert np.isfinite(x).tolist() == [True, True, True, False] and not np.isinf(x).any()
    assert np.array_equal(fl(np.copysign(pn.array([1.5, -2.0]), pn.array([-1.0, 3.0]))), [-1.5, 2.0])
    assert np.array_equal(fl(np.fmax(x, pn.array([0.0, 0.0, 0.0, 7.0]))), [0, 0, 3, 7])
    assert np.logical_and(x, pn.array([1.0, 1.0, 0.0, 1.0])).tolist() == [True, False, False, True]


def test_isclose_allclose():
    x = pn.array([1.0, 2.0, np.nan])
    assert np.isclose(x, [1.0 + 1e-9, 2.1, np.nan], equal_nan=True).tolist() == [True, False, True]
    assert np.allclose(x[:2], x[:2]) and not np.allclose(x[:2], pn.array([1.0, 2.5]))


# ---- order statistics, nan* reductions, average, diff, trace ----------------------------------------------------

def quantile_ref(c, bits, q, method="linear"):
    v = sorted(val(c, b) for b in bits)
    p = F(q) * (len(v) - 1)
    lo = math.floor(p)
    hi = min(lo + 1, len(v) - 1)
    fr = p - lo
    return rnd(c, {"linear": v[lo] + fr * (v[hi] - v[lo]), "lower": v[lo], "higher": v[lo] if fr == 0 else v[hi],
                   "midpoint": v[lo] if fr == 0 else (v[lo] + v[hi]) / 2}[method])


def test_median_percentile_quantile_exact():
    c = P16
    x = pn.array(rng.standard_normal(37) * 10, c)
    assert int(np.median(x).bits) == quantile_ref(c, x.bits, 0.5)
    assert int(np.median(x[:36]).bits) == quantile_ref(c, x.bits[:36], 0.5)
    for q in (0.1, 0.25, 0.9):
        for m in ("linear", "lower", "higher", "midpoint"):
            assert int(np.quantile(x, q, method=m).bits) == quantile_ref(c, x.bits, q, m)
    pr = np.percentile(x, [10, 50, 90])
    assert pr.shape == (3,) and int(pr.bits[1]) == quantile_ref(c, x.bits, 0.5)
    m2 = pn.array(rng.standard_normal((4, 9)), c)
    assert np.median(m2, axis=1).shape == (4,) and np.median(m2, axis=1, keepdims=True).shape == (4, 1)
    assert np.isnan(fl(np.median(pn.array([1.0, np.nan]))))


def test_nan_reductions_average_diff_trace():
    x = pn.array([1.0, np.nan, 3.0, 4.0])
    assert float(np.nansum(x)) == 8.0 and float(np.nanmean(x)) == pytest.approx(8 / 3, rel=1e-7)
    assert float(np.nanmax(x)) == 4.0 and float(np.nanmin(x)) == 1.0
    assert float(np.nanvar(x)) == pytest.approx(np.nanvar(fl(x)), rel=1e-7)
    c = P32
    a, w = pn.array(rng.standard_normal(20), c), pn.array(rng.random(20), c)
    want = sum(val(c, p) * val(c, q) for p, q in zip(a.bits, w.bits)) / sum(val(c, q) for q in w.bits)
    assert int(np.average(a, weights=w).bits) == rnd(c, want)
    assert np.array_equal(fl(np.diff(pn.array([1.0, 4.0, 9.0, 16.0]), 2)), [2.0, 2.0])
    assert np.array_equal(fl(np.diff(pn.array([1.0, 4.0]), prepend=0.0)), [1.0, 3.0])
    assert float(np.trace(pn.array(np.arange(9.0).reshape(3, 3)))) == 12.0


# ---- norms -----------------------------------------------------------------------------------------------------

def test_norms():
    c = P32
    v = pn.array(rng.standard_normal(30), c)
    assert int(np.linalg.norm(v, 1).bits) == rnd(c, sum(abs(val(c, b)) for b in v.bits))
    assert float(np.linalg.norm(v, np.inf)) == np.max(np.abs(fl(v)))
    assert float(np.linalg.norm(v, -np.inf)) == np.min(np.abs(fl(v)))
    assert float(np.linalg.norm(v, 0)) == 30.0
    M = pn.array(rng.standard_normal((5, 4)), c)
    Mf = fl(M)
    for o in (None, "fro", 1, -1, np.inf, -np.inf):
        assert float(np.linalg.norm(M, o)) == pytest.approx(np.linalg.norm(Mf, o), rel=1e-7), o
    assert np.allclose(fl(np.linalg.norm(M, axis=1)), np.linalg.norm(Mf, axis=1), rtol=1e-7)
    assert np.linalg.norm(M, axis=0, keepdims=True).shape == (1, 4)
    with pytest.raises(TypeError, match="SVD"):
        np.linalg.norm(M, 2)


# ---- FFT: norm=, irfft, fft2 / fftn, shifts, complex inputs through np.fft --------------------------------------

def test_fft_norms_and_inverses():
    x = pn.array(rng.standard_normal(64))
    xf = fl(x)
    for nm in ("forward", "ortho", "backward"):
        X = np.fft.fft(x, norm=nm)
        assert np.allclose(X.to_complex(), np.fft.fft(xf, norm=nm), atol=2e-6)
        back = np.fft.ifft(X, norm=nm)                                # PositComplex through np.fft
        assert np.allclose(back.to_complex(), xf, atol=2e-6)
    R = np.fft.rfft(x)
    assert np.allclose(fl(np.fft.irfft(R)), xf, atol=2e-6) and np.fft.irfft(R, 64).shape == (64,)
    y = pn.array(rng.standard_normal(10))
    assert np.allclose(fl(np.fft.irfft(np.fft.rfft(y), 10)), fl(y), atol=2e-6)       # odd / even, non power of 2
    assert np.allclose(fl(np.fft.irfft(np.fft.rfft(y[:9]), 9)), fl(y[:9]), atol=2e-6)
    Z = pn.array(rng.standard_normal((8, 16)))
    assert np.allclose(np.fft.fft2(Z).to_complex(), np.fft.fft2(fl(Z)), atol=1e-5)
    assert np.allclose(np.fft.ifft2(np.fft.fft2(Z)).to_complex(), fl(Z), atol=1e-6)
    assert np.allclose(np.fft.fftn(Z, axes=(0,)).to_complex(), np.fft.fftn(fl(Z), axes=(0,)), atol=1e-5)
    X = np.fft.fft(x)
    assert np.array_equal(np.fft.fftshift(X).to_complex(), np.fft.fftshift(X.to_complex()))
    assert np.array_equal(fl(np.fft.ifftshift(np.fft.fftshift(x))), xf)


def test_ortho_scaling_rounded_once():
    c = P16
    X = PositComplex(pn.array(rng.standard_normal(12) * 3, c), pn.array(rng.standard_normal(12) * 3, c))
    from positnp.fft import _div_sqrt
    r = _div_sqrt(X.real, 12)
    for b, o in zip(X.real.bits, r.bits):
        v, g = val(c, b), int(o)
        if v == 0:
            assert g == 0
            continue
        t = v * v / 12
        mag = g if v > 0 else (-g) & c.mask
        m, lo, hi = val(c, mag), val(c, mag - 1), val(c, mag + 1)
        assert ((m + lo) / 2) ** 2 <= t <= ((m + hi) / 2) ** 2


# ---- PositComplex as an array ------------------------------------------------------------------------------------

def test_complex_container_and_numpy():
    z = pn.array(rng.standard_normal(8) + 1j * rng.standard_normal(8))
    zc = z.to_complex()
    assert len(z) == 8 and z.ndim == 1 and z[2:5].to_complex().tolist() == zc[2:5].tolist()
    z2 = z.copy()
    z2[0] = 3 - 4j
    assert z2.to_complex()[0] == 3 - 4j and z.to_complex()[0] == zc[0]
    assert z.reshape(2, 4).shape == (2, 4) and np.flip(z).to_complex().tolist() == zc[::-1].tolist()
    s = np.sum(z)
    assert s.real.equal_bits(np.sum(z.real)) and s.imag.equal_bits(np.sum(z.imag))
    assert np.concatenate([z, z]).shape == (16,) and np.angle(z).shape == (8,)
    assert np.allclose(fl(np.angle(z)), np.angle(zc), atol=1e-7) and np.all(np.isclose(z, zc))
    assert (z == z).all() and not (z != z).any()
    with pytest.raises(TypeError, match="to_complex"):
        np.asarray(z)


def test_complex_power_exact():
    c = P8
    re, im = np.meshgrid(np.arange(0, 256, 7), np.arange(0, 256, 11))
    z = PositComplex(pn.from_bits(re.ravel(), c), pn.from_bits(im.ravel(), c))
    for n in (3, -2, 0):
        w = z ** n
        for i in range(0, z.size, 5):
            a, b = val(c, z.real.bits[i]), val(c, z.imag.bits[i])
            if a is None or b is None or (a == 0 and b == 0 and n <= 0):
                assert w.real.bits[i] == c.nar
                continue
            p = (complex_frac(a, b, n))
            assert int(w.real.bits[i]) == rnd(c, p[0]) and int(w.imag.bits[i]) == rnd(c, p[1])


def complex_frac(a, b, n):
    re, im = F(1), F(0)
    for _ in range(abs(n)):
        re, im = re * a - im * b, re * b + im * a
    if n < 0:
        d = re * re + im * im
        re, im = re / d, -im / d
    return re, im


def test_complex_matmul_and_vdot_rounded_once():
    c = P16
    A = PositComplex(pn.array(rng.standard_normal((3, 5)), c), pn.array(rng.standard_normal((3, 5)), c))
    B = PositComplex(pn.array(rng.standard_normal((5, 2)), c), pn.array(rng.standard_normal((5, 2)), c))
    C = A @ B
    for i in range(3):
        for j in range(2):
            ar = [val(c, A.real.bits[i, k]) for k in range(5)]
            ai = [val(c, A.imag.bits[i, k]) for k in range(5)]
            br = [val(c, B.real.bits[k, j]) for k in range(5)]
            bi = [val(c, B.imag.bits[k, j]) for k in range(5)]
            assert int(C.real.bits[i, j]) == rnd(c, sum(p * q - r * s for p, q, r, s in zip(ar, br, ai, bi)))
            assert int(C.imag.bits[i, j]) == rnd(c, sum(p * s + r * q for p, q, r, s in zip(ar, br, ai, bi)))
    v = A[0]
    d = np.vdot(v, v)
    assert int(d.imag.bits) == 0 and float(d.real) == pytest.approx(np.vdot(v.to_complex(), v.to_complex()).real, rel=1e-3)
    assert (A @ B.real).shape == (3, 2)
