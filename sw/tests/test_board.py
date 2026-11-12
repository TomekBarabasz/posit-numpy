"""Stage 1 on the KV260: PositAluHw (UIO) against ref.py, bit for bit. No PYNQ.

Runs only where the overlay is loaded (a UIO device named "posit_alu" exists); elsewhere
every test is skipped. From sw/, after scripts/install_overlay.sh posit32_alu:
    sudo python3 -m pytest -v tests/test_board.py
    sudo POSITNP_VECTORS=100000 python3 -m pytest -v tests/test_board.py   # pairs per operation (default 20000)
posit<8,*> overlays are checked exhaustively (all 65 536 pairs per operation).
"""

import os
import random

import pytest

from positnp import ref, regs
from positnp.backend_hw import IP_NAME, PositAluHw, find_uio

VECTORS = int(os.environ.get("POSITNP_VECTORS", "20000"))

if find_uio(IP_NAME) is None:
    pytest.skip(f"no UIO device {IP_NAME!r} (overlay not loaded)", allow_module_level=True)


@pytest.fixture(scope="module")
def alu():
    hw = PositAluHw.open()
    hw.soft_reset()
    return hw


def test_id_and_config(alu):
    assert alu.read(regs.ID_VERSION) == regs.ID << 16 | regs.VERSION
    assert alu.config.es == 2 and alu.config.n in (8, 16, 32)
    assert alu.ops == ("add", "sub", "mul", "div")
    print(f"\n{alu} on {alu.mmio.device}")


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
    print(f"\none op() through UIO: {t * 1e6:.2f} us; division busy {alu.stats()['cycles']} cycles")
