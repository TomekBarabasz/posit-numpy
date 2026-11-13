"""ref.py against the Scala PositRef (shared digests) and known cases."""

import hashlib
import math
from pathlib import Path

import pytest

from positnp import ref
from positnp.ref import Flags, PositConfig

DIGESTS = Path(__file__).with_name("ref_digests.txt")


def lcg(n, seed, count):
    """The same generator as RefDigest.lcg in Scala."""
    s = seed
    for _ in range(count):
        s = (s * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        a = s >> (64 - n)
        s = (s * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        b = s >> (64 - n)
        yield a, b


def pairs(n, es, kind, count):
    if kind == "all":
        return ((a, b) for a in range(1 << n) for b in range(1 << n))
    return lcg(n, int(kind[3:]), count)


def entries():
    for line in DIGESTS.read_text().splitlines():
        if line and not line.startswith("#"):
            n, es, name, kind, count, digest = line.split()
            yield pytest.param(int(n), int(es), name, kind, int(count), digest, id=f"p{n}_{es}_{name}_{kind}")


QUIRE_OPS = {"qsum": "sum", "qdot": "dot", "qsumsq": "sumsq"}
STAGE5_OPS = ("sqrt", "fma", "f2p", "p2f")


def stage5_values(n, kind, count, name):
    """RefDigest.values: all n-bit values ("one") or LCG values (fma: triples)."""
    k = 3 if name == "fma" else 1
    if kind == "one":
        return list(range(1 << n))
    flat = [v for ab in lcg(n, int(kind[3:]), (count * k + 1) // 2) for v in ab]
    return flat[:count * k]


def stage5_ref(c, name, vals):
    if name == "sqrt":
        return [ref.sqrt(c, v) for v in vals]
    if name == "fma":
        return [ref.fma(c, *vals[i:i + 3]) for i in range(0, len(vals), 3)]
    if name == "f2p":
        return [ref.from_float32(c, v) for v in vals]
    return [ref.to_float32(c, v) for v in vals]


def quire_vectors(n, es, kind, count):
    """Pairs split into vectors of length 1, 2, ..., 37, 1, 2, ... (RefDigest.quireVectors)."""
    it = iter(list(pairs(n, es, kind, count)))
    i = 0
    while True:
        v = [p for _, p in zip(range(1 + i % 37), it)]
        if not v:
            return
        yield v
        i += 1


def quire_line(c, mode, vec, accumulate):
    a = [x for x, _ in vec]
    b = [y for _, y in vec]
    q, nar = accumulate(c, mode, a, b)
    r, f = ref.quire_round(c, q, nar)
    raw = "nar" if nar else f"{ref.quire_raw(c, q):x}"
    return f"{r:x} {f.bits} {raw}\n"


@pytest.mark.parametrize("n,es,name,kind,count,digest", list(entries()))
def test_digest_matches_scala(n, es, name, kind, count, digest):
    c = PositConfig(n, es)
    md = hashlib.sha256()
    if name in QUIRE_OPS:
        for vec in quire_vectors(n, es, kind, count):
            md.update(quire_line(c, QUIRE_OPS[name], vec, ref.quire_accumulate).encode("ascii"))
    elif name in STAGE5_OPS:
        for r, f in stage5_ref(c, name, stage5_values(n, kind, count, name)):
            md.update(f"{r:x} {f.bits}\n".encode("ascii"))
    else:
        for a, b in pairs(n, es, kind, count):
            r, f = ref.op(c, name, a, b)
            md.update(f"{r:x} {f.bits}\n".encode("ascii"))
    assert md.hexdigest() == digest


P8 = PositConfig(8)
ONE, TWO, THREE, NAR, MAXPOS, MINPOS = 0x40, 0x48, 0x4C, 0x80, 0x7F, 0x01


def test_known_values():
    assert ref.op(P8, "add", ONE, ONE) == (TWO, Flags())
    assert ref.op(P8, "div", TWO, TWO) == (ONE, Flags())
    assert ref.op(P8, "div", ONE, 0) == (NAR, Flags(nar=True))
    assert ref.op(P8, "mul", MAXPOS, MAXPOS) == (MAXPOS, Flags(sat=True, inexact=True))
    assert ref.op(P8, "mul", MINPOS, MINPOS) == (MINPOS, Flags(sat=True, inexact=True))
    assert ref.op(P8, "div", ONE, THREE)[1] == Flags(inexact=True)
    for name in ref.OPS:
        assert ref.op(P8, name, NAR, ONE) == (NAR, Flags(nar=True))


def test_roundtrip_float_posit16():
    c = PositConfig(16)
    for raw in range(1 << 16):
        x = ref.decode(c, raw)
        if x is None:
            assert math.isnan(ref.to_float(c, raw))
        else:
            assert ref.encode(c, x) == raw
            assert ref.to_float(c, raw) == math.ldexp(*x)


def test_flags_bits_roundtrip():
    for b in range(8):
        assert Flags.from_bits(b).bits == b


def test_config_validation():
    with pytest.raises(ValueError):
        PositConfig(5, 2)
