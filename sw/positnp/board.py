"""pn.board: PL applications on the KV260 from Python (Ubuntu + xmutil, no PYNQ).

    import positnp as pn
    pn.board.apps()                    # installed: [App('posit32_16_8_vec', engines 32/16/8), App('posit32_alu'), ...]
    pn.board.active()                  # 'posit32_16_8_vec' (xmutil listapps), None when nothing is loaded
    hw = pn.board.load("posit32_16_8_vec")   # {32: PositVecHw, 16: ..., 8: ...}, all set as backends
    x = pn.array([1.0, 2.0], pn.P16)   # posit16 arrays now run on the posit16 engine, posit32 on posit32
    print(pn.board.describe())         # engines, operations, lanes, buffers
    pn.board.unload()                  # close the engines, xmutil unloadapp, everything back to PositSw

Applications are the directories in /lib/firmware/xilinx/ (.bit.bin, .dtbo, shell.json and <name>.json from
build.tcl with the list of engines). pn.board.install(name) puts one there from a package: the bitstream
packages installed with the extras (pip install positnp[kv260-dsp] -> positnp-kv260-dsp, found through the
entry point group "positnp.overlays"), positnp/overlays/<name>/ of a source checkout, or a directory in
$POSITNP_OVERLAYS. From a shell: positnp-board available | install NAME | load NAME | describe | udmabuf. Loading needs
root: run Python with sudo, or allow `sudo -n xmutil` (and `sudo -n modprobe u-dma-buf`) for the user,
e.g. /etc/sudoers.d/positnp:  <user> ALL=(root) NOPASSWD: /usr/bin/xmutil, /usr/sbin/modprobe u-dma-buf
and give the user access to /dev/uio* and /dev/udmabuf-* with a udev rule.

Only one application is loaded at a time (the KV260 shell is flat); several formats at once come from
one bitstream with several engines (build.tcl -engines "32 16 8").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import importlib

from . import backend_hw, udmabuf

_array = importlib.import_module(".array", __package__)   # module (positnp.array is the function)

FW_DIR = Path("/lib/firmware/xilinx")
OVERLAY_GROUP = "positnp.overlays"           # entry points of the bitstream packages
APP_FILES = (".bit.bin", ".dtsi", "shell.json")
UDMABUF_SCRIPT = Path(__file__).with_name("_data") / "build_udmabuf.sh"


def overlay_dirs() -> List[Path]:
    """Directories that hold application directories: $POSITNP_OVERLAYS, the bitstream packages (entry points
    "positnp.overlays": a module whose directory holds <name>/), positnp/overlays of a source checkout."""
    dirs = [Path(p) for p in os.environ.get("POSITNP_OVERLAYS", "").split(os.pathsep) if p]
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group=OVERLAY_GROUP):
            try:
                mod = ep.load()
            except Exception:                                  # a broken package must not break the board
                continue
            dirs.append(Path(getattr(mod, "OVERLAY_DIR", Path(mod.__file__).parent)))
    except ImportError:
        pass
    dirs.append(Path(__file__).with_name("overlays"))
    return dirs


class BoardError(RuntimeError):
    pass


@dataclass
class App:
    """An installed PL application: /lib/firmware/xilinx/<name>/ and its <name>.json (when present)."""
    name: str
    path: Path
    info: Dict = field(default_factory=dict)

    @property
    def engines(self) -> List[Dict]:
        """Engines from build.tcl -engines ({n, ip, dma, udmabuf, ...}); [] for other applications."""
        if self.info.get("engines"):
            return list(self.info["engines"])
        if self.info.get("ip") == backend_hw.VEC_NAME:          # one engine, before -engines
            return [{"n": self.info.get("n"), "ip": self.info["ip"], "dma": self.info.get("dma"),
                     "udmabuf": self.info.get("udmabuf"), "buf_size": self.info.get("buf_size")}]
        return []

    @property
    def kind(self) -> str:
        if self.engines:
            return "vec"
        if self.info.get("ip") == backend_hw.IP_NAME:
            return "alu"
        return "other"

    def __repr__(self) -> str:
        ns = [e.get("n") for e in self.engines]
        what = f"engines {'/'.join(str(n) for n in ns)}" if ns else self.kind
        return f"App({self.name!r}, {what})"


def _default_run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1800)


class Board:
    """The KV260 seen from Python. pn.board.* are the methods of one shared instance.

    run, fw_dir, sudo and open_engine can be replaced (tests run it all on a PC with fakes).
    """

    def __init__(self, fw_dir: Path = FW_DIR, run: Optional[Callable] = None, sudo: Optional[bool] = None,
                 open_engine: Optional[Callable] = None, timeout: float = 10.0,
                 overlays: Optional[Callable[[], List[Path]]] = None):
        self.fw_dir = Path(fw_dir)
        self._run = run or _default_run
        self.overlay_dirs = overlays or overlay_dirs
        self.sudo = (os.geteuid() != 0) if sudo is None else sudo
        self._open_engine = open_engine or backend_hw.PositVecHw.open_engine
        self.timeout = timeout
        self._engines: Dict[int, backend_hw.PositVecHw] = {}
        self._loaded: Optional[str] = None

    # ---- installed applications -------------------------------------------------------------------

    def apps(self) -> List[App]:
        """Installed applications (directories with a .bit.bin), sorted by name."""
        out = []
        if self.fw_dir.is_dir():
            for d in sorted(self.fw_dir.iterdir()):
                if not d.is_dir() or not any(d.glob("*.bit.bin")):
                    continue
                info = {}
                js = d / f"{d.name}.json"
                if js.exists():
                    try:
                        info = json.loads(js.read_text())
                    except (OSError, ValueError):
                        info = {}
                out.append(App(d.name, d, info))
        return out

    def app(self, name: str) -> App:
        for a in self.apps():
            if a.name == name:
                return a
        have = ", ".join(a.name for a in self.apps()) or "none"
        hint = f"pn.board.install({name!r})" if name in self.available() else \
            "pip install positnp[kv260] (or -dsp / -ml / -linalg), then pn.board.install(name)"
        raise BoardError(f"application {name!r} is not installed in {self.fw_dir} (installed: {have}); {hint}")

    # ---- applications shipped with packages -------------------------------------------------------

    def available(self) -> Dict[str, Path]:
        """Applications that can be installed: {name: source directory} (first one found wins)."""
        out: Dict[str, Path] = {}
        for base in self.overlay_dirs():
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if d.is_dir() and d.name not in out and all((d / (f if f == "shell.json" else d.name + f)).exists()
                                                             for f in APP_FILES):
                    out[d.name] = d
        return out

    def install(self, name: str, load: bool = False):
        """Install application `name` into the firmware directory: the device tree overlay compiled with dtc
        (.dtsi -> .dtbo), the bitstream, shell.json and <name>.json copied (with sudo -n when not root).
        load=True: then load() it. Returns the App (or the engines, with load=True)."""
        src = self.available().get(name)
        if src is None:
            raise BoardError(f"no package provides {name!r} (available: {', '.join(self.available()) or 'none'}); "
                             "pip install positnp[kv260] (or -dsp / -ml / -linalg)")
        with tempfile.TemporaryDirectory() as tmp:
            dtbo = Path(tmp) / f"{name}.dtbo"
            r = self._run(["dtc", "-@", "-q", "-O", "dtb", "-o", str(dtbo), str(src / f"{name}.dtsi")])
            if getattr(r, "returncode", 1) != 0:
                raise BoardError(f"dtc failed for {name}.dtsi: {(r.stderr or r.stdout or '').strip()} "
                                 "(sudo apt install device-tree-compiler)")
            dst = self.fw_dir / name
            self._cmd("rm", "-rf", str(dst))
            files = [src / f"{name}.bit.bin", dtbo, src / "shell.json"] + \
                ([src / f"{name}.json"] if (src / f"{name}.json").exists() else [])
            for f in files:
                self._cmd("install", "-D", "-m", "644", str(f), str(dst / f.name))
        try:                                                       # dfx-mgr notices new directories itself;
            if name not in self._cmd("xmutil", "listapps", check=False):   # if not, a restart makes it look
                self._cmd("systemctl", "restart", "dfx-mgr", check=False)
                time.sleep(2 if self._run is _default_run else 0)
        except BoardError:
            pass
        return self.load(name) if load else self.app(name)

    def setup_udmabuf(self, tag: str = "") -> str:
        """Build and install the u-dma-buf kernel module (once per kernel; needs network, kernel headers, sudo):
        runs the build_udmabuf.sh shipped with positnp."""
        r = self._run(["bash", str(UDMABUF_SCRIPT)] + ([tag] if tag else []))
        if r.returncode != 0:
            raise BoardError(f"build_udmabuf.sh failed: {(r.stderr or r.stdout or '').strip()[-2000:]}")
        return r.stdout or ""

    # ---- xmutil -----------------------------------------------------------------------------------

    def _cmd(self, *args: str, check: bool = True) -> str:
        cmd = (["sudo", "-n"] if self.sudo else []) + list(args)
        try:
            r = self._run(cmd)
        except FileNotFoundError as e:
            raise BoardError(f"{args[0]} not found: is this the KV260 (Ubuntu with xmutil)? ({e})") from None
        if check and r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            hint = " (sudo -n refused: run Python with sudo or allow xmutil in sudoers, see pn.board docs)" \
                if self.sudo and "password" in msg.lower() else ""
            raise BoardError(f"{' '.join(cmd)}: exit {r.returncode}: {msg}{hint}")
        return r.stdout or ""

    def active(self) -> Optional[str]:
        """The loaded application (xmutil listapps, Active_slot >= 0), None when none is."""
        for line in self._cmd("xmutil", "listapps").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] == "Accelerator":
                continue
            slot = parts[-1].rstrip(",")
            if slot.lstrip("-").isdigit() and int(slot) >= 0:
                return parts[0]
        return None

    # ---- load / open / unload ---------------------------------------------------------------------

    def load(self, name: str, activate: bool = True, force: bool = False) -> Dict[int, backend_hw.PositVecHw]:
        """Load application `name` and open its engines: {n: PositVecHw}.

        activate: register every engine as the backend of its format (pn.set_backend).
        force: reload even when `name` is already the active application (resets the PL).
        """
        app = self.app(name)
        self.close()
        if force or self.active() != name:
            if any(e.get("udmabuf") for e in app.engines):
                self._cmd("modprobe", "u-dma-buf", check=False)
            self._cmd("xmutil", "unloadapp", check=False)          # error when nothing was loaded: fine
            self._cmd("xmutil", "loadapp", name)
            self._wait_devices(app)
        self._loaded = name
        return self.open(activate) if app.kind == "vec" else {}

    def _wait_devices(self, app: App) -> None:
        need_uio = [n for e in app.engines for n in (e.get("ip"), e.get("dma")) if n]
        need_buf = [e["udmabuf"] for e in app.engines if e.get("udmabuf")]
        if app.kind == "alu":
            need_uio = [backend_hw.IP_NAME]
        t0 = time.monotonic()
        while True:
            missing = [n for n in need_uio if backend_hw.find_uio(n) is None] + \
                      [n for n in need_buf if not (udmabuf.SYS_CLASS / n).exists()]
            if not missing:
                return
            if time.monotonic() - t0 > self.timeout:
                raise BoardError(f"{app.name} loaded, but no device {', '.join(missing)} after {self.timeout:.0f} s "
                                 f"(dmesg | tail -30; u-dma-buf module: positnp-board udmabuf; CMA: grep Cma /proc/meminfo)")
            time.sleep(0.05)

    def open(self, activate: bool = True) -> Dict[int, backend_hw.PositVecHw]:
        """Open the engines of the application that is already loaded (no reload): {n: PositVecHw}."""
        self.close()
        if self._loaded is None:
            try:
                self._loaded = self.active()          # for describe(); xmutil may be unavailable
            except BoardError:
                pass
        found = backend_hw.vec_engines()
        if not found:
            raise BoardError("no posit_vec engine in the loaded application (pn.board.active(), pn.board.apps())")
        try:
            for e in found:
                hw = self._open_engine(e)
                if hw.config.n in self._engines:
                    hw.close()
                    raise BoardError(f"two engines for posit<{hw.config.n}>: {e.name}")
                self._engines[hw.config.n] = hw
        except BaseException:
            self.close()
            raise
        if activate:
            _array.set_backend(list(self._engines.values()))
        return dict(self._engines)

    def engines(self) -> Dict[int, backend_hw.PositVecHw]:
        """Engines opened by load() / open(): {n: PositVecHw}."""
        return dict(self._engines)

    def engine(self, n: int) -> backend_hw.PositVecHw:
        if n not in self._engines:
            raise BoardError(f"no open posit<{n}> engine (open: {sorted(self._engines) or 'none'}; pn.board.load(...))")
        return self._engines[n]

    def close(self) -> None:
        """Close the open engines and take them out of the backends (formats go back to PositSw)."""
        for hw in self._engines.values():
            for cfg, be in list(_array.active_backends().items()):
                if be is hw:
                    _array._backends.pop(cfg, None)
            hw.close()
        self._engines.clear()

    def unload(self) -> None:
        """close(), then xmutil unloadapp."""
        self.close()
        self._cmd("xmutil", "unloadapp", check=False)
        self._loaded = None

    # ---- description ------------------------------------------------------------------------------

    def info(self) -> List[Dict]:
        """One dict per open engine: format, node, operations, k, lanes, buffer, quire."""
        rows = []
        for n, hw in sorted(self._engines.items(), reverse=True):
            ec = hw.engine_cfg() or {}
            rows.append({"n": n, "config": str(hw.config), "engine": getattr(hw.engine, "name", None),
                         "ops": list(hw.array_ops), "quire": hw.has_quire(),
                         "k": ec.get("k"), "units": ec.get("units"), "lanes": ec.get("lanes", 1),
                         "gemv_k": getattr(hw, "gemv_max_k", 0), "gemv_cols": getattr(hw, "gemv_cols", 1),
                         "table_rate": getattr(hw, "table_rate", 0) if getattr(hw, "has_table", lambda: False)() else 0,
                         "fft_max_s": getattr(hw, "fft_max_s", 0),
                         "buffer_mb": hw.buf.size >> 20, "version": f"{hw.version >> 8}.{hw.version & 0xFF}"})
        return rows

    def describe(self) -> str:
        rows = self.info()
        head = f"application: {self._loaded or '?'}"
        if not rows:
            return head + " (no engines open: pn.board.load(name) or pn.board.open())"
        lines = [head]
        for r in rows:
            lanes = f", {r['units']} lane{'s' if r['units'] != 1 else ''} div/sqrt/fma, k={r['k']}" if r["units"] else ""
            if r.get("lanes", 1) > 1:
                lanes = f", {r['lanes']} elements/clock" + lanes
            gemv = f", matmul (K <= {r['gemv_k']}, {r.get('gemv_cols', 1)} columns/pass)" if r.get("gemv_k") else ""
            if r.get("table_rate"):
                gemv += f", functions from a table ({r['table_rate']} lookups/clock)"
            if r.get("fft_max_s"):
                gemv += f", FFT core (N = 16 .. {4 ** r['fft_max_s']})"
            lines.append(f"  {r['config']:<12} {r['engine']}: {', '.join(r['ops'])}"
                         f"{', quire' if r['quire'] else ''}{gemv}{lanes}, buffer {r['buffer_mb']} MB, v{r['version']}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Board(loaded={self._loaded!r}, engines={sorted(self._engines)})"


_board = Board()


def apps() -> List[App]:
    return _board.apps()


def active() -> Optional[str]:
    return _board.active()


def load(name: str, activate: bool = True, force: bool = False) -> Dict[int, backend_hw.PositVecHw]:
    return _board.load(name, activate, force)


def open(activate: bool = True) -> Dict[int, backend_hw.PositVecHw]:   # noqa: A001 - pn.board.open()
    return _board.open(activate)


def engines() -> Dict[int, backend_hw.PositVecHw]:
    return _board.engines()


def engine(n: int) -> backend_hw.PositVecHw:
    return _board.engine(n)


def close() -> None:
    _board.close()


def unload() -> None:
    _board.unload()


def info() -> List[Dict]:
    return _board.info()


def describe() -> str:
    return _board.describe()


def available() -> Dict[str, Path]:
    return _board.available()


def install(name: str, load: bool = False):
    return _board.install(name, load)


def setup_udmabuf(tag: str = "") -> str:
    return _board.setup_udmabuf(tag)


def use(board: Board) -> Board:
    """Replace the shared instance behind pn.board.* (tests, other firmware directory); returns the old one."""
    global _board
    old, _board = _board, board
    return old


def main(argv: Optional[List[str]] = None) -> int:
    """positnp-board: the same from a shell (installing and loading need root or sudo -n)."""
    import argparse
    p = argparse.ArgumentParser(prog="positnp-board", description="posit-numpy PL applications on the KV260")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("available", help="applications the installed packages provide")
    sub.add_parser("list", help="applications installed in /lib/firmware/xilinx")
    i = sub.add_parser("install", help="install an application from a package")
    i.add_argument("name")
    i.add_argument("--load", action="store_true", help="load it afterwards")
    ld = sub.add_parser("load", help="load an installed application and describe its engines")
    ld.add_argument("name")
    sub.add_parser("unload", help="unload the active application")
    sub.add_parser("describe", help="open the engines of the loaded application and describe them")
    u = sub.add_parser("udmabuf", help="build and install the u-dma-buf kernel module")
    u.add_argument("tag", nargs="?", default="")
    a = p.parse_args(argv)
    try:
        if a.cmd == "available":
            for n, d in available().items():
                print(f"{n:<24} {d}")
        elif a.cmd == "list":
            for app in apps():
                print(repr(app))
        elif a.cmd == "install":
            install(a.name)
            print(f"installed {a.name} in {FW_DIR / a.name}")
            if a.load:
                load(a.name)
                print(describe())
        elif a.cmd == "load":
            load(a.name)
            print(describe())
        elif a.cmd == "unload":
            unload()
        elif a.cmd == "describe":
            open()
            print(describe())
        elif a.cmd == "udmabuf":
            print(setup_udmabuf(a.tag))
    except BoardError as e:
        print(f"positnp-board: {e}", file=sys.stderr)
        return 1
    return 0


__all__ = ["App", "Board", "BoardError", "FW_DIR", "OVERLAY_GROUP", "apps", "active", "available", "install",
           "setup_udmabuf", "load", "open", "engines", "engine", "close", "unload", "info", "describe", "use",
           "overlay_dirs", "main"]


if __name__ == "__main__":          # python3 -m positnp.board ... (z repozytorium, bez pip install)
    sys.exit(main())
