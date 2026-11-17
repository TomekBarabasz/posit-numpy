"""The NumPy API added after the review: mixed formats, astype, **, var / std, clip, rounding to integers,
cumsum / prod, pn.save / pn.load, PositComplex arithmetic and np.linalg (solve, inv, det).

References are exact rationals (fractions.Fraction) rounded once by the reference model (ref.round_), so they
do not share code with the vectorised rounding of the library."""

from fractions import Fraction as F

import numpy as np
import pytest

import positnp as pn
from positnp import ref
from positnp.array import _round_exact
from positnp.fft import PositComplex

P8, P16, P32 = pn.P8, pn.P16, pn.P32


def val(c, b):
    d = ref.decode(c, int(b))
    return None if d is None else F(d[0]) * F(2) ** d[1]


def rnd(c, q):
    """An exact rational rounded once (None: NaR)."""
    if q is None:
        return c.nar
    if q == 0:
        return 0
    x, inexact = ref._div((q.numerator, 0), (q.denominator, 0))
    return ref.round_(c, x, inexact)[0]


def bits(x):
    return x.bits.astype(np.int64).tolist()


def is_rounded_sqrt(c, r, v):
    """r (positive posit bits) is sqrt(v) rounded to nearest: v lies between the squared midpoints to its
    neighbours (values in the middle of the range, where posit rounding is rounding of the value)."""
    x, lo, hi = val(c, r), val(c, r - 1), val(c, r + 1)
    return ((x + lo) / 2) ** 2 <= v <= ((x + hi) / 2) ** 2


# ---- the vectorised rounding of exact values ------------------------------------------------------------

@pytest.mark.parametrize("c", [P8, P16, P32])
def test_round_exact_matches_reference(c):
    rng = np.random.default_rng(c.n)
    ms, es, st = [], [], []
    for _ in range(3000):
        m = int(rng.integers(1, 1 << 62)) >> int(rng.integers(0, 62))
        m = (m << int(rng.integers(0, 200))) | int(rng.integers(0, 2))
        ms.append(-m if rng.integers(0, 2) else m)
        es.append(int(rng.integers(-c.max_scale - 250, c.max_scale + 60)))
        st.append(bool(rng.integers(0, 2)))
    got = _round_exact(c, ms, es, st).tolist()
    want = [ref.round_(c, (m, e), s)[0] if m else 0 for m, e, s in zip(ms, es, st)]
    assert got == want


# ---- formats ------------------------------------------------------------------------------------------

def test_widening_is_exact_and_mixed_formats_promote():
    x8 = pn.from_bits(np.arange(256), P8)
    x16 = x8.astype(P16)
    assert x16.config == P16 and np.array_equal(x16.to_float(), x8.to_float(), equal_nan=True)
    assert np.array_equal(x8.astype(P32).to_float(), x8.to_float(), equal_nan=True)
    y = pn.array(np.linspace(-3, 3, 256), P16)
    assert (x8 + y).equal_bits(x16 + y) and (y * x8).equal_bits(y * x16)
    assert np.array_equal(x8 < y, x16 < y)
    assert np.concatenate([x8, y]).config == P16 and pn.fma(x8, y, 1).config == P16
    A, b = pn.array(np.eye(3), P32), pn.array([1.0, 2.0, 3.0], P8)
    assert (A @ b).config == P32 and np.array_equal((A @ b).to_float(), [1, 2, 3])
    with pytest.raises(TypeError, match="different es"):
        y + pn.array([1.0], pn.PositConfig(16, 1))


def test_narrowing_rounds_once():
    x = pn.from_bits(np.random.default_rng(3).integers(0, 1 << 32, 5000), P32)
    assert bits(x.astype(P16)) == bits(pn.array(x.to_float(), P16))
    assert pn.array(x, P16).equal_bits(x.astype(P16))
    z = pn.zeros(4, P16)
    z[:] = pn.array([1.0, 1.0 + 2.0 ** -20, 3.0, 4.0], P32)      # assignment rounds into the array's format
    assert z.config == P16 and z.to_float().tolist() == [1.0, 1.0, 3.0, 4.0]


# ---- powers -------------------------------------------------------------------------------------------

def pow_ref(c, b, n):
    v = val(c, b)
    if v is None or (v == 0 and n <= 0):
        return c.nar
    return rnd(c, v ** n)


@pytest.mark.parametrize("n", [-3, -2, 0, 3, 4, 7])
def test_integer_powers_posit8_all(n):
    x = pn.from_bits(np.arange(256), P8)
    assert bits(x ** n) == [pow_ref(P8, b, n) for b in range(256)]


def test_integer_powers_posit16_and_32():
    for c, size in ((P16, 1500), (P32, 600)):
        b = np.random.default_rng(c.n).integers(0, 1 << c.n, size)
        x = pn.from_bits(b, c)
        for n in (3, -2, 9, -5):
            assert bits(x ** n) == [pow_ref(c, int(v), n) for v in b]


def test_power_fast_paths_and_errors():
    x = pn.from_bits(np.random.default_rng(5).integers(0, 1 << 16, 3000), P16)
    assert (x ** 2).equal_bits(x * x) and np.power(x, 2).equal_bits(x * x) and np.square(x).equal_bits(x * x)
    assert (x ** -1).equal_bits(1 / x) and np.reciprocal(x).equal_bits(1 / x)
    assert (x ** 0.5).equal_bits(np.sqrt(x)) and (x ** 1).equal_bits(x)
    assert np.power(x, 3).equal_bits(x ** 3)
    assert (pn.array([0.0, 2.0]) ** 0).bits[0] == P32.nar          # posit standard: pow(0, 0) is NaR
    for bad in (lambda: x ** 1.5, lambda: 2 ** x, lambda: x ** x):
        with pytest.raises(TypeError):
            bad()


# ---- var / std ------------------------------------------------------------------------------------------

def var_exact(vals, ddof):
    N = len(vals)
    s1, s2 = sum(vals), sum(v * v for v in vals)
    return (N * s2 - s1 * s1) / (N * (N - ddof))


def test_var_std_exact():
    rng = np.random.default_rng(7)
    for c in (P16, P32):
        x = pn.array(rng.standard_normal(60) * 30, c)
        vals = [val(c, b) for b in x.bits]
        for ddof in (0, 1):
            v = var_exact(vals, ddof)
            assert int(np.var(x, ddof=ddof).bits) == rnd(c, v)
            assert is_rounded_sqrt(c, int(np.std(x, ddof=ddof).bits), v)
    m = pn.array(rng.standard_normal((6, 7)), P32)
    cols = np.var(m, axis=0)
    assert cols.shape == (7,) and all(cols[j].equal_bits(np.var(m[:, j])) for j in range(7))
    assert np.var(pn.array([1.0]), ddof=1).bits == P32.nar and np.std(pn.array([1.0, np.nan])).bits == P32.nar


def test_var_without_cancellation():
    base = 4096.0
    f = base + np.arange(8) * 2.0 ** -10                           # exact in posit32 and in float32
    x = pn.array(f, P32)
    want = float(var_exact([F(v) for v in f], 0))
    assert float(np.var(x)) == pytest.approx(want, rel=1e-7)
    f32 = f.astype(np.float32)                                     # the naive formula in float32 loses it all
    naive = np.mean(f32 * f32) - np.mean(f32) ** 2
    assert abs(naive - want) > 100 * abs(float(np.var(x)) - want)


# ---- clip and rounding to integers -------------------------------------------------------------------------

def test_clip():
    x = pn.from_bits(np.arange(1 << 16), P16)
    f = x.to_float()
    got = np.clip(x, -2.5, 3.0).to_float()
    real = ~np.isnan(f)
    assert np.array_equal(got[real], np.clip(f[real], -2.5, 3.0)) and np.isnan(got[~real]).all()
    assert np.clip(x, min=0, max=1).equal_bits(np.clip(x, 0, 1)) and np.clip(x, None, 1).max().to_float() == 1.0
    assert np.isnan(np.clip(pn.array([1.0, 2.0]), pn.nar(), 3).to_float()).all()
    lo = pn.array([[0.0], [1.0]])
    assert np.clip(pn.array([0.5, 2.0]), lo, 1.5).to_float().tolist() == [[0.5, 1.5], [1.0, 1.5]]


@pytest.mark.parametrize("name", ["floor", "ceil", "trunc", "rint"])
def test_rounding_to_integers_exact_posit16_all(name):
    x = pn.from_bits(np.arange(1 << 16), P16)
    got = getattr(np, name)(x)
    assert np.array_equal(got.to_float(), getattr(np, name)(x.to_float()), equal_nan=True)
    assert getattr(x, name)().equal_bits(got)


def test_round():
    x = pn.array([0.5, 1.5, 2.5, -0.5, -1.7])
    assert np.round(x).to_float().tolist() == [0.0, 2.0, 2.0, -0.0, -2.0]
    with pytest.raises(TypeError, match="decimals"):
        np.round(x, 1)


# ---- cumsum / prod -------------------------------------------------------------------------------------------

def test_cumsum_every_prefix_exact():
    rng = np.random.default_rng(11)
    b = rng.integers(0, 1 << 16, 300)
    b[b == P16.nar] = 0
    b[200] = P16.nar
    x = pn.from_bits(b, P16)
    want, s = [], F(0)
    for i, v in enumerate(b):
        if i >= 200:
            want.append(P16.nar)
            continue
        s += val(P16, v)
        want.append(rnd(P16, s))
    assert bits(np.cumsum(x)) == want
    m = pn.array(rng.standard_normal((3, 5)), P32)
    rows = np.cumsum(m, axis=1)
    assert rows.shape == (3, 5) and all(rows[i].equal_bits(np.cumsum(m[i])) for i in range(3))
    assert np.cumsum(m).shape == (15,)


def test_prod_exact_and_saturating():
    rng = np.random.default_rng(13)
    for c, n in ((P16, 40), (P32, 1000)):
        x = pn.array(1 + rng.standard_normal(n) * 0.05, c)
        want = F(1)
        for v in x.bits:
            want *= val(c, v)
        assert int(np.prod(x).bits) == rnd(c, want)
    assert np.prod(pn.array([1e3] * 20, P16)).bits == P16.maxpos
    assert int(np.prod(pn.array([-1e-3] * 21, P16)).bits) == (-1) & P16.mask          # -minpos
    assert np.prod(pn.array([3.0, 0.0, 5.0])).to_float() == 0.0 and np.prod(pn.array([1.0, np.nan])).bits == P32.nar
    m = pn.array(rng.standard_normal((4, 3)))
    assert np.prod(m, axis=0).shape == (3,) and np.prod(pn.array([], P32)).to_float() == 1.0


# ---- files ------------------------------------------------------------------------------------------------

def test_save_load(tmp_path):
    x = pn.array(np.arange(12.0).reshape(3, 4) / 7, P16)
    pn.save(tmp_path / "x.npz", x)
    y = pn.load(tmp_path / "x.npz")
    assert y.config == P16 and y.equal_bits(x)
    z = PositComplex(pn.array([1.0, 2.0], P8), pn.array([-1.0, 0.5], P8))
    pn.save(tmp_path / "z.npz", z)
    w = pn.load(tmp_path / "z.npz")
    assert w.real.equal_bits(z.real) and w.imag.equal_bits(z.imag)
    with pytest.raises(TypeError, match="pn.save"):
        np.save(tmp_path / "x.npy", x)


# ---- PositComplex ----------------------------------------------------------------------------------------

def complex_pair(c, n, seed):
    rng = np.random.default_rng(seed)
    return [PositComplex(pn.array(rng.standard_normal(n) * 4, c), pn.array(rng.standard_normal(n) * 4, c))
            for _ in range(2)]


def test_complex_product_and_quotient_rounded_once():
    c = P16
    z, w = complex_pair(c, 300, 17)
    p, q = z * w, z / w
    for i in range(300):
        a, b, x, y = (val(c, t.bits[i]) for t in (z.real, z.imag, w.real, w.imag))
        assert int(p.real.bits[i]) == rnd(c, a * x - b * y) and int(p.imag.bits[i]) == rnd(c, a * y + b * x)
        d = x * x + y * y
        assert int(q.real.bits[i]) == rnd(c, (a * x + b * y) / d) and int(q.imag.bits[i]) == rnd(c, (b * x - a * y) / d)
    m = abs(z)
    for i in range(300):
        a, b = val(c, z.real.bits[i]), val(c, z.imag.bits[i])
        assert is_rounded_sqrt(c, int(m.bits[i]), a * a + b * b)
    assert np.multiply(z, w).real.equal_bits(p.real) and np.abs(z).equal_bits(m)


def test_complex_with_reals_and_numbers():
    z, w = complex_pair(P32, 50, 19)
    zc = z.to_complex()
    assert (z + 1).imag.equal_bits(z.imag) and np.allclose((1 - z).to_complex(), 1 - zc, rtol=1e-7)
    assert np.allclose((2 * z).to_complex(), 2 * zc) and np.allclose((z * (1 + 2j)).to_complex(), zc * (1 + 2j), rtol=1e-7)
    assert np.allclose((1 / z).to_complex(), 1 / zc, rtol=1e-7) and np.allclose((z / 2).to_complex(), zc / 2)
    r = pn.array(np.arange(1.0, 4.0)[:, None])                     # (3, 1) real against (50,) complex
    assert (z * r).shape == (3, 50) and (z + r).imag.shape == (3, 50)
    X = pn.fft.fft(pn.array(np.random.default_rng(1).standard_normal(64)))
    power = X * X.conj()
    assert np.all(power.imag.to_float() == 0)                      # a b - b a is exactly 0 in one rounding
    assert np.isnan((z / PositComplex(pn.zeros(50), pn.zeros(50))).to_complex().real).all()


# ---- linalg -------------------------------------------------------------------------------------------------

def solve_exact(A, b):
    n = len(b)
    M = [[F(A[i][j]) for j in range(n)] + [F(b[i])] for i in range(n)]
    for k in range(n):
        p = max(range(k, n), key=lambda i: abs(M[i][k]))
        M[k], M[p] = M[p], M[k]
        for i in range(k + 1, n):
            f = M[i][k] / M[k][k]
            M[i] = [M[i][j] - f * M[k][j] for j in range(n + 1)]
    x = [F(0)] * n
    for i in reversed(range(n)):
        x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]
    return np.array([float(v) for v in x])


@pytest.mark.parametrize("c", [P16, P32])
def test_solve_integer_system_exactly(c):
    rng = np.random.default_rng(c.n)
    A = rng.integers(-4, 5, (6, 6)).astype(float) + 9 * np.eye(6)
    x = rng.integers(-3, 4, 6).astype(float)
    X = np.linalg.solve(pn.array(A, c), pn.array(A @ x, c))
    assert X.config == c and np.array_equal(X.to_float(), x)
    B = np.stack([x, -x, 2 * x], axis=1)                          # several right-hand sides
    assert np.array_equal(pn.linalg.solve(pn.array(A, c), pn.array(A @ B, c)).to_float(), B)


def test_refinement_on_hilbert():
    n = 6
    H = pn.array([[1 / (i + j + 1) for j in range(n)] for i in range(n)])
    b = pn.array(H.to_float() @ np.ones(n))
    xe = solve_exact(H.to_float(), b.to_float())
    err = [np.abs(pn.linalg.solve(H, b, refine=r).to_float() - xe).max() for r in (0, 1, 2)]
    assert err[1] < err[0] / 100 and err[2] <= err[1]
    f32 = np.linalg.solve(H.to_float().astype(np.float32), b.to_float().astype(np.float32))
    assert err[1] < np.abs(f32 - xe).max()


def test_inv_det_and_errors():
    rng = np.random.default_rng(23)
    A = rng.standard_normal((5, 5)) + 3 * np.eye(5)
    Ai = np.linalg.inv(pn.array(A))
    assert np.abs(Ai.to_float() @ A - np.eye(5)).max() < 1e-6
    assert float(np.linalg.det(pn.array(A))) == pytest.approx(np.linalg.det(A), rel=1e-6)
    T = np.triu(rng.integers(1, 5, (4, 4)).astype(float))[[2, 0, 3, 1]]    # rows permuted: det = +-prod(diag)
    assert float(np.linalg.det(pn.array(T))) == pytest.approx(np.linalg.det(T), abs=0)
    S = np.array([[1.0, 2.0], [2.0, 4.0]])
    with pytest.raises(np.linalg.LinAlgError):
        np.linalg.solve(pn.array(S), pn.array([1.0, 1.0]))
    assert float(np.linalg.det(pn.array(S))) == 0.0
    assert np.isnan(np.linalg.solve(pn.array([[1.0, np.nan], [0, 1]]), pn.array([1.0, 1.0])).to_float()).all()
    with pytest.raises(np.linalg.LinAlgError):
        np.linalg.inv(pn.array(np.ones((2, 3))))
    X = np.linalg.solve(pn.array(A, P32), pn.array(A @ np.ones(5), P16))
    assert X.config == P32
