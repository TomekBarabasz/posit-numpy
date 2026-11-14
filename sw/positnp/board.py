"""pn.board: PL applications on the KV260 from Python (Ubuntu + xmutil, no PYNQ).

    import positnp as pn
    pn.board.apps()                    # installed: [App('posit32_16_8_vec', engines 32/16/8), App('posit32_alu'), ...]
    pn.board.active()                  # 'posit32_16_8_vec' (xmutil listapps), None when nothing is loaded
    hw = pn.board.load("posit32_16_8_vec")   # {32: PositVecHw, 16: ..., 8: ...}, all set as backends
    x = pn.array([1.0, 2.0], pn.P16)   # posit16 arrays now run on the posit16 engine, posit32 on posit32
    print(pn.board.describe())         # engines, operations, lanes, buffers
    pn.board.unload()                  # close the engines, xmutil unloadapp, everything back to PositSw

Applications are the directories in /lib/firmware/xilinx/ installed by sw/scripts/install_overlay.sh
(.bit.bin, .dtbo, shell.json and <name>.json from build.tcl with the list of engines). Loading needs
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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import importlib

from . import backend_hw, udmabuf

_array = importlib.import_module(".array", __package__)   # module (positnp.array is the function)

FW_DIR = Path("/lib/firmware/xilinx")


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


class Board:
    """The KV260 seen from Python. pn.board.* are the methods of one shared instance.

    run, fw_dir, sudo and open_engine can be replaced (tests run it all on a PC with fakes).
    """

    def __init__(self, fw_dir: Path = FW_DIR, run: Optional[Callable] = None, sudo: Optional[bool] = None,
                 open_engine: Optional[Callable] = None, timeout: float = 10.0):
        self.fw_dir = Path(fw_dir)
        self._run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=120))
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
        raise BoardError(f"application {name!r} is not installed in {self.fw_dir} (installed: {have}); "
                         f"install it with sw/scripts/install_overlay.sh {name}")

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
                                 f"(dmesg | tail -30; u-dma-buf module: sw/scripts/build_udmabuf.sh; CMA: grep Cma /proc/meminfo)")
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
                         "k": ec.get("k"), "units": ec.get("units"),
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
            lines.append(f"  {r['config']:<12} {r['engine']}: {', '.join(r['ops'])}"
                         f"{', quire' if r['quire'] else ''}{lanes}, buffer {r['buffer_mb']} MB, v{r['version']}")
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


def use(board: Board) -> Board:
    """Replace the shared instance behind pn.board.* (tests, other firmware directory); returns the old one."""
    global _board
    old, _board = _board, board
    return old


__all__ = ["App", "Board", "BoardError", "FW_DIR", "apps", "active", "load", "open", "engines", "engine",
           "close", "unload", "info", "describe", "use"]
