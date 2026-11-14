"""pn.board on a PC: firmware directory, xmutil, /sys/class/uio and u-dma-buf all faked."""

import json
import types

import numpy as np
import pytest

import positnp as pn
from positnp import backend_hw, udmabuf
from positnp.backend_hw import PositVecHw
from positnp.board import Board, BoardError
from positnp.fakes import FakeDma, FakeVecMmio
from positnp.ref import PositConfig
from positnp.udmabuf import FakeUDmaBuf


def _app(fw, name, info=None):
    d = fw / name
    d.mkdir(parents=True)
    (d / f"{name}.bit.bin").write_bytes(b"\0")
    if info is not None:
        (d / f"{name}.json").write_text(json.dumps(info))


def _engines(ns, units=1):
    return [{"n": n, "ip": f"posit_vec_p{n}", "dma": f"posit_dma_p{n}", "udmabuf": f"udmabuf-posit-p{n}",
             "buf_size": 1 << 16} for n in ns]


class FakeKv260:
    """xmutil + device nodes: loadapp creates the UIO / u-dma-buf entries of the app's json, unloadapp removes them."""

    def __init__(self, fw, uio, bufs, appear=True):
        self.fw, self.uio, self.bufs, self.appear = fw, uio, bufs, appear
        self.active = None
        self.log = []

    def _devices(self, name):
        info = json.loads((self.fw / name / f"{name}.json").read_text()) if (self.fw / name / f"{name}.json").exists() else {}
        es = info.get("engines", [])
        if not es and info.get("ip"):
            return [info["ip"]], []
        return [x for e in es for x in (e["ip"], e["dma"])], [e["udmabuf"] for e in es]

    def run(self, cmd):
        self.log.append(cmd)
        args = cmd[2:] if cmd[:2] == ["sudo", "-n"] else cmd
        out, rc = "", 0
        if args[:2] == ["xmutil", "listapps"]:
            rows = [f"{a.name:>24} XRT_FLAT {a.name:>24} XRT_FLAT (0+0) {'0,' if a.name == self.active else '-1'}"
                    for a in sorted(self.fw.iterdir())]
            out = "Accelerator Accel_type Base Base_type #slots(PL+AIE) Active_slot\n\n" + "\n".join(rows)
        elif args[:2] == ["xmutil", "unloadapp"]:
            if self.active is None:
                rc = 1
            for p in list(self.uio.iterdir()) + list(self.bufs.iterdir()):
                for f in p.iterdir():
                    f.unlink()
                p.rmdir()
            self.active = None
        elif args[:2] == ["xmutil", "loadapp"]:
            self.active = args[2]
            uios, bufs = self._devices(args[2])
            if self.appear:
                for i, nm in enumerate(uios):
                    (self.uio / f"uio{i}").mkdir()
                    (self.uio / f"uio{i}" / "name").write_text(nm + "\n")
                for b in bufs:
                    (self.bufs / b).mkdir()
                    (self.bufs / b / "size").write_text("65536")
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")


def _fake_open(e):
    mem = FakeUDmaBuf(1 << 16)
    vec = FakeVecMmio(PositConfig(e.n), units=2)
    hw = PositVecHw(vec, FakeDma(mem, vec), mem)
    hw.engine = e
    return hw


@pytest.fixture
def kv(tmp_path, monkeypatch):
    fw, uio, bufs = tmp_path / "fw", tmp_path / "uio", tmp_path / "udmabuf"
    for d in (fw, uio, bufs):
        d.mkdir()
    monkeypatch.setattr(backend_hw, "UIO_CLASS", uio)
    monkeypatch.setattr(udmabuf, "SYS_CLASS", bufs)
    _app(fw, "posit32_16_8_vec", {"overlay": "posit32_16_8_vec", "engines": _engines([32, 16, 8])})
    _app(fw, "posit32_alu", {"overlay": "posit32_alu", "ip": "posit_alu"})
    _app(fw, "k26-starter-kits")
    fake = FakeKv260(fw, uio, bufs)
    board = Board(fw, run=fake.run, sudo=True, open_engine=_fake_open, timeout=0.3)
    old = pn.board.use(board)
    yield fake, board
    board.close()
    pn.board.use(old)
    pn.set_backend(None)


def test_apps_and_kinds(kv):
    apps = {a.name: a for a in pn.board.apps()}
    assert set(apps) == {"posit32_16_8_vec", "posit32_alu", "k26-starter-kits"}
    assert [e["n"] for e in apps["posit32_16_8_vec"].engines] == [32, 16, 8]
    assert (apps["posit32_16_8_vec"].kind, apps["posit32_alu"].kind, apps["k26-starter-kits"].kind) == ("vec", "alu", "other")
    assert pn.board.active() is None


def test_load_registers_every_engine(kv):
    fake, _ = kv
    hw = pn.board.load("posit32_16_8_vec")
    assert sorted(hw) == [8, 16, 32] and pn.board.active() == "posit32_16_8_vec"
    assert all(c[:2] == ["sudo", "-n"] for c in fake.log)
    assert ["sudo", "-n", "xmutil", "loadapp", "posit32_16_8_vec"] in fake.log
    for n, c in ((32, pn.P32), (16, pn.P16), (8, pn.P8)):
        assert pn.get_backend(c) is hw[n]
    x = pn.array(np.linspace(-3, 3, 501), pn.P16)
    want = pn.array(np.linspace(-3, 3, 501), pn.P16)
    hw[16].clear_stats()
    got = x * x - x / 3
    assert hw[16].stats()["elements"] == 3 * 501 and hw[32].stats()["elements"] == 0
    with pn.using(None):
        assert got.equal_bits(want * want - want / 3)
    text = pn.board.describe()
    assert "posit_vec_p16" in text and "2 lanes" in text and "quire" in text
    assert [r["n"] for r in pn.board.info()] == [32, 16, 8]


def test_load_again_does_not_reload(kv):
    fake, _ = kv
    pn.board.load("posit32_16_8_vec")
    n = sum(c[2:4] == ["xmutil", "loadapp"] for c in fake.log)
    pn.board.load("posit32_16_8_vec")
    assert sum(c[2:4] == ["xmutil", "loadapp"] for c in fake.log) == n          # already active: open only
    pn.board.load("posit32_16_8_vec", force=True)
    assert sum(c[2:4] == ["xmutil", "loadapp"] for c in fake.log) == n + 1


def test_unload_closes_and_falls_back(kv):
    hw = pn.board.load("posit32_16_8_vec")
    pn.board.unload()
    assert pn.board.active() is None and pn.board.engines() == {}
    assert isinstance(pn.get_backend(pn.P32), pn.PositSw)
    with pytest.raises(backend_hw.PositAluError, match="closed"):
        hw[32].array_op("add", np.arange(4), np.arange(4))
    assert (pn.array([1.0, 2.0]) * 2).to_float().tolist() == [2.0, 4.0]       # software again


def test_other_backends_survive(kv):
    mine = _fake_open(backend_hw.VecEngine("posit_vec_p32", "posit_dma_p32", "udmabuf-posit-p32", 32))
    pn.set_backend(mine)
    pn.board.load("posit32_16_8_vec")                       # replaces posit32 by the board's engine
    assert pn.get_backend(pn.P32) is pn.board.engine(32)
    pn.board.close()
    assert isinstance(pn.get_backend(pn.P32), pn.PositSw)


def test_errors(kv):
    fake, board = kv
    with pytest.raises(BoardError, match="not installed.*install_overlay.sh"):
        pn.board.load("posit99_vec")
    fake.appear = False
    with pytest.raises(BoardError, match="no device posit_vec_p32"):
        pn.board.load("posit32_16_8_vec")
    with pytest.raises(BoardError, match="no open posit<16>"):
        pn.board.engine(16)


def test_alu_app_loads_without_engines(kv):
    fake, _ = kv
    assert pn.board.load("posit32_alu") == {}
    assert pn.board.active() == "posit32_alu"
