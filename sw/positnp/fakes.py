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
                 stage5: bool = True, k: int = 4, units: int = 1, mem: Optional[FakeUDmaBuf] = None, lanes: int = 1,
                 lazy_mem: bool = False, gemv_k: Optional[int] = None, gemv_cols: int = 4,
                 table: Optional[bool] = None, fft_max_s: Optional[int] = None):
        super().__init__(config, ops, busy_reads)
        self.engine_cfg = lanes << 16 | units << 8 | k
        # stage 6: with `mem` the engine has its own AXI4 master and works on that buffer (MEM_* registers)
        self.mem = mem
        for r in (regs.MEM_LEN, regs.MEM_SRC_A, regs.MEM_SRC_B, regs.MEM_SRC_C, regs.MEM_DST,
                  regs.MEM_LO, regs.MEM_HI, regs.MEM_CFG, regs.MEM_STATUS):
            self.regs[r] = 0
        self.mem_log = []
        # stage 7a: gemv (x buffer of gemv_k elements); default 4096 with the master, none without
        self.gemv_k = (4096 if mem is not None else 0) if gemv_k is None else gemv_k
        self.gemv_cols = gemv_cols if self.gemv_k else 0
        self.xbuf = np.zeros((max(self.gemv_cols, 1), max(self.gemv_k, 1)), np.uint64)
        self.regs[regs.MEM_WLEN] = 0
        self.regs[regs.GEMV_K] = 0
        self.regs[regs.GEMV_COLS] = 1
        self.regs[regs.GEMV_MODE] = 0
        # 5.3: table of 2**n entries (posit8 / posit16 with the master)
        self.table = (mem is not None and config.n <= 16) if table is None else table
        self.tab = np.zeros(1 << config.n if self.table else 1, np.uint64)
        self.regs[regs.TABLE_CTRL] = 0
        self.table_loads = 0
        # 5.5: FFT core (fft_max_s: largest S, N = 4^S; 0: none)
        self.fft_max_s = (6 if mem is not None and config.n >= 16 else 0) if fft_max_s is None else fft_max_s
        self.regs[regs.FFT_CTRL] = 0
        self.regs[regs.FFT_CFG] = 2
        self.fft_tw = None
        self.fft_tw_loads = 0
        # lazy_mem: the command runs only at the first MEM_STATUS read after the start - the latest
        # moment real hardware could read its input - so a driver that touches a half of the buffer
        # still in use (or starts a command before the previous one finished) gets wrong results
        self.lazy_mem = lazy_mem
        self.mem_pending = False
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
            if self.mem is not None and "fma" in self.stage5:
                s5 |= 1 << regs.OP_FMA3
            master = (1 << regs.CONFIG_HAS_MASTER) if self.mem is not None else 0
            if self.gemv_k:
                master |= 1 << regs.CONFIG_HAS_GEMV
                s5 |= 1 << regs.OP_XLOAD | 1 << regs.OP_GEMV
            if self.table:
                s5 |= 1 << regs.OP_TABLE
            return super().read(offset) | 1 << regs.CONFIG_HAS_STREAM | 1 << regs.CONFIG_HAS_QUIRE | master | \
                (reduce_mask | s5) << 16
        if offset == regs.FFT_INFO:
            return self.fft_max_s
        if offset in (regs.FFT_CTRL, regs.FFT_CFG):
            return self.regs[offset]
        if offset == regs.TABLE_CFG:
            return (2 << 8 | self.config.n) if self.table else 0
        if offset == regs.TABLE_CTRL:
            return self.regs[offset]
        if offset == regs.GEMV_CFG:
            return self.gemv_cols << 16 | self.gemv_k if self.gemv_k else 0
        if offset in (regs.MEM_WLEN, regs.GEMV_K, regs.GEMV_COLS, regs.GEMV_MODE):
            return self.regs[offset]
        if offset == regs.CNT_ELEMENTS:
            self.log.append(("r", offset))
            return self.counters["elements"]
        if offset == regs.ENGINE_CFG:
            return self.engine_cfg
        if offset == regs.MEM_STATUS and self.mem_pending:
            self.mem_pending = False
            self.process_mem()
        if offset in (regs.MEM_LEN, regs.MEM_SRC_A, regs.MEM_SRC_B, regs.MEM_SRC_C, regs.MEM_DST,
                      regs.MEM_LO, regs.MEM_HI, regs.MEM_CFG, regs.MEM_STATUS):
            return self.regs[offset]
        if offset == regs.MEM_CYCLES:
            return self.regs.get(regs.MEM_CYCLES, 0)
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
        if offset in (regs.MEM_LEN, regs.MEM_SRC_A, regs.MEM_SRC_B, regs.MEM_SRC_C, regs.MEM_DST,
                      regs.MEM_LO, regs.MEM_HI, regs.MEM_CFG):
            self.regs[offset] = value & 0xFFFFFFFF
            return
        if offset in (regs.FFT_CTRL, regs.FFT_CFG) and self.fft_max_s:
            self.regs[offset] = value & 7
            return
        if offset == regs.TABLE_CTRL and self.table:
            self.regs[offset] = value & 1
            return
        if offset in (regs.MEM_WLEN, regs.GEMV_K, regs.GEMV_COLS, regs.GEMV_MODE) and self.gemv_k:
            mask = {regs.MEM_WLEN: 0xFFFFFFFF, regs.GEMV_K: 2 * self.gemv_k - 1, regs.GEMV_COLS: 0x1F,
                    regs.GEMV_MODE: 1}[offset]
            self.regs[offset] = value & mask
            return
        if offset == regs.CTRL and value >> regs.CTRL_MEM_START & 1 and self.mem is not None:
            if self.mem_pending:
                raise AssertionError("MEM start while the previous command is still running")
            if self.lazy_mem:
                self.mem_pending = True
                self.regs[regs.MEM_STATUS] = 1 << regs.MEM_ST_BUSY
                self.mem_regs_at_start = dict(self.regs)
            else:
                self.process_mem()
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

    def process_mem(self) -> None:
        """MEM command as PositVecAxi 5.0: range / alignment checks, then the op on the buffer."""
        r = self.regs
        n, by = self.config.n, self.config.n // 8
        length, cfg = r[regs.MEM_LEN], r[regs.MEM_CFG]
        op = r[regs.OP]
        gemv = bool(self.gemv_k) and op == regs.OP_GEMV
        xload = bool(self.gemv_k) and op == regs.OP_XLOAD
        fctl = r[regs.FFT_CTRL] if self.fft_max_s else 0
        is_tw, is_fft = bool(fctl & 2), bool(fctl & 1) and not fctl & 2
        if is_tw or is_fft:
            op = -1                                                  # FFT mode: OP does not matter
        gemv = gemv and op == regs.OP_GEMV
        xload = xload and op == regs.OP_XLOAD
        tload = self.table and op == regs.OP_TABLE and bool(r[regs.TABLE_CTRL] & 1)
        lookup = self.table and op == regs.OP_TABLE and not tload
        use = [True, bool(cfg >> regs.MEM_CFG_READ_B & 1), bool(cfg >> regs.MEM_CFG_READ_C & 1)]
        write = bool(cfg >> regs.MEM_CFG_WRITE & 1) and not xload and not tload and not is_tw
        srcs = [r[regs.MEM_SRC_A], r[regs.MEM_SRC_B], r[regs.MEM_SRC_C]]
        nbytes = -(-length * by // 16) * 16
        cols = r[regs.GEMV_COLS]
        wlen = r[regs.MEM_WLEN] * cols if gemv else length
        wbytes = -(-wlen * by // 16) * 16
        bad_range = any(a < r[regs.MEM_LO] or a + nbytes > r[regs.MEM_HI] for a, u in zip(srcs, use) if u) or \
            (write and (r[regs.MEM_DST] < r[regs.MEM_LO] or r[regs.MEM_DST] + wbytes > r[regs.MEM_HI]))
        bad_align = any(a % 16 for a, u in zip(srcs, use) if u) or (write and r[regs.MEM_DST] % 16)
        k = r[regs.GEMV_K]
        bad_k = k == 0 or k > self.gemv_k or cols == 0 or cols > self.gemv_cols
        window = gemv and bool(r[regs.GEMV_MODE] & 1)
        rows_m = r[regs.MEM_WLEN]
        bad_len = ((rows_m - 1) * cols + k != length or rows_m == 0 or cols > k) if window else rows_m * k != length
        bad_shape = (gemv and (bad_k or bad_len or wlen > 0xFFFFFFFF)) or \
            (xload and (bad_k or k * cols != length)) or (tload and length != self.tab.size)
        S = r[regs.FFT_CFG]
        s_ok = 2 <= S <= self.fft_max_s
        if is_fft:
            bad_shape = not s_ok or r[regs.MEM_WLEN] == 0 or length != r[regs.MEM_WLEN] * 2 * 4 ** S
        if is_tw:
            bad_shape = not s_ok or length != 3 * 4 ** S // 2
        self.mem_log.append((length, srcs, r[regs.MEM_DST], cfg))
        st = 1 << regs.MEM_ST_DONE | bad_range << regs.MEM_ST_ERR_RANGE | bad_align << regs.MEM_ST_ERR_ALIGN | \
            bad_shape << regs.MEM_ST_ERR_SHAPE
        if bad_range or bad_align or bad_shape or length == 0:
            r[regs.MEM_STATUS] = st
            return
        dt = {8: np.uint8, 16: np.uint16, 32: np.uint32}[n]
        view = lambda a: np.frombuffer(self.mem.memory, dt, length, a - self.mem.phys_addr)   # noqa: E731
        a = view(srcs[0]).astype(np.uint64)
        b = view(srcs[1]).astype(np.uint64) if use[1] else np.zeros(length, np.uint64)
        if is_tw:
            h = length // 2
            self.fft_tw = (a[:h].astype(np.int64), a[h:].astype(np.int64))
            self.fft_tw_loads += 1
        elif is_fft:
            from . import fft as F
            N = 4 ** S
            x = a.astype(np.int64).reshape(-1, 2, N)
            cj = bool(fctl & 4)
            xi = (-x[:, 1]) & self.config.mask if cj else x[:, 1]
            yr, yi = F._core_rows(self.config, x[:, 0], xi, N, self.fft_tw, False)
            if cj:
                yi = (-yi) & self.config.mask
            if write:
                np.frombuffer(self.mem.memory, dt, length, r[regs.MEM_DST] - self.mem.phys_addr)[:] = \
                    np.stack([yr, yi], axis=1).reshape(-1).astype(dt)
        elif tload:
            self.tab[:] = a
            self.table_loads += 1
        elif lookup:
            self.counters["elements"] += length
            if write:
                view(r[regs.MEM_DST])[:] = self.tab[a.astype(np.int64)].astype(dt)
        elif xload:
            self.xbuf[:cols, :k] = a.reshape(cols, k)
        elif gemv:
            xs = [self.xbuf[cc, :k].tolist() for cc in range(cols)]
            if window:                                       # row i = a[i * cols .. + K); column p: m in [p, p + T)
                t = k - cols + 1
                al = a.tolist()
                rows = [al[i * cols:i * cols + k] for i in range(rows_m)]
                xs = None
            else:
                rows = a.reshape(rows_m, k).tolist()
            y = []
            for row in rows:
                for p in range(cols):
                    if xs is None:
                        rr, x = row[p:p + t], self.xbuf[p, p:p + t].tolist()
                    else:
                        rr, x = row, xs[p]
                    q, nar = ref.quire_accumulate(self.config, "dot", rr, x, 0, False)
                    v, f = ref.quire_round(self.config, q, nar)
                    y.append(v)
                    for kk in ("nar", "sat", "inexact"):
                        self.counters[kk] += getattr(f, kk)
            self.counters["elements"] += length
            if write:
                np.frombuffer(self.mem.memory, dt, wlen, r[regs.MEM_DST] - self.mem.phys_addr)[:] = \
                    np.array(y, dtype=np.uint64).astype(dt)
        elif self.is_reduce():
            words = a | (b << np.uint64(n))
            if self.regs[regs.OP] == regs.OP_SUMSQ:
                words = a | (a << np.uint64(n))
            self.process_quire(words)
        elif self.regs[regs.OP] == regs.OP_FMA3:
            c = view(srcs[2]).tolist()
            res = []
            for x, y, z in zip(a.tolist(), b.tolist(), c):
                v, f = ref.fma(self.config, x, y, z)
                res.append(v)
                for kk in ("nar", "sat", "inexact"):
                    self.counters[kk] += getattr(f, kk)
            self.counters["elements"] += length
            view(r[regs.MEM_DST])[:] = np.array(res, dtype=np.uint64).astype(dt)
        else:
            res = self.process_stream(a | (b << np.uint64(n)))
            if write:
                view(r[regs.MEM_DST])[:] = res.astype(dt)
        r[regs.MEM_STATUS] = st
        r[regs.MEM_CYCLES] = length + 20

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
