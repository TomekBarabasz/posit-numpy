"""Stage 1 hardware backend: the posit ALU behind AXI4-Lite, driven through MMIO. No PYNQ.

On the KV260 (Ubuntu + xmutil), after sw/scripts/install_overlay.sh posit32_alu:

    from positnp.backend_hw import PositAluHw
    alu = PositAluHw.open()                  # /dev/uioN named "posit_alu" (from the overlay's .dtbo)
    bits, flags = alu.op("div", 0x40000000, 0x4C000000)
    alu.stats()                              # {'nar': 0, 'sat': 0, 'inexact': 1, 'cycles': ...}

The register sequence is the one PositAluAxiLiteTestplan checks in simulation:
OP, OPERAND_A, OPERAND_B, CTRL.start, poll STATUS.done, read RESULT.

`PositAluHw` needs only an object with read(offset) and write(offset, value):
`UioMmio` (the normal way), `DevMemMmio` (explicit opt-in, see its warning) and
`FakeAluMmio` (the register model on ref.py, for a PC without the board).
"""

from __future__ import annotations

import json
import mmap
import os
import time
from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple

from . import ref, regs
from .ref import Flags, PositConfig

OVERLAY_DIR = Path(__file__).with_name("overlays")
IP_NAME = "posit_alu"  # block design instance = UIO node name in the .dtsi (hw/top-modules/alu/vivado/build.tcl)
UIO_CLASS = Path("/sys/class/uio")


class Mmio(Protocol):
    def read(self, offset: int) -> int: ...
    def write(self, offset: int, value: int) -> None: ...


class PositAluError(RuntimeError):
    pass


# ---- register access on the board ----------------------------------------------------------

class _MappedWords:
    """32-bit accesses through a memoryview cast to 'I': one aligned load/store per register.

    Byte-wise copies (struct on the raw mmap) could turn into four 8-bit bus writes, and the
    AXI4-Lite slave ignores WSTRB, so every access here is a whole word.
    """

    def __init__(self, fd: int, size: int, offset: int):
        self.size = size
        self._mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=offset)
        self._w = memoryview(self._mm).cast("I")

    def read(self, offset: int) -> int:
        return self._w[offset >> 2]

    def write(self, offset: int, value: int) -> None:
        self._w[offset >> 2] = value & 0xFFFFFFFF

    def close(self) -> None:
        self._w.release()
        self._mm.close()


def find_uio(name: str = IP_NAME) -> Optional[str]:
    """/dev/uioN whose device tree node is `name`, or None (overlay not loaded)."""
    if not UIO_CLASS.exists():
        return None
    for d in sorted(UIO_CLASS.iterdir()):
        try:
            if (d / "name").read_text().strip() == name:
                return f"/dev/{d.name}"
        except OSError:
            continue
    return None


class UioMmio(_MappedWords):
    """Registers through /dev/uioN (generic-uio node from the overlay). Needs read/write access to the node."""

    def __init__(self, name: str = IP_NAME):
        dev = find_uio(name)
        if dev is None:
            raise PositAluError(f"no UIO device named {name!r}: is the overlay loaded? "
                                f"(sudo xmutil listapps; sw/scripts/install_overlay.sh)")
        size = int((UIO_CLASS / Path(dev).name / "maps" / "map0" / "size").read_text(), 0)
        fd = os.open(dev, os.O_RDWR | os.O_SYNC)
        try:
            super().__init__(fd, size, 0)   # map0 = offset 0
        finally:
            os.close(fd)
        self.device = dev


class DevMemMmio(_MappedWords):
    """Registers through /dev/mem at a physical address (root only).

    Only for when UIO is not available. Reading an address in the PL window while the
    PL holds no design there can stall the AXI bus and hang the whole system, so
    nothing in positnp falls back to this on its own.
    """

    def __init__(self, base: int, size: int = 0x1000):
        if base % mmap.PAGESIZE:
            raise ValueError(f"base 0x{base:x} is not page aligned")
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            super().__init__(fd, size, base)
        finally:
            os.close(fd)


def overlay_info(overlay: str) -> Dict[str, object]:
    """overlays/<overlay>/<overlay>.json written by build.tcl (base address, size, clock, WNS...)."""
    return json.loads((OVERLAY_DIR / overlay / f"{overlay}.json").read_text())


class PositAluHw:
    def __init__(self, mmio: Mmio, timeout_polls: int = 100_000):
        self.mmio = mmio
        self.timeout_polls = timeout_polls
        idv = self.read(regs.ID_VERSION)
        if idv >> 16 != regs.ID:
            raise PositAluError(f"ID_VERSION = 0x{idv:08x}: not the posit ALU (expected ID 0x{regs.ID:04x})")
        self.version = idv & 0xFFFF
        cfg = self.read(regs.CONFIG)
        self.config = PositConfig(cfg & 0xFF, (cfg >> 8) & 0xF)
        self.op_mask = cfg >> 16
        self.ops = tuple(name for name, code in regs.OPS.items() if self.op_mask >> code & 1)

    # ---- construction on the board ----------------------------------------------------

    @classmethod
    def open(cls, name: str = IP_NAME, devmem_overlay: Optional[str] = None) -> "PositAluHw":
        """Bind to the ALU of the loaded overlay.

        Default: the UIO device `name` (needs sudo, or a udev rule for /dev/uio*).
        devmem_overlay="posit32_alu": /dev/mem at the address from overlays/<name>/<name>.json
        instead; only when you know that overlay is loaded (see DevMemMmio).
        """
        if devmem_overlay is not None:
            info = overlay_info(devmem_overlay)
            return cls(DevMemMmio(int(info["base"]), int(info["size"])))
        return cls(UioMmio(name))

    # ---- registers ----------------------------------------------------------------------

    def read(self, offset: int) -> int:
        return int(self.mmio.read(offset)) & 0xFFFFFFFF

    def write(self, offset: int, value: int) -> None:
        self.mmio.write(offset, int(value) & 0xFFFFFFFF)

    def status(self) -> Dict[str, object]:
        s = self.read(regs.STATUS)
        return {
            "busy": bool(s >> regs.STATUS_BUSY & 1),
            "done": bool(s >> regs.STATUS_DONE & 1),
            "error": bool(s >> regs.STATUS_ERROR & 1),
            "flags": Flags.from_bits(s >> regs.STATUS_FLAGS_SHIFT & 7),
        }

    # ---- operations ---------------------------------------------------------------------

    def op(self, name: str, a: int, b: int) -> Tuple[int, Flags]:
        """One operation on raw posit bits; returns (result bits, flags)."""
        if name not in regs.OPS:
            raise ValueError(f"unknown operation {name!r}, expected one of {tuple(regs.OPS)}")
        if name not in self.ops:
            raise PositAluError(f"{name} is not built into this bitstream (ops: {self.ops})")
        mask = self.config.mask
        self.write(regs.OP, regs.OPS[name])
        self.write(regs.OPERAND_A, a & mask)
        self.write(regs.OPERAND_B, b & mask)
        self.write(regs.CTRL, 1 << regs.CTRL_START)
        s = self._wait_done()
        if s >> regs.STATUS_ERROR & 1:
            raise PositAluError(f"STATUS.error after {name}")
        return self.read(regs.RESULT) & mask, Flags.from_bits(s >> regs.STATUS_FLAGS_SHIFT & 7)

    def _wait_done(self) -> int:
        for _ in range(self.timeout_polls):
            s = self.read(regs.STATUS)
            if s >> regs.STATUS_DONE & 1:
                return s
        raise PositAluError(f"STATUS.done did not come after {self.timeout_polls} polls (STATUS = 0x{s:08x})")

    def add(self, a: int, b: int) -> int:
        return self.op("add", a, b)[0]

    def sub(self, a: int, b: int) -> int:
        return self.op("sub", a, b)[0]

    def mul(self, a: int, b: int) -> int:
        return self.op("mul", a, b)[0]

    def div(self, a: int, b: int) -> int:
        return self.op("div", a, b)[0]

    # ---- control and counters -----------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        return {
            "nar": self.read(regs.CNT_NAR),
            "sat": self.read(regs.CNT_SAT),
            "inexact": self.read(regs.CNT_INEXACT),
            "cycles": self.read(regs.CYCLES),
        }

    def clear_stats(self) -> None:
        self.write(regs.CTRL, 1 << regs.CTRL_CLEAR_COUNTERS)

    def soft_reset(self) -> None:
        self.write(regs.CTRL, 1 << regs.CTRL_SOFT_RESET)

    def time_op(self, name: str = "add", a: int = 0, b: int = 0, repeat: int = 10_000) -> float:
        """Mean wall time of one op() call in seconds (MMIO round trips included)."""
        t0 = time.perf_counter()
        for _ in range(repeat):
            self.op(name, a, b)
        return (time.perf_counter() - t0) / repeat

    def __repr__(self) -> str:
        return f"PositAluHw({self.config}, ops={self.ops}, version={self.version >> 8}.{self.version & 0xFF})"


class FakeAluMmio:
    """Register-level model of PositAluAxiLite on ref.py, for running PositAluHw without a board.

    Models the visible behaviour only (result on the first STATUS read after start); it checks
    the driver, not the hardware.
    """

    def __init__(self, config: PositConfig = PositConfig(32), ops=("add", "sub", "mul", "div"),
                 busy_reads: int = 1):
        self.config = config
        self.ops = tuple(ops)
        self.busy_reads = busy_reads
        self.regs = {regs.OP: 0, regs.OPERAND_A: 0, regs.OPERAND_B: 0, regs.RESULT: 0}
        self.counters = {"nar": 0, "sat": 0, "inexact": 0, "cycles": 0}
        self.busy = self.done = self.error = False
        self.flags = 0
        self._pending = 0
        self.log = []

    def _mask(self) -> int:
        return sum(1 << regs.OPS[o] for o in self.ops)

    def read(self, offset: int) -> int:
        self.log.append(("r", offset))
        if offset == regs.ID_VERSION:
            return regs.ID << 16 | regs.VERSION
        if offset == regs.CONFIG:
            return self._mask() << 16 | self.config.es << 8 | self.config.n
        if offset == regs.STATUS:
            if self.busy:
                self._pending -= 1
                self.counters["cycles"] += 1
                if self._pending <= 0:
                    self._finish()
            return (self.flags << regs.STATUS_FLAGS_SHIFT | int(self.error) << regs.STATUS_ERROR |
                    int(self.done) << regs.STATUS_DONE | int(self.busy) << regs.STATUS_BUSY)
        counters = {regs.CNT_NAR: "nar", regs.CNT_SAT: "sat", regs.CNT_INEXACT: "inexact", regs.CYCLES: "cycles"}
        if offset in counters:
            return self.counters[counters[offset]]
        return self.regs.get(offset, 0)

    def write(self, offset: int, value: int) -> None:
        self.log.append(("w", offset, value))
        if offset == regs.CTRL:
            if value >> regs.CTRL_SOFT_RESET & 1:
                self.busy = self.done = self.error = False
            if value >> regs.CTRL_CLEAR_COUNTERS & 1:
                self.counters = dict.fromkeys(self.counters, 0)
            if value >> regs.CTRL_START & 1 and not self.busy:
                legal = self._mask() >> self.regs[regs.OP] & 1
                self.done, self.error = not legal, not legal
                if legal:
                    self.busy = True
                    self._pending = self.busy_reads
                    self._cmd = (self.regs[regs.OP], self.regs[regs.OPERAND_A], self.regs[regs.OPERAND_B])
        elif offset in (regs.OP, regs.OPERAND_A, regs.OPERAND_B):
            mask = 0xF if offset == regs.OP else self.config.mask
            self.regs[offset] = value & mask

    def _finish(self) -> None:
        code, a, b = self._cmd
        name = next(k for k, v in regs.OPS.items() if v == code)
        r, f = ref.op(self.config, name, a, b)
        self.regs[regs.RESULT] = r
        self.flags = f.bits
        for k in ("nar", "sat", "inexact"):
            self.counters[k] += getattr(f, k)
        self.busy, self.done = False, True
