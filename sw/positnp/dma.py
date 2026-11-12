"""Xilinx AXI DMA (PG021) in direct register mode, driven from user space through UIO.

Only what stage 2 needs: one MM2S transfer (memory -> stream) and one S2MM transfer
(stream -> memory) at a time, completion by polling, no interrupts, no scatter-gather.
Addresses are 32-bit (c_addr_width = 32 in the block design), lengths up to 2**26 - 1 bytes.
"""

from __future__ import annotations

import time

# register offsets
MM2S_DMACR = 0x00
MM2S_DMASR = 0x04
MM2S_SA = 0x18
MM2S_LENGTH = 0x28
S2MM_DMACR = 0x30
S2MM_DMASR = 0x34
S2MM_DA = 0x48
S2MM_LENGTH = 0x58

# DMACR
CR_RS = 1 << 0
CR_RESET = 1 << 2
# DMASR
SR_HALTED = 1 << 0
SR_IDLE = 1 << 1
SR_INT_ERR = 1 << 4
SR_SLV_ERR = 1 << 5
SR_DEC_ERR = 1 << 6
SR_IOC = 1 << 12
SR_ERR_IRQ = 1 << 14
SR_ERRORS = SR_INT_ERR | SR_SLV_ERR | SR_DEC_ERR

MAX_LENGTH = (1 << 26) - 1


class DmaError(RuntimeError):
    pass


def _decode_sr(sr: int) -> str:
    names = [(SR_HALTED, "halted"), (SR_IDLE, "idle"), (SR_INT_ERR, "DMAIntErr"), (SR_SLV_ERR, "DMASlvErr"),
             (SR_DEC_ERR, "DMADecErr"), (SR_IOC, "IOC"), (SR_ERR_IRQ, "Err_Irq")]
    return f"0x{sr:08x} [" + " ".join(n for bit, n in names if sr & bit) + "]"


class AxiDma:
    def __init__(self, mmio, timeout_s: float = 5.0):
        self.mmio = mmio
        self.timeout_s = timeout_s

    def reset(self) -> None:
        """Soft reset of both channels (one bit resets the whole core)."""
        self.mmio.write(MM2S_DMACR, CR_RESET)
        t0 = time.monotonic()
        while self.mmio.read(MM2S_DMACR) & CR_RESET:
            if time.monotonic() - t0 > self.timeout_s:
                raise DmaError("AXI DMA reset did not finish")

    def _start(self, cr: int, sr: int, addr_reg: int, len_reg: int, addr: int, nbytes: int) -> None:
        if not 0 < nbytes <= MAX_LENGTH:
            raise ValueError(f"transfer of {nbytes} B, AXI DMA takes 1 .. {MAX_LENGTH}")
        if addr >> 32:
            raise ValueError(f"address 0x{addr:x} above 4 GB: the DMA has 32-bit addresses")
        self.mmio.write(cr, CR_RS)
        self.mmio.write(sr, SR_IOC | SR_ERR_IRQ)   # write-1-to-clear
        self.mmio.write(addr_reg, addr)
        self.mmio.write(len_reg, nbytes)            # starts the transfer

    def start_s2mm(self, addr: int, nbytes: int) -> None:
        """Arm stream -> memory; finishes at tlast or after nbytes."""
        self._start(S2MM_DMACR, S2MM_DMASR, S2MM_DA, S2MM_LENGTH, addr, nbytes)

    def start_mm2s(self, addr: int, nbytes: int) -> None:
        """Memory -> stream; the core raises tlast on the last beat."""
        self._start(MM2S_DMACR, MM2S_DMASR, MM2S_SA, MM2S_LENGTH, addr, nbytes)

    def _wait(self, sr_reg: int, what: str) -> None:
        t0 = time.monotonic()
        while True:
            sr = self.mmio.read(sr_reg)
            if sr & SR_ERRORS:
                raise DmaError(f"{what}: {_decode_sr(sr)}")
            if sr & SR_IOC:
                return
            if time.monotonic() - t0 > self.timeout_s:
                raise DmaError(f"{what}: no completion after {self.timeout_s} s, DMASR {_decode_sr(sr)}")

    def wait_mm2s(self) -> None:
        self._wait(MM2S_DMASR, "MM2S")

    def wait_s2mm(self) -> int:
        """Wait for S2MM; returns the number of bytes actually written."""
        self._wait(S2MM_DMASR, "S2MM")
        return self.mmio.read(S2MM_LENGTH)

    def status(self) -> str:
        return f"MM2S {_decode_sr(self.mmio.read(MM2S_DMASR))}, S2MM {_decode_sr(self.mmio.read(S2MM_DMASR))}"
