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

import numpy as np

from . import ref, regs
from .dma import MAX_LENGTH as DMA_MAX_LENGTH, AxiDma, DmaError
from .ref import Flags, PositConfig
from .udmabuf import UDmaBuf

OVERLAY_DIR = Path(__file__).with_name("overlays")
IP_NAME = "posit_alu"  # block design instance = UIO node name in the .dtsi (hw/top-modules/alu/vivado/build.tcl)
VEC_NAME = "posit_vec"  # stage 2 (hw/top-modules/vec/vivado/build.tcl)
DMA_NAME = "posit_dma"
BUF_NAME = "udmabuf-posit"
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
        self.array_ops = tuple(name for name, code in {**regs.OPS, **regs.STAGE5}.items() if self.op_mask >> code & 1)

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


class PositVecHw(PositAluHw):
    """Stage 2: whole arrays through AXI DMA and AXI4-Stream; single ops as in stage 1.

        vec = PositVecHw.open()                     # /dev/uio (posit_vec, posit_dma), /dev/udmabuf-posit
        r = vec.array_op("mul", a_bits, b_bits)     # numpy arrays of raw posit bits -> raw bits
        r = vec.array_op("add", a_bits, scalar=b)   # array (+) scalar

    Per chunk: pack {b, a} into the buffer, cache sync, arm S2MM, start MM2S, wait for
    both, check the received byte count, cache sync, copy the result out.
    """

    IN_DTYPE = {8: np.uint16, 16: np.uint32, 32: np.uint64}    # s_axis word: {b, a}
    OUT_DTYPE = {8: np.uint8, 16: np.uint16, 32: np.uint32}    # m_axis word: result
    ALIGN = 4096

    def __init__(self, mmio: Mmio, dma_mmio: Mmio, buf, timeout_polls: int = 100_000):
        super().__init__(mmio, timeout_polls)
        cfg = self.read(regs.CONFIG)
        if not cfg >> regs.CONFIG_HAS_STREAM & 1 or self.version < regs.VERSION_VEC:
            raise PositAluError(f"engine without the stream path (CONFIG 0x{cfg:08x}, version 0x{self.version:04x})")
        n = self.config.n
        if n not in self.IN_DTYPE:
            raise PositAluError(f"posit<{n}>: the stream path needs n = 8, 16 or 32")
        self.dma = AxiDma(dma_mmio)
        self.buf = buf
        self.in_dtype = self.IN_DTYPE[n]
        self.out_dtype = self.OUT_DTYPE[n]
        self.in_bytes = 2 * n // 8
        self.out_bytes = n // 8
        self._sw = None
        self._fallback = 0
        from .backend_sw import PositSw
        self._sw = PositSw(self.config)
        self.dma.reset()

    @classmethod
    def open(cls, name: str = VEC_NAME, dma: str = DMA_NAME, buf: str = BUF_NAME) -> "PositVecHw":
        return cls(UioMmio(name), UioMmio(dma), UDmaBuf(buf))

    def chunk_elements(self) -> int:
        """Elements per DMA round: input and output regions in one buffer, input within one transfer."""
        usable = self.buf.size - self.ALIGN
        return max(1, min(usable // (self.in_bytes + self.out_bytes), DMA_MAX_LENGTH // self.in_bytes))

    def _bits(self, x) -> np.ndarray:
        """Raw bits as the n-bit unsigned dtype; no copy when they already are (PositArray.bits)."""
        x = np.asarray(x)
        if x.size == 0:
            return np.empty(0, self.out_dtype)
        if not np.issubdtype(x.dtype, np.integer):
            raise TypeError(f"raw posit bits expected (an integer array), got {x.dtype}")
        # n is the dtype width (8/16/32), so the C cast is exactly "& mask"
        return x.astype(self.out_dtype, copy=False).ravel()

    UNARY = ("sqrt", "f2p", "p2f")

    def array_op(self, name: str, a, b=None, scalar: Optional[int] = None, c=None) -> np.ndarray:
        """Element-wise `name` on raw bits, shape of a. Returns raw bits.

          add/sub/mul/div   a (+) b, or a (+) scalar
          sqrt, f2p, p2f    unary (f2p: a = float32 bits; p2f: result = float32 bits)
          fma               a * b + c: c an int -> OP_FMA (c in OPERAND_C; b an array or `scalar`);
                            c an array and b `scalar` -> OP_AXPY (y = alpha * x + y in one stream pass)

        An operation the bitstream lacks, or fma with three arrays (needs a third stream, stage 6),
        is computed by PositSw instead; stats()["fallback"] counts those elements.
        Time per phase of the last call, in seconds: self.last_timing
        (pack, sync_device, dma, sync_cpu, copy_out, total).
        """
        route = self._route(name, b, scalar, c)
        if route is None:
            self._fallback += int(np.size(a))
            return self._sw.array_op(name, a, b, scalar=scalar, c=c).astype(self.out_dtype)
        code, b, scalar, c_reg = route
        return self._stream(code, a, b, scalar, c_reg)

    def _route(self, name, b, scalar, c):
        """(op code, stream b half, scalar for OPERAND_B, OPERAND_C) or None for the software path."""
        codes = {**regs.OPS, **regs.STAGE5}
        if name not in codes:
            raise ValueError(f"unknown operation {name!r}")
        if name not in self.array_ops:
            return None
        if name in self.UNARY:
            return codes[name], None, 0, None
        if name == "fma":
            if c is None:
                raise ValueError("fma needs c")
            if np.ndim(c) == 0:
                return regs.OP_FMA, b, scalar, int(c)
            if scalar is not None:
                return regs.OP_AXPY, c, scalar, None    # alpha in OPERAND_B, b half of the stream = the addend
            return None                                 # three arrays: not with one {b, a} stream
        if name == "axpy":
            raise ValueError("use name='fma' with scalar=alpha and c=y")
        return codes[name], b, scalar, None

    def _stream(self, code: int, a, b=None, scalar: Optional[int] = None, c_reg: Optional[int] = None) -> np.ndarray:
        t_start = time.perf_counter()
        timing = dict.fromkeys(("pack", "sync_device", "dma", "sync_cpu", "copy_out"), 0.0)
        shape = np.shape(a)
        av = self._bits(a)
        if b is not None:
            bv = self._bits(b)
            if bv.size != av.size:
                raise ValueError(f"a has {av.size} elements, b {bv.size}")
        elif scalar is not None:
            bv = None
        else:
            raise ValueError("array_op needs b or scalar")
        out = np.empty(av.size, self.out_dtype)
        if av.size == 0:
            return out.reshape(shape)

        self.write(regs.OP, code)
        self.write(regs.STREAM_CFG, 1 << regs.STREAM_CFG_SCALAR_B if scalar is not None and bv is None else 0)
        if scalar is not None:
            self.write(regs.OPERAND_B, int(scalar) & self.config.mask)
        if c_reg is not None:
            self.write(regs.OPERAND_C, int(c_reg) & self.config.mask)

        step = self.chunk_elements()
        for i in range(0, av.size, step):
            k = min(step, av.size - i)
            in_len = k * self.in_bytes
            out_off = -(-in_len // self.ALIGN) * self.ALIGN
            out_len = k * self.out_bytes

            # {b, a} in a little-endian 2n-bit word = the pair (a, b) of n-bit words: two strided copies
            t = time.perf_counter()
            pairs = self.buf.view(self.out_dtype, 0, 2 * k).reshape(k, 2)
            pairs[:, 0] = av[i:i + k]
            if bv is not None:
                pairs[:, 1] = bv[i:i + k]          # scalar mode: the b half is ignored by the engine
            timing["pack"] += time.perf_counter() - t

            t = time.perf_counter()
            self.buf.sync_for_device(0, in_len, 1)             # clean: the DMA reads the input
            self.buf.sync_for_device(out_off, out_len, 2)      # invalidate: no dirty line over the result
            timing["sync_device"] += time.perf_counter() - t

            t = time.perf_counter()
            self.dma.start_s2mm(self.buf.phys_addr + out_off, out_len)
            self.dma.start_mm2s(self.buf.phys_addr, in_len)
            try:
                self.dma.wait_mm2s()
                got = self.dma.wait_s2mm()
            except DmaError as e:
                raise DmaError(f"{e}; engine STATUS 0x{self.read(regs.STATUS):08x}, "
                               f"CNT_ELEMENTS {self.read(regs.CNT_ELEMENTS)}") from None
            if got != out_len:
                raise DmaError(f"S2MM wrote {got} B, expected {out_len} B ({k} elements)")
            timing["dma"] += time.perf_counter() - t

            t = time.perf_counter()
            self.buf.sync_for_cpu(out_off, out_len, 2)
            timing["sync_cpu"] += time.perf_counter() - t

            t = time.perf_counter()
            out[i:i + k] = self.buf.view(self.out_dtype, out_off, k)
            timing["copy_out"] += time.perf_counter() - t
        timing["total"] = time.perf_counter() - t_start
        self.last_timing = timing
        return out.reshape(shape)

    def stats(self) -> Dict[str, int]:
        s = super().stats()
        s["elements"] = self.read(regs.CNT_ELEMENTS)
        s["fallback"] = self._fallback
        return s

    def clear_stats(self) -> None:
        super().clear_stats()
        self._fallback = 0

    def has_quire(self) -> bool:
        return bool(self.read(regs.CONFIG) >> regs.CONFIG_HAS_QUIRE & 1)

    def reduce(self, mode: str, a, b=None):
        """Quire reduction on the FPGA: (bits, Flags, q, nar), q the exact quire as an int.

        sum: a, dot: a*b, sumsq: a*a. Only MM2S runs (no results on the stream); the engine
        accumulates every chunk into the same quire, then one CTRL.quire_round.
        """
        if mode not in regs.REDUCE:
            raise ValueError(f"quire mode {mode!r}, expected one of {tuple(regs.REDUCE)}")
        if not self.has_quire():
            raise PositAluError("this bitstream has no quire (stage 4, PositVecAxi 3.0)")
        av = self._bits(a)
        bv = self._bits(b) if mode == "dot" else None
        if bv is not None and bv.size != av.size:
            raise ValueError(f"a has {av.size} elements, b {bv.size}")
        self.write(regs.OP, regs.REDUCE[mode])
        self.write(regs.CTRL, 1 << regs.CTRL_QUIRE_CLEAR)
        before = self.read(regs.CNT_ELEMENTS)
        step = max(1, min(self.buf.size // self.in_bytes, DMA_MAX_LENGTH // self.in_bytes))
        for i in range(0, av.size, step):
            k = min(step, av.size - i)
            pairs = self.buf.view(self.out_dtype, 0, 2 * k).reshape(k, 2)
            pairs[:, 0] = av[i:i + k]
            if bv is not None:
                pairs[:, 1] = bv[i:i + k]
            self.buf.sync_for_device(0, k * self.in_bytes, 1)
            self.dma.start_mm2s(self.buf.phys_addr, k * self.in_bytes)
            self.dma.wait_mm2s()
        # MM2S done = data left memory; wait until the engine took every element
        for _ in range(self.timeout_polls):
            if (self.read(regs.CNT_ELEMENTS) - before) & 0xFFFFFFFF >= av.size:
                break
        else:
            raise PositAluError(f"quire took {(self.read(regs.CNT_ELEMENTS) - before) & 0xFFFFFFFF} "
                                f"of {av.size} elements")
        self.write(regs.CTRL, 1 << regs.CTRL_QUIRE_ROUND)
        st = self._wait_done()
        bits = self.read(regs.RESULT) & self.config.mask
        flags = Flags.from_bits(st >> regs.STATUS_FLAGS_SHIFT & 7)
        nar = bool(st >> regs.STATUS_QUIRE_NAR & 1)
        width = self.read(regs.QUIRE_CFG) & 0xFFFF
        raw = 0
        for k in range(width // 32 - 1, -1, -1):
            raw = (raw << 32) | self.read(regs.QUIRE + 4 * k)
        q = 0 if nar else ref.quire_from_raw(self.config, raw)
        return bits, flags, q, nar

    def time_array(self, name: str = "add", size: int = 1 << 20) -> float:
        """Elements per second for one array_op on `size` random elements (DMA, packing and copies included)."""
        rnd = np.random.default_rng(0)
        a = rnd.integers(0, 1 << self.config.n, size, dtype=np.int64).astype(self.out_dtype)
        b = rnd.integers(0, 1 << self.config.n, size, dtype=np.int64).astype(self.out_dtype)
        args = {"fma": dict(b=b, c=int(a[0])),                       # OP_FMA, c in OPERAND_C
                "axpy": dict(scalar=int(a[0]), c=b)}.get(name, dict(b=b))   # axpy: OP_AXPY
        t0 = time.perf_counter()
        self.array_op("fma" if name == "axpy" else name, a, **args)
        return size / (time.perf_counter() - t0)


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
