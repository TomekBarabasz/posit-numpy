"""PyPI packaging: the main wheel, the bitstream packages (packaging/make_overlay_packages.py) and installing an
application from a package with pn.board.install / positnp-board - on a PC, with the board faked."""

import importlib.util
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest

import positnp as pn
from positnp.board import Board, BoardError

SW = Path(__file__).resolve().parents[1]


def _setuptools_ok() -> bool:
    try:
        import setuptools
        return int(setuptools.__version__.split(".")[0]) >= 77
    except ImportError:
        return False


needs_setuptools = pytest.mark.skipif(not _setuptools_ok(), reason="building wheels needs setuptools >= 77 "
                                      "(pip install -U setuptools; Debian's system one cannot)")


def _maker():
    spec = importlib.util.spec_from_file_location("make_overlay_packages", SW / "packaging" / "make_overlay_packages.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fake_app(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True)
    (d / f"{name}.bit.bin").write_bytes(b"\x00\x01bitstream")
    (d / f"{name}.dtsi").write_text("/dts-v1/;\n/plugin/;\n")
    (d / "shell.json").write_text('{"shell_type": "XRT_FLAT", "num_slots": "1"}')
    (d / f"{name}.json").write_text('{"overlay": "%s", "engines": []}' % name)
    (d / "build.log").write_text("not packaged")
    return d


class FakeShell:
    """dtc, rm, install, xmutil listapps, systemctl: enough of the board for install()."""

    def __init__(self, fw: Path):
        self.fw, self.log = fw, []

    def run(self, cmd):
        self.log.append(cmd)
        args = cmd[2:] if cmd[:2] == ["sudo", "-n"] else cmd
        out = ""
        if args[0] == "dtc":
            Path(args[args.index("-o") + 1]).write_bytes(b"dtbo")
        elif args[:2] == ["rm", "-rf"]:
            shutil.rmtree(args[2], ignore_errors=True)
        elif args[0] == "install":
            dst = Path(args[-1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(args[-2], dst)
        elif args[:2] == ["xmutil", "listapps"]:
            out = "Accelerator Accel_type Base Base_type #slots(PL+AIE) Active_slot\n"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


def test_install_from_a_package_directory(tmp_path):
    pkgs, fw = tmp_path / "pkgs", tmp_path / "fw"
    _fake_app(pkgs, "posit16_8_ml")
    fw.mkdir()
    sh = FakeShell(fw)
    board = Board(fw, run=sh.run, sudo=True, overlays=lambda: [pkgs])
    assert list(board.available()) == ["posit16_8_ml"]
    with pytest.raises(BoardError, match=r"pn.board.install\('posit16_8_ml'\)"):
        board.app("posit16_8_ml")
    app = board.install("posit16_8_ml")
    assert app.name == "posit16_8_ml"
    assert sorted(p.name for p in (fw / "posit16_8_ml").iterdir()) == \
        ["posit16_8_ml.bit.bin", "posit16_8_ml.dtbo", "posit16_8_ml.json", "shell.json"]
    assert any(c[:3] == ["sudo", "-n", "install"] for c in sh.log)
    assert ["sudo", "-n", "systemctl", "restart", "dfx-mgr"] in sh.log        # listapps did not show it
    with pytest.raises(BoardError, match="no package provides"):
        board.install("posit99_alu")


def test_overlay_dirs_from_the_environment(tmp_path, monkeypatch):
    _fake_app(tmp_path / "extra", "posit32_16_linalg")
    monkeypatch.setenv("POSITNP_OVERLAYS", str(tmp_path / "extra"))
    assert pn.board.overlay_dirs()[0] == tmp_path / "extra"
    assert "posit32_16_linalg" in Board(tmp_path / "fw").available()


def test_cli_available(tmp_path, monkeypatch, capsys):
    _fake_app(tmp_path / "extra", "posit32_16_8_dsp")
    monkeypatch.setenv("POSITNP_OVERLAYS", str(tmp_path / "extra"))
    old = pn.board.use(Board(tmp_path / "fw"))
    try:
        assert pn.board.main(["available"]) == 0
        assert "posit32_16_8_dsp" in capsys.readouterr().out
        assert pn.board.main(["install", "posit00_none"]) == 1
    finally:
        pn.board.use(old)


def test_overlay_package_tree_and_wheel(tmp_path):
    m = _maker()
    src = _fake_app(tmp_path / "overlays", "posit16_8_ml")
    root = m.make("posit16_8_ml", src, tmp_path / "out", "9.9.9")
    mod = root / "positnp_kv260_ml"
    assert (mod / "posit16_8_ml" / "posit16_8_ml.bit.bin").exists() and not (mod / "posit16_8_ml" / "build.log").exists()
    py = (root / "pyproject.toml").read_text()
    assert 'name = "positnp-kv260-ml"' in py and '"positnp>=9.9.9"' in py and 'posit16_8_ml = "positnp_kv260_ml"' in py
    assert list(Board(tmp_path / "fw", overlays=lambda: [mod]).available()) == ["posit16_8_ml"]
    if not _setuptools_ok():
        pytest.skip("building wheels needs setuptools >= 77")
    m.wheel(root, tmp_path / "dist", isolation=False)
    whl = next((tmp_path / "dist").glob("positnp_kv260_ml-9.9.9-*.whl"))
    names = zipfile.ZipFile(whl).namelist()
    assert "positnp_kv260_ml/posit16_8_ml/posit16_8_ml.bit.bin" in names
    ep = next(n for n in names if n.endswith("entry_points.txt"))
    assert "[positnp.overlays]" in zipfile.ZipFile(whl).read(ep).decode()
    with pytest.raises(SystemExit, match="missing"):
        m.make("posit32_16_8_mem", tmp_path / "nothing", tmp_path / "out", "9.9.9")


@needs_setuptools
def test_main_wheel(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(SW / "positnp", src / "positnp", ignore=shutil.ignore_patterns("__pycache__"))
    _fake_app(src / "positnp" / "overlays", "posit32_16_8_mem")              # must not end up in the wheel
    for f in ("pyproject.toml", "README.pypi.md"):
        shutil.copy(SW / f, src / f)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w",
                    str(tmp_path / "dist"), str(src)], check=True)
    whl = next((tmp_path / "dist").glob(f"positnp-{pn.__version__}-*.whl"))
    z = zipfile.ZipFile(whl)
    names = z.namelist()
    assert "positnp/_data/build_udmabuf.sh" in names and "positnp/npapi.py" in names and "positnp/nn.py" in names
    assert not any(n.endswith(".bit.bin") for n in names)
    meta = z.read(next(n for n in names if n.endswith("METADATA"))).decode()
    assert "Provides-Extra: kv260-ml" in meta and 'positnp-kv260-ml; extra == "kv260-ml"' in meta
    assert "positnp-board = positnp.board:main" in z.read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
