"""vecops (NumPy) against ref.py and the shared digests from PositRef.scala."""

import hashlib
import math

import numpy as np
import pytest

from positnp import ref, vecops
from positnp.ref import PositConfig

from test_ref import QUIRE_OPS, entries, lcg, quire_line, quire_vectors

DIGEST_ENTRIES = list(entries())


def _vec_quire(c, mode, a, b):
    return vecops.quire(c, mode, np.array(a, dtype=np.int64), np.array(b, dtype=np.int64))


@pytest.mark.parametrize("n,es,name,kind,count,digest", DIGEST_ENTRIES)
def test_digest_matches_scala(n, es, name, kind, count, digest):
    c = PositConfig(n, es)
    if name in QUIRE_OPS:
        md = hashlib.sha256()
        for vec in quire_vectors(n, es, kind, count):
            md.update(quire_line(c, QUIRE_OPS[name], vec, _vec_quire).encode("ascii"))
        assert md.hexdigest() == digest
        return
    if kind == "all":
        a, b = np.meshgrid(np.arange(1 << n), np.arange(1 << n), indexing="ij")
        a, b = a.ravel(), b.ravel()
    else:
        a, b = (np.array(x, dtype=np.int64) for x in zip(*lcg(n, int(kind[3:]), count)))
    r = vecops.op(c, name, a, b)
    md = hashlib.sha256("".join(f"{x:x} {f}\n" for x, f in zip(r.bits.tolist(), r.flags.tolist())).encode("ascii"))
    assert md.hexdigest() == digest


@pytest.mark.parametrize("n,es", [(8, 0), (8, 2), (10, 1), (12, 3), (16, 2)])
def test_decode_roundtrip_exhaustive(n, es):
    c = PositConfig(n, es)
    bits = np.arange(1 << n)
    r = vecops.roundtrip(c, vecops.decode(c, bits))
    assert np.array_equal(r.bits, bits)
    assert not r.sat.any() and not r.inexact.any()
    assert r.nar.sum() == 1


@pytest.mark.parametrize("n,es", [(8, 2), (16, 2), (32, 2), (12, 3)])
def test_to_float_matches_ref(n, es):
    c = PositConfig(n, es)
    bits = np.arange(1 << n) if n <= 16 else np.random.default_rng(3).integers(0, 1 << n, 50_000)
    got = vecops.to_float(c, bits)
    want = np.array([ref.to_float(c, int(b)) for b in bits.tolist()])
    assert np.array_equal(got, want, equal_nan=True)


@pytest.mark.parametrize("n,es", [(8, 2), (16, 2), (32, 2), (12, 3)])
def test_from_float_matches_ref(n, es):
    c = PositConfig(n, es)
    rnd = np.random.default_rng(n)
    x = np.concatenate([
        rnd.standard_normal(20_000) * 10.0 ** rnd.integers(-12, 12, 20_000),
        vecops.to_float(c, rnd.integers(0, 1 << n, 2_000)),               # exact posits
        [0.0, -0.0, 1e300, -1e300, 1e-300, 5e-324, np.inf, -np.inf, np.nan]])
    want = []
    for v in x.tolist():
        if math.isnan(v) or math.isinf(v):
            want.append(c.nar)
        elif v == 0:
            want.append(0)
        else:
            num, den = v.as_integer_ratio()
            want.append(ref.encode(c, (num, -(den.bit_length() - 1))))
    assert np.array_equal(vecops.from_float(c, x).bits, np.array(want))


def test_from_float_ties_to_even_posit16():
    c = PositConfig(16)
    # neighbours 0x4000 (1.0, even) and 0x4001: midpoint rounds to the even one
    mid = (vecops.to_float(c, 0x4000) + vecops.to_float(c, 0x4001)) / 2
    assert vecops.from_float(c, [mid]).bits[0] == 0x4000
    mid = (vecops.to_float(c, 0x4001) + vecops.to_float(c, 0x4002)) / 2
    assert vecops.from_float(c, [mid]).bits[0] == 0x4002
    assert vecops.from_float(c, [np.nextafter(mid, 0)]).bits[0] == 0x4001


def test_level0():
    c = PositConfig(8)
    bits = np.arange(256)
    s = vecops.to_signed(c, bits)
    assert s.min() == -128 and vecops.to_signed(c, [c.nar])[0] == -128      # NaR is the minimum
    f = vecops.to_float(c, bits)
    order = np.argsort(s[bits != c.nar])
    assert np.all(np.diff(f[bits != c.nar][order]) > 0)                       # integer order == value order
    assert np.array_equal(vecops.neg(c, vecops.neg(c, bits)), bits)
    assert vecops.neg(c, [c.nar])[0] == c.nar and vecops.absolute(c, [c.nar])[0] == c.nar
    assert np.array_equal(vecops.to_float(c, vecops.absolute(c, bits))[bits != c.nar], np.abs(f[bits != c.nar]))
