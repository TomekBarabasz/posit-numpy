"""Quire models (ref.py, vecops): exact against Fractions, cancellation, order independence."""

from fractions import Fraction

import numpy as np
import pytest

from positnp import ref, vecops
from positnp.ref import PositConfig

P16, P32 = PositConfig(16), PositConfig(32)


def exact_value(c, mode, a, b):
    def val(x):
        d = ref.decode(c, x)
        return Fraction(d[0]) * Fraction(2) ** d[1]
    if mode == "sum":
        return sum(val(x) for x in a)
    if mode == "sumsq":
        return sum(val(x) ** 2 for x in a)
    return sum(val(x) * val(y) for x, y in zip(a, b))


@pytest.mark.parametrize("c", [P16, P32])
@pytest.mark.parametrize("mode", ["sum", "dot", "sumsq"])
def test_quire_is_exact(c, mode):
    rnd = np.random.default_rng(c.n)
    for size in (1, 2, 7, 300):
        a = rnd.integers(0, 1 << c.n, size)
        b = rnd.integers(0, 1 << c.n, size)
        a[a == c.nar] = 0
        b[b == c.nar] = 0
        q, nar = vecops.quire(c, mode, a, b)
        assert not nar
        assert Fraction(q, 1 << c.quire_frac) == exact_value(c, mode, a.tolist(), b.tolist())


def test_cancellation_and_order():
    c = P32
    big, small = ref.encode(c, (1, 100)), ref.encode(c, (3, -200))
    q, nar = vecops.quire(c, "sum", np.array([big, small, (-big) & c.mask]))
    assert vecops.quire_round(c, q, nar).bits[0] == small                # exact: no posit-add would keep `small`
    rnd = np.random.default_rng(1)
    a = rnd.integers(0, 1 << 32, 5000)
    b = rnd.integers(0, 1 << 32, 5000)
    a[a == c.nar] = 0
    b[b == c.nar] = 0
    perm = rnd.permutation(5000)
    assert vecops.quire(c, "dot", a, b) == vecops.quire(c, "dot", a[perm], b[perm])


def test_nar_and_raw():
    c = P16
    assert vecops.quire(c, "dot", np.array([0x4000, 0x8000]), np.array([1, 1])) == (0, True)
    assert ref.quire_round(c, 0, True)[0] == c.nar
    q = -12345 << 7
    assert ref.quire_from_raw(c, ref.quire_raw(c, q)) == q
    assert c.quire_width() == 256 and P32.quire_width() == 512
