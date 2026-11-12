"""PositAluHw driver against FakeAluMmio (register model on ref.py): runs on any PC."""

import random

import pytest

from positnp import ref, regs
from positnp.backend_hw import FakeAluMmio, PositAluError, PositAluHw
from positnp.ref import Flags, PositConfig


@pytest.fixture
def alu():
    return PositAluHw(FakeAluMmio(PositConfig(16), busy_reads=3))


def test_config(alu):
    assert alu.config == PositConfig(16, 2)
    assert alu.ops == ("add", "sub", "mul", "div")


def test_ops_match_ref(alu):
    rnd = random.Random(1)
    for name in ref.OPS:
        for _ in range(200):
            a, b = rnd.getrandbits(16), rnd.getrandbits(16)
            assert alu.op(name, a, b) == ref.op(alu.config, name, a, b)


def test_register_sequence(alu):
    alu.mmio.log.clear()
    alu.op("mul", 0x4800, 0x4800)
    writes = [e[1:] for e in alu.mmio.log if e[0] == "w"]
    assert writes == [(regs.OP, regs.OP_MUL), (regs.OPERAND_A, 0x4800), (regs.OPERAND_B, 0x4800),
                      (regs.CTRL, 1 << regs.CTRL_START)]


def test_counters(alu):
    alu.clear_stats()
    alu.op("div", 0x4000, 0)
    alu.op("mul", 0x7FFF, 0x7FFF)
    s = alu.stats()
    assert (s["nar"], s["sat"], s["inexact"]) == (1, 1, 1)
    alu.clear_stats()
    assert alu.stats() == {"nar": 0, "sat": 0, "inexact": 0, "cycles": 0}


def test_flags_in_status(alu):
    _, f = alu.op("div", 0x4000, 0x4C00)
    assert f == Flags(inexact=True)
    assert alu.status()["flags"] == Flags(inexact=True)


def test_missing_op():
    alu = PositAluHw(FakeAluMmio(PositConfig(32), ops=("add", "mul")))
    assert alu.ops == ("add", "mul")
    with pytest.raises(PositAluError):
        alu.op("div", 1, 1)
    with pytest.raises(ValueError):
        alu.op("pow", 1, 1)


def test_wrong_ip():
    class Zero:
        def read(self, offset):
            return 0

        def write(self, offset, value):
            pass

    with pytest.raises(PositAluError):
        PositAluHw(Zero())


def test_mapped_words_on_a_file(tmp_path):
    """_MappedWords (the base of UioMmio / DevMemMmio) on a plain file: word offsets, 32-bit wrap."""
    import os

    from positnp.backend_hw import _MappedWords

    f = tmp_path / "regs.bin"
    f.write_bytes(bytes(4096))
    fd = os.open(f, os.O_RDWR)
    try:
        m = _MappedWords(fd, 4096, 0)
    finally:
        os.close(fd)
    m.write(regs.OPERAND_A, 0x1_2345_6789)
    m.write(regs.OPERAND_B, 0xDEADBEEF)
    assert m.read(regs.OPERAND_A) == 0x2345_6789
    assert m.read(regs.OPERAND_B) == 0xDEADBEEF
    m.close()
    raw = f.read_bytes()
    assert raw[regs.OPERAND_A:regs.OPERAND_A + 4] == (0x23456789).to_bytes(4, "little")


def test_find_uio(tmp_path, monkeypatch):
    from positnp import backend_hw

    for n, name in ((0, "axi-pmon"), (3, "posit_alu")):
        d = tmp_path / f"uio{n}"
        d.mkdir()
        (d / "name").write_text(name + "\n")
    monkeypatch.setattr(backend_hw, "UIO_CLASS", tmp_path)
    assert backend_hw.find_uio("posit_alu") == "/dev/uio3"
    assert backend_hw.find_uio("nope") is None
