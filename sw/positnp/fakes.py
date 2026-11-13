"""Register-level models of the stage 2 hardware on ref.py, for running PositVecHw on a PC.

    mem = FakeUDmaBuf(1 << 20)
    vec = FakeVecMmio(PositConfig(32))
    hw  = PositVecHw(vec, FakeDma(mem, vec), mem)

FakeDma moves data between the fake buffer and FakeVecMmio.process_stream the way
AXI DMA moves it between DDR and PositVecAxi: MM2S reads {b, a} words, S2MM writes
n-bit results; IOC bits on completion. They check the driver, not the hardware.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from . import dma as d
from . import ref, regs
from .backend_hw import FakeAluMmio
from .ref import PositConfig
from .udmabuf import FakeUDmaBuf


class FakeVecMmio(FakeAluMmio):
    """PositVecAxi registers: stage 1 map plus STREAM_CFG, CNT_ELEMENTS, version 2.0."""

    def __init__(self, config: PositConfig = PositConfig(32), ops=("add", "sub", "mul", "div"), busy_reads: int = 1,
                 stage5: bool = True):
        super().__init__(config, ops, busy_reads)
        self.stage5 = ("sqrt", "fma", "axpy") + (("f2p", "p2f") if config.n == 32 else ()) if stage5 else ()
        self.regs[regs.STREAM_CFG] = 0
        self.regs[regs.OPERAND_C] = 0
        self.counters["elements"] = 0
        self.quire, self.quire_nar = 0, False
        self.quire_raw = 0

    def read(self, offset: int) -> int:
        if offset == regs.ID_VERSION:
            self.log.append(("r", offset))
            return regs.ID << 16 | regs.VERSION_VEC
        if offset == regs.CONFIG:
            self.log.append(("r", offset))
            reduce_mask = sum(1 << v for v in regs.REDUCE.values())
            s5 = sum(1 << regs.STAGE5[n] for n in self.stage5)
            return super().read(offset) | 1 << regs.CONFIG_HAS_STREAM | 1 << regs.CONFIG_HAS_QUIRE | (reduce_mask | s5) << 16
        if offset == regs.CNT_ELEMENTS:
            self.log.append(("r", offset))
            return self.counters["elements"]
        if offset == regs.QUIRE_CFG:
            return self.config.quire_frac << 16 | self.config.quire_width()
        if regs.QUIRE <= offset < regs.QUIRE + self.config.quire_width() // 8:
            return (self.quire_raw >> (8 * (offset - regs.QUIRE))) & 0xFFFFFFFF
        if offset == regs.STATUS:
            return super().read(offset) | int(self.quire_nar) << regs.STATUS_QUIRE_NAR
        return super().read(offset)

    def write(self, offset: int, value: int) -> None:
        if offset == regs.STREAM_CFG:
            self.log.append(("w", offset, value))
            self.regs[offset] = value & 1
            return
        if offset == regs.OPERAND_C:
            self.log.append(("w", offset, value))
            self.regs[offset] = value & self.config.mask
            return
        if offset == regs.CTRL and value >> regs.CTRL_CLEAR_COUNTERS & 1:
            self.counters["elements"] = 0
        if offset == regs.CTRL and value >> regs.CTRL_QUIRE_CLEAR & 1:
            self.quire, self.quire_nar = 0, False
        if offset == regs.CTRL and value >> regs.CTRL_QUIRE_ROUND & 1:
            r, f = ref.quire_round(self.config, self.quire, self.quire_nar)
            self.regs[regs.RESULT] = r
            self.flags = f.bits
            for k in ("nar", "sat", "inexact"):
                self.counters[k] += getattr(f, k)
            self.quire_raw = ref.quire_raw(self.config, self.quire)
            self.done, self.error, self.busy = True, False, False
            return
        if offset == regs.OP:
            self.log.append(("w", offset, value))
            self.regs[offset] = value & 0xF
            return
        super().write(offset, value)

    def is_reduce(self) -> bool:
        return self.regs[regs.OP] in regs.REDUCE.values()

    def process_quire(self, words: np.ndarray) -> None:
        n = self.config.n
        mode = next(k for k, v in regs.REDUCE.items() if v == self.regs[regs.OP])
        a = [w & self.config.mask for w in words.tolist()]
        b = [(w >> n) & self.config.mask for w in words.tolist()]
        self.quire, self.quire_nar = ref.quire_accumulate(self.config, mode, a, b, self.quire, self.quire_nar)
        self.counters["elements"] += len(words)

    def process_stream(self, words: np.ndarray) -> np.ndarray:
        """{b, a} words -> results with the current OP / STREAM_CFG / OPERAND_B, like PositStreamAlu."""
        n = self.config.n
        code = self.regs[regs.OP]
        name = next((k for k, v in regs.OPS.items() if v == code and k in self.ops), None)
        scalar_b = self.regs[regs.STREAM_CFG] >> regs.STREAM_CFG_SCALAR_B & 1
        out = np.empty(len(words), dtype=np.uint64)
        for i, w in enumerate(words.tolist()):
            a, b = w & self.config.mask, (w >> n) & self.config.mask
            if scalar_b:
                b = self.regs[regs.OPERAND_B]
            s5 = {v: k for k, v in regs.STAGE5.items() if k in self.stage5}.get(code)
            c = self.regs[regs.OPERAND_C]
            if s5 == "sqrt":
                r, f = ref.sqrt(self.config, a)
            elif s5 == "fma":
                r, f = ref.fma(self.config, a, b, c)
            elif s5 == "axpy":                        # a * OPERAND_B + b (stream)
                r, f = ref.fma(self.config, a, self.regs[regs.OPERAND_B], b)
            elif s5 == "f2p":
                r, f = ref.from_float32(self.config, a)
            elif s5 == "p2f":
                r, f = ref.to_float32(self.config, a)
            elif name is None:
                r, f = self.config.nar, ref.Flags(nar=True)
            else:
                r, f = ref.op(self.config, name, a, b)
            out[i] = r
            for k in ("nar", "sat", "inexact"):
                self.counters[k] += getattr(f, k)
        self.counters["elements"] += len(words)
        self.counters["cycles"] += len(words)
        return out


class FakeDma:
    """AXI DMA registers in direct mode over a FakeUDmaBuf; `fault` injects an error ("slverr", "hang")."""

    def __init__(self, mem: FakeUDmaBuf, vec: FakeVecMmio, fault: Optional[str] = None):
        self.mem = mem
        self.vec = vec
        self.fault = fault
        self.r = {k: 0 for k in (d.MM2S_DMACR, d.MM2S_DMASR, d.MM2S_SA, d.MM2S_LENGTH,
                                 d.S2MM_DMACR, d.S2MM_DMASR, d.S2MM_DA, d.S2MM_LENGTH)}
        self.r[d.MM2S_DMASR] = self.r[d.S2MM_DMASR] = d.SR_HALTED
        self.s2mm_armed = False
        self.transfers = []

    def read(self, offset: int) -> int:
        return self.r.get(offset, 0)

    def write(self, offset: int, value: int) -> None:
        if offset in (d.MM2S_DMASR, d.S2MM_DMASR):
            self.r[offset] &= ~(value & (d.SR_IOC | d.SR_ERR_IRQ))
            return
        if offset == d.MM2S_DMACR and value & d.CR_RESET:
            self.r[d.MM2S_DMASR] = self.r[d.S2MM_DMASR] = d.SR_HALTED
            self.s2mm_armed = False
            return
        self.r[offset] = value
        if offset in (d.MM2S_DMACR, d.S2MM_DMACR) and value & d.CR_RS:
            sr = d.MM2S_DMASR if offset == d.MM2S_DMACR else d.S2MM_DMASR
            self.r[sr] &= ~d.SR_HALTED
        if offset == d.S2MM_LENGTH:
            self.s2mm_armed = True
        if offset == d.MM2S_LENGTH:
            self._run()

    def _run(self) -> None:
        n = self.vec.config.n
        in_b, out_b = 2 * n // 8, n // 8
        nbytes = self.r[d.MM2S_LENGTH]
        if nbytes % in_b:
            raise AssertionError(f"MM2S length {nbytes} not a multiple of the {in_b}-byte word")
        if self.vec.is_reduce() and self.fault is None:
            in_dtype = {8: np.uint16, 16: np.uint32, 32: np.uint64}[n]
            src = self.r[d.MM2S_SA] - self.mem.phys_addr
            self.vec.process_quire(np.frombuffer(self.mem.memory, in_dtype, nbytes // in_b, src))
            self.r[d.MM2S_DMASR] |= d.SR_IOC | d.SR_IDLE
            return
        if self.fault == "hang" or not self.s2mm_armed:
            return  # no IOC: the driver must time out
        if self.fault == "slverr":
            self.r[d.MM2S_DMASR] |= d.SR_SLV_ERR | d.SR_ERR_IRQ
            return
        k = nbytes // in_b
        src = self.r[d.MM2S_SA] - self.mem.phys_addr
        dst = self.r[d.S2MM_DA] - self.mem.phys_addr
        in_dtype = {8: np.uint16, 16: np.uint32, 32: np.uint64}[n]
        out_dtype = {8: np.uint8, 16: np.uint16, 32: np.uint32}[n]
        res = self.vec.process_stream(np.frombuffer(self.mem.memory, in_dtype, k, src))
        if k * out_b > self.r[d.S2MM_LENGTH]:
            raise AssertionError("S2MM armed for fewer bytes than the stream delivers")
        np.frombuffer(self.mem.memory, out_dtype, k, dst)[:] = res.astype(out_dtype)
        self.transfers.append((src, nbytes, dst, k * out_b))
        self.r[d.S2MM_LENGTH] = k * out_b
        self.r[d.MM2S_DMASR] |= d.SR_IOC | d.SR_IDLE
        self.r[d.S2MM_DMASR] |= d.SR_IOC | d.SR_IDLE
        self.s2mm_armed = False
