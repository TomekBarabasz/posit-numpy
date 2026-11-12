"""Stage 1 on the KV260: PositAluHw (MMIO) against ref.py, bit for bit.

Runs only where pynq imports and the overlay exists; elsewhere every test is skipped.
    sudo -E python3 -m pytest -v tests/test_board.py                       # overlays/posit32_alu.bit
    POSITNP_BITFILE=/path/posit8_alu.bit sudo -E python3 -m pytest tests/test_board.py
    POSITNP_VECTORS=100000 ...                                             # pairs per operation (default 20000)
posit<8,*> bitstreams are checked exhaustively (all 65 536 pairs per operation).
"""

import os
import random
from pathlib import Path

import pytest

from positnp import ref, regs
from positnp.backend_hw import OVERLAY_DIR, PositAluHw
from positnp.ref import Flags

pynq = pytest.importorskip("pynq")

BITFILE = Path(os.environ.get("POSITNP_BITFILE", OVERLAY_DIR / "posit32_alu.bit"))
VECTORS = int(os.environ.get("POSITNP_VECTORS", "20000"))

if not BITFILE.exists():
    pytest.skip(f"no overlay {BITFILE}", allow_module_level=True)


@pytest.fixture(scope="module")
def alu():
    hw = PositAluHw.from_overlay(str(BITFILE))
    hw.soft_reset()
    return hw


def test_id_and_config(alu):
    assert alu.read(regs.ID_VERSION) == regs.ID << 16 | regs.VERSION
    n = int(BITFILE.stem.split("_")[0].removeprefix("posit"))
    assert alu.config.n == n and alu.config.es == 2
    print(f"\n{alu}")


@pytest.mark.parametrize("name", ref.OPS)
def test_op_matches_ref(alu, name):
    c = alu.config
    if c.n <= 8:
        pairs = ((a, b) for a in range(1 << c.n) for b in range(1 << c.n))
    else:
        rnd = random.Random(f"board-{name}")
        pairs = ((rnd.getrandbits(c.n), rnd.getrandbits(c.n)) for _ in range(VECTORS))
    bad = []
    count = 0
    for a, b in pairs:
        got = alu.op(name, a, b)
        want = ref.op(c, name, a, b)
        count += 1
        if got != want:
            bad.append((a, b, got, want))
            if len(bad) >= 10:
                break
    assert not bad, f"{len(bad)} mismatches, first: " + ", ".join(
        f"a=0x{a:x} b=0x{b:x} -> 0x{g[0]:x} {g[1]}, ref 0x{w[0]:x} {w[1]}" for a, b, g, w in bad)
    print(f"\n{name}: {count} pairs bit for bit")


def test_counters_known_cases(alu):
    c = alu.config
    one = ref.encode(c, (1, 0))
    three = ref.encode(c, (3, 0))
    cases = [("div", one, 0, (1, 0, 0)),               # 1 / 0 = NaR
             ("mul", c.maxpos, c.maxpos, (0, 1, 1)),   # saturation
             ("div", one, three, (0, 0, 1)),           # 1/3 inexact
             ("add", one, one, (0, 0, 0))]
    for name, a, b, want in cases:
        alu.clear_stats()
        alu.op(name, a, b)
        s = alu.stats()
        assert (s["nar"], s["sat"], s["inexact"]) == want, f"{name}(0x{a:x}, 0x{b:x}): {s}"


def test_timing(alu):
    t = alu.time_op("add", 0, 0, repeat=10_000)
    alu.clear_stats()
    alu.op("div", ref.encode(alu.config, (1, 0)), ref.encode(alu.config, (3, 0)))
    print(f"\none op() through MMIO: {t * 1e6:.2f} us; division busy {alu.stats()['cycles']} cycles")
