"""Property tests (hypothesis): hardware path on the fakes == software path; float conversion == ref.py."""

import numpy as np
import pytest

pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.extra import numpy as hnp  # noqa: E402

import positnp as pn  # noqa: E402
from test_array import fake_hw  # noqa: E402


@settings(max_examples=40, deadline=None)
@given(n=st.sampled_from([8, 16, 32]), name=st.sampled_from(["add", "sub", "mul", "div"]),
       data=st.data())
def test_hw_equals_sw(n, name, data):
    c = pn.PositConfig(n)
    size = data.draw(st.integers(0, 3000))
    a = data.draw(hnp.arrays(np.int64, size, elements=st.integers(0, (1 << n) - 1)))
    b = data.draw(hnp.arrays(np.int64, size, elements=st.integers(0, (1 << n) - 1)))
    x, y = pn.from_bits(a, c), pn.from_bits(b, c)
    want = x._binary(name, y)
    with pn.using(fake_hw(c)):
        got = x._binary(name, y)
        got_s = x._binary(name, pn.from_bits(b[:1] if size else [0], c))
    assert got.equal_bits(want)
    assert got_s.equal_bits(x._binary(name, pn.from_bits(b[:1] if size else [0], c)))


@settings(max_examples=60, deadline=None)
@given(hnp.arrays(np.float64, st.integers(1, 200), elements=st.floats(allow_nan=True, allow_infinity=True)))
def test_float_conversion_matches_ref(x):
    from positnp import ref
    c = pn.P32
    got = pn.array(x, c).bits.astype(np.int64)
    want = []
    for v in x.tolist():
        if v != v or v in (float("inf"), float("-inf")):
            want.append(c.nar)
        elif v == 0:
            want.append(0)
        else:
            num, den = v.as_integer_ratio()
            want.append(ref.encode(c, (num, -(den.bit_length() - 1))))
    assert got.tolist() == want


