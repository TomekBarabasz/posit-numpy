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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

import numpy as np

from . import ref, regs
from .dma import MAX_LENGTH as DMA_MAX_LENGTH, AxiDma, DmaError
from .ref import Flags, PositConfig
from .udmabuf import UDmaBuf

OVERLAY_DIR = Path(__file__).with_name("overlays")
IP_NAME = "posit_alu"  # block design instance = UIO node name in the .dtsi (hw/top-modules/alu/vivado/build.tcl)
VEC_NAME = "posit_vec"  # stage 2-5 overlays with one engine; build.tcl -engines: posit_vec_p<n>
DMA_NAME = "posit_dma"
BUF_NAME = "udmabuf-posit"
_VEC_RE = re.compile(r"^posit_vec(?:_p(\d+))?$")
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


@dataclass(frozen=True)
class VecEngine:
    """One PositVecAxi engine of the loaded overlay: UIO names of its registers and DMA, its u-dma-buf.

    n is the posit width from the node name (posit_vec_p<n>), None for overlays built before
    build.tcl -engines (one engine named posit_vec); PositVecHw reads the format from CONFIG anyway.
    """
    name: str
    dma: str
    buf: str
    n: Optional[int] = None


def vec_engines() -> List[VecEngine]:
    """Engines of the loaded overlay (generic-uio nodes posit_vec_p<n> / posit_vec), widest first."""
    found = []
    if UIO_CLASS.exists():
        for d in UIO_CLASS.iterdir():
            try:
                m = _VEC_RE.match((d / "name").read_text().strip())
            except OSError:
                continue
            if m:
                n = int(m.group(1)) if m.group(1) else None
                sfx = f"_p{n}" if n else ""
                found.append(VecEngine(m.group(0), DMA_NAME + sfx, BUF_NAME + (f"-p{n}" if n else ""), n))
    return sorted(found, key=lambda e: (-(e.n or 0), e.name))


def select_engine(engines: List[VecEngine], n: Optional[int] = None) -> VecEngine:
    """The engine for posit<n> (or the only one when n is None); PositAluError with what is there."""
    have = ", ".join(e.name for e in engines) or "none"
    if n is None:
        if len(engines) == 1:
            return engines[0]
        raise PositAluError(f"PositVecHw.open(n): engines loaded: {have}" if engines else
                            "no posit_vec engine: is the overlay loaded? (sudo xmutil listapps; "
                            "sw/scripts/install_overlay.sh)")
    for e in engines:
        if e.n == n:
            return e
    legacy = [e for e in engines if e.n is None]
    if legacy:
        return legacy[0]                 # format checked against CONFIG after opening
    raise PositAluError(f"no posit<{n}> engine in the loaded overlay (engines: {have})")


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


class _Closed:
    """Stands in for the mappings of a closed PositVecHw: any use raises."""

    def __getattr__(self, name):
        raise PositAluError("PositVecHw is closed")


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
        if not cfg >> regs.CONFIG_HAS_STREAM & 1 or self.version < regs.VERSION_STREAM_MIN:
            raise PositAluError(f"engine without the stream path (CONFIG 0x{cfg:08x}, version 0x{self.version:04x})")
        n = self.config.n
        if n not in self.IN_DTYPE:
            raise PositAluError(f"posit<{n}>: the stream path needs n = 8, 16 or 32")
        # stage 6: the engine reads and writes memory itself (no AXI DMA); MEM_LO/HI = this buffer
        self.master = bool(cfg >> regs.CONFIG_HAS_MASTER & 1) and self.version >= 0x0500
        if not self.master and dma_mmio is None:
            raise PositAluError("engine without its own AXI4 master needs the AXI DMA (UIO posit_dma*)")
        self.dma = AxiDma(dma_mmio) if dma_mmio is not None else None
        self.buf = buf
        self.engine: Optional[VecEngine] = None
        self.fma3 = self.master and bool(self.op_mask >> regs.OP_FMA3 & 1)
        self.in_dtype = self.IN_DTYPE[n]
        self.out_dtype = self.OUT_DTYPE[n]
        self.in_bytes = 2 * n // 8
        self.out_bytes = n // 8
        self._sw = None
        self._fallback = 0
        from .backend_sw import PositSw
        self._sw = PositSw(self.config)
        if self.dma is not None:
            self.dma.reset()
        if self.master:
            hi = min(self.buf.phys_addr + self.buf.size, 0xFFFFFFF0)
            self.write(regs.MEM_LO, self.buf.phys_addr)
            self.write(regs.MEM_HI, hi)

    @classmethod
    def open(cls, n: Optional[int] = None) -> "PositVecHw":
        """The posit<n> engine of the loaded overlay; n may be omitted when there is one engine.

            PositVecHw.open()      # posit32_vec (one engine)
            PositVecHw.open(16)    # posit32_16_8_vec: the posit16 engine
        """
        e = select_engine(vec_engines(), n)
        hw = cls.open_engine(e)
        if n is not None and hw.config.n != n:
            hw.close()
            raise PositAluError(f"{e.name} computes {hw.config}, not posit<{n}>")
        return hw

    @classmethod
    def open_engine(cls, e: VecEngine) -> "PositVecHw":
        parts = []
        try:
            parts.append(UioMmio(e.name))
            dma = UioMmio(e.dma) if e.dma and find_uio(e.dma) is not None else None   # none: own AXI4 master
            parts.append(dma)
            parts.append(UDmaBuf(e.buf))
            hw = cls(*parts)
        except BaseException:
            for p in parts:
                if p is not None:
                    p.close()
            raise
        hw.engine = e
        return hw

    @classmethod
    def open_all(cls) -> Dict[int, "PositVecHw"]:
        """Every engine of the loaded overlay, by posit width: {32: PositVecHw, 16: ..., 8: ...}."""
        out: Dict[int, PositVecHw] = {}
        try:
            for e in vec_engines():
                hw = cls.open_engine(e)
                out[hw.config.n] = hw
        except BaseException:
            for hw in out.values():
                hw.close()
            raise
        return out

    def engine_cfg(self) -> Optional[Dict[str, int]]:
        """Build parameters of the engine (version >= 4.1): k = divider / sqrt bits per cycle,
        units = div / sqrt / FMA lanes (VecKv260 n:es:k:units). None for older bitstreams."""
        if self.version < 0x0401:
            return None
        v = self.read(regs.ENGINE_CFG)
        return {"k": v & 0xFF, "units": v >> 8 & 0xFF}

    def close(self) -> None:
        """Unmap the registers, the DMA and the buffer (before xmutil unloadapp). Idempotent."""
        if isinstance(self.mmio, _Closed):
            return
        for part in (self.buf, self.dma.mmio if self.dma is not None else None, self.mmio):
            close = getattr(part, "close", None)
            if close is not None:
                try:
                    close()
                except (ValueError, BufferError):
                    pass             # already closed
        self.buf = self.dma = self.mmio = _Closed()

    def __enter__(self) -> "PositVecHw":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

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
                            c an array and b `scalar` -> OP_AXPY (y = alpha * x + y in one stream pass);
                            three arrays -> OP_FMA3 (stage 6 engines: a third read stream)

        An operation the bitstream lacks (or fma with three arrays on an engine without its own AXI4
        master) is computed by PositSw instead; stats()["fallback"] counts those elements.
        Stage 6 engines (self.master) read a, b, c from the buffer and write the result there
        themselves: no {b, a} packing, no AXI DMA.
        Time per phase of the last call, in seconds: self.last_timing
        (pack, sync_device, dma, sync_cpu, copy_out, total).
        """
        route = self._route(name, b, scalar, c)
        if route is None:
            self._fallback += int(np.size(a))
            return self._sw.array_op(name, a, b, scalar=scalar, c=c).astype(self.out_dtype)
        code, b, scalar, c_reg, c_arr = route
        if self.master:
            return self._mem(code, a, b, scalar, c_reg, c_arr)
        return self._stream(code, a, b, scalar, c_reg)

    def _route(self, name, b, scalar, c):
        """(op code, b array, scalar for OPERAND_B, OPERAND_C, c array) or None for the software path."""
        codes = {**regs.OPS, **regs.STAGE5}
        if name not in codes:
            raise ValueError(f"unknown operation {name!r}")
        if name not in self.array_ops:
            return None
        if name in self.UNARY:
            return codes[name], None, 0, None, None
        if name == "fma":
            if c is None:
                raise ValueError("fma needs c")
            if np.ndim(c) == 0:
                return regs.OP_FMA, b, scalar, int(c), None
            if scalar is not None:
                return regs.OP_AXPY, c, scalar, None, None   # alpha in OPERAND_B, b stream = the addend
            if self.fma3:
                return regs.OP_FMA3, b, None, None, c        # three arrays (stage 6)
            return None                                      # three arrays: not with one {b, a} stream
        if name == "axpy":
            raise ValueError("use name='fma' with scalar=alpha and c=y")
        return codes[name], b, scalar, None, None

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

    # ---- stage 6: commands with addresses (own AXI4 master) ------------------------------------------

    # Podwojny bufor (stage 6): the buffer is split in two halves; while the engine works on one half
    # the CPU copies the next chunk into the other and takes the previous result out. Chunks of
    # MEM_CHUNK_BYTES per array keep many commands in flight even for medium arrays.
    MEM_CHUNK_BYTES = 1 << 20
    double_buffer = True

    def _layout(self, arrays: int):
        """(elements per chunk, [offsets of the `arrays` regions in half 0], offset of half 1)."""
        halves = 2 if self.double_buffer else 1
        per = (self.buf.size // halves // arrays) // self.ALIGN * self.ALIGN
        if self.double_buffer:
            per = min(per, self.MEM_CHUNK_BYTES)
        step = max(1, min(per // self.out_bytes, 0xFFFFFFFF))
        region = -(-step * self.out_bytes // self.ALIGN) * self.ALIGN
        return step, [r * region for r in range(arrays)], region * arrays

    def _mem_start(self, length: int, src: List[int], dst: Optional[int], read_b: bool, read_c: bool) -> None:
        """Start one MEM command (addresses relative to the buffer); _mem_wait() collects it."""
        self.write(regs.MEM_LEN, length)
        for reg, off in zip((regs.MEM_SRC_A, regs.MEM_SRC_B, regs.MEM_SRC_C), src):
            self.write(reg, self.buf.phys_addr + off)
        if dst is not None:
            self.write(regs.MEM_DST, self.buf.phys_addr + dst)
        self.write(regs.MEM_CFG, read_b << regs.MEM_CFG_READ_B | read_c << regs.MEM_CFG_READ_C |
                   (dst is not None) << regs.MEM_CFG_WRITE)
        self.write(regs.CTRL, 1 << regs.CTRL_MEM_START)
        self._mem_len = length

    def _mem_wait(self) -> int:
        """Wait for MEM_STATUS.done of the started command; raises on its errors; returns its cycles."""
        t0 = time.perf_counter()
        while True:
            st = self.read(regs.MEM_STATUS)
            if st >> regs.MEM_ST_DONE & 1:
                break
            if time.perf_counter() - t0 > 10.0:
                raise PositAluError(f"MEM command did not finish (MEM_STATUS 0x{st:x}, {self._mem_len} elements)")
        errs = [n for n, b in (("address outside the buffer", regs.MEM_ST_ERR_RANGE),
                               ("address not aligned to 16 B", regs.MEM_ST_ERR_ALIGN),
                               ("AXI error response", regs.MEM_ST_ERR_RESP)) if st >> b & 1]
        if errs:
            raise PositAluError(f"MEM command rejected: {', '.join(errs)} (MEM_STATUS 0x{st:x})")
        return self.read(regs.MEM_CYCLES)

    def _mem_cmd(self, length: int, src: List[int], dst: Optional[int], read_b: bool, read_c: bool) -> int:
        """One MEM command, start to done."""
        self._mem_start(length, src, dst, read_b, read_c)
        return self._mem_wait()

    def _mem_pipeline(self, ins: List[np.ndarray], out: Optional[np.ndarray], read_b: bool, read_c: bool,
                      timing: Dict[str, float]) -> int:
        """Chunks through the two halves of the buffer: fill k+1 while the engine runs k, then drain k.

        Engine and CPU never touch the same half at the same time: a half is refilled only after its
        command finished and its result was copied out. Returns the engine cycles of all commands.
        """
        size = ins[0].size
        step, offs, half = self._layout(len(ins))
        chunks = [(i, min(step, size - i)) for i in range(0, size, step)]
        base = lambda j: [o + (j % 2) * half if self.double_buffer else o for o in offs]   # noqa: E731
        nbytes = lambda k: k * self.out_bytes                                               # noqa: E731

        def fill(j):
            i, k = chunks[j]
            t = time.perf_counter()
            for x, off in zip(ins, base(j)):
                self.buf.view(self.out_dtype, off, k)[:] = x[i:i + k]      # contiguous copies, no packing
            timing["pack"] += time.perf_counter() - t
            t = time.perf_counter()
            r = base(j)                           # regions of one half are adjacent: one sync for all
            self.buf.sync_for_device(r[0], r[-1] - r[0] + nbytes(k), 1)
            timing["sync_device"] += time.perf_counter() - t

        def start(j):
            r = base(j)
            self._mem_start(chunks[j][1], r + [0] * (3 - len(r)), r[0] if out is not None else None, read_b, read_c)

        def drain(j):
            if out is None:
                return
            i, k = chunks[j]
            t = time.perf_counter()
            self.buf.sync_for_cpu(base(j)[0], nbytes(k), 2)
            timing["sync_cpu"] += time.perf_counter() - t
            t = time.perf_counter()
            out[i:i + k] = self.buf.view(self.out_dtype, base(j)[0], k)    # result over a (in place)
            timing["copy_out"] += time.perf_counter() - t

        cycles = 0
        fill(0)
        start(0)
        for j in range(len(chunks)):
            nxt = j + 1 < len(chunks)
            if nxt and self.double_buffer:
                fill(j + 1)                       # other half, while the engine runs chunk j
            t = time.perf_counter()
            cycles += self._mem_wait()
            timing["dma"] += time.perf_counter() - t
            if nxt:
                if not self.double_buffer:
                    drain(j)
                    fill(j + 1)
                start(j + 1)                      # engine on chunk j+1 ...
            if self.double_buffer or not nxt:
                drain(j)                          # ... while the CPU takes chunk j out
        return cycles

    def _mem(self, code: int, a, b=None, scalar: Optional[int] = None, c_reg: Optional[int] = None,
             c_arr=None) -> np.ndarray:
        """Element-wise op with the engine reading a (b, c) from the buffer and writing the result over a."""
        t_start = time.perf_counter()
        timing = dict.fromkeys(("pack", "sync_device", "dma", "sync_cpu", "copy_out"), 0.0)
        shape = np.shape(a)
        ins = [self._bits(a)]
        if b is not None:
            ins.append(self._bits(b))
        elif scalar is None:
            raise ValueError("array_op needs b or scalar")
        if c_arr is not None:
            ins.append(self._bits(c_arr))
        if any(x.size != ins[0].size for x in ins):
            raise ValueError(f"array sizes differ: {[x.size for x in ins]}")
        size = ins[0].size
        out = np.empty(size, self.out_dtype)
        if size == 0:
            return out.reshape(shape)
        self.write(regs.OP, code)
        self.write(regs.STREAM_CFG, 1 << regs.STREAM_CFG_SCALAR_B if scalar is not None and b is None else 0)
        if scalar is not None:
            self.write(regs.OPERAND_B, int(scalar) & self.config.mask)
        if c_reg is not None:
            self.write(regs.OPERAND_C, int(c_reg) & self.config.mask)
        cycles = self._mem_pipeline(ins, out, b is not None, c_arr is not None, timing)
        timing["total"] = time.perf_counter() - t_start
        timing["engine_cycles"] = cycles
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
        if self.master and av.size:
            timing = dict.fromkeys(("pack", "sync_device", "dma", "sync_cpu", "copy_out"), 0.0)
            self._mem_pipeline([av] + ([bv] if bv is not None else []), None, bv is not None, False, timing)
        step = max(1, min(self.buf.size // self.in_bytes, DMA_MAX_LENGTH // self.in_bytes))
        for i in (range(0, av.size, step) if not self.master else ()):
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
                "axpy": dict(scalar=int(a[0]), c=b),                  # axpy: OP_AXPY
                "fma3": dict(b=b, c=a[::-1].copy())}.get(name, dict(b=b))   # three arrays: OP_FMA3
        t0 = time.perf_counter()
        self.array_op("fma" if name in ("axpy", "fma3") else name, a, **args)
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
