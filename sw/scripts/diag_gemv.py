#!/usr/bin/env python3
"""Diagnostyka gemv na płytce: XLOAD i GEMV krok po kroku, z rejestrami po każdym poleceniu.

    sudo python3 scripts/diag_gemv.py          # silnik posit32
    sudo python3 scripts/diag_gemv.py 16       # inny silnik

Każde polecenie czeka najwyżej 2 s (bez wyjątku), potem wypisuje MEM_STATUS, STATUS, liczniki
i kończy. Jeśli skrypt zatrzyma się na wypisywaniu rejestrów (bez "MEM_STATUS ..."), zawiesza się
sam odczyt AXI4-Lite - wtedy Ctrl-C nie pomoże i trzeba zrestartować płytkę.
"""

import sys
import time

import numpy as np

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from positnp import ref, regs                      # noqa: E402
from positnp.backend_hw import PositVecHw          # noqa: E402


def dump(hw, label):
    r = hw.read
    st = r(regs.MEM_STATUS)
    print(f"  {label}: MEM_STATUS 0x{st:02x} (busy {st & 1}, done {st >> 1 & 1}, err range/align/resp/shape "
          f"{st >> 2 & 1}{st >> 3 & 1}{st >> 4 & 1}{st >> 5 & 1}), MEM_CYCLES {r(regs.MEM_CYCLES)}, "
          f"STATUS 0x{r(regs.STATUS):04x}, CNT_ELEMENTS {r(regs.CNT_ELEMENTS)}, CYCLES {r(regs.CYCLES)}", flush=True)
    print(f"     OP {r(regs.OP)}, MEM_LEN {r(regs.MEM_LEN)}, MEM_WLEN {r(regs.MEM_WLEN)}, GEMV_K {r(regs.GEMV_K)}, "
          f"GEMV_COLS {r(regs.GEMV_COLS)}, "
          f"MEM_CFG 0x{r(regs.MEM_CFG):x}, SRC_A 0x{r(regs.MEM_SRC_A):08x}, DST 0x{r(regs.MEM_DST):08x}, "
          f"LO 0x{r(regs.MEM_LO):08x}, HI 0x{r(regs.MEM_HI):08x}", flush=True)
    return st


def cmd(hw, label, op, length, src, dst):
    hw.write(regs.OP, op)
    hw._mem_start(length, [src, 0, 0], dst, False, False)
    t0 = time.perf_counter()
    while not hw.read(regs.MEM_STATUS) >> regs.MEM_ST_DONE & 1:
        if time.perf_counter() - t0 > 2.0:
            dump(hw, f"{label} NIE SKONCZYL po 2 s")
            return False
    dump(hw, f"{label} ok")
    return True


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 32
    hw = PositVecHw.open(n)
    c = hw.config
    print(f"{hw.engine.name}: {c}, wersja 0x{hw.version:04x}, CONFIG 0x{hw.read(regs.CONFIG):08x}, "
          f"ENGINE_CFG 0x{hw.read(regs.ENGINE_CFG):08x}, GEMV_CFG 0x{hw.read(regs.GEMV_CFG):x}, "
          f"gemv_max_k {hw.gemv_max_k}, gemv_cols {hw.gemv_cols}",
          flush=True)
    hw.soft_reset()
    hw.write(regs.CTRL, 1 << regs.CTRL_CLEAR_COUNTERS)
    dump(hw, "po resecie")

    rnd = np.random.default_rng(0)
    a = rnd.integers(0, 1 << n, 64).astype(hw.out_dtype)
    print("add 64 elementow (sciezka z etapu 6):", flush=True)
    t0 = time.perf_counter()
    ok = np.array_equal(hw.array_op("add", a, a[::-1].copy()), ref_add(c, a, a[::-1]))
    print(f"  {'ok' if ok else 'ZLY WYNIK'} ({(time.perf_counter() - t0) * 1e3:.1f} ms)", flush=True)
    dump(hw, "po add")

    P = hw.gemv_cols
    for m, k, cols in ((1, 1, 1), (1, 4, 1), (4, 4, 1), (3, 17, 1), (1, 1, P), (5, 9, P)):
        A = rnd.integers(0, 1 << n, (m, k)).astype(hw.out_dtype)
        x = rnd.integers(0, 1 << n, k * cols).astype(hw.out_dtype)          # cols kolumn po K elementow
        Ad, xd, yd = hw.to_device(A), hw.to_device(x), hw.heap.empty(max(m * cols, 16), hw.out_dtype)
        offs = [hw.heap.offset_of(v) for v in (Ad, xd, yd)]
        for o, v in zip(offs, (Ad, xd, yd)):
            hw.buf.sync_for_device(o, v.nbytes, 1)
        print(f"gemv {m}x{k}, {cols} kolumn: A @ 0x{hw.buf.phys_addr + offs[0]:08x}, x @ 0x{hw.buf.phys_addr + offs[1]:08x}, "
              f"y @ 0x{hw.buf.phys_addr + offs[2]:08x}", flush=True)
        hw.write(regs.GEMV_K, k)
        hw.write(regs.MEM_WLEN, m)
        if hw.version >= 0x0502:
            hw.write(regs.GEMV_COLS, cols)
        if not cmd(hw, "XLOAD", regs.OP_XLOAD, k * cols, offs[1], None):
            return 1
        if not cmd(hw, "GEMV", regs.OP_GEMV, m * k, offs[0], offs[2]):
            return 1
        hw.buf.sync_for_cpu(offs[2], m * cols * hw.out_bytes, 2)
        want = []
        for i in range(m):
            for cc in range(cols):
                q, nar = ref.quire_accumulate(c, "dot", A[i].tolist(), x[cc * k:(cc + 1) * k].tolist())
                want.append(ref.quire_round(c, q, nar)[0])
        got = [int(v) for v in yd[:m * cols]]
        print(f"  y {'ok' if got == want else 'ZLE'}: {[hex(v) for v in got]} (oczekiwane {[hex(v) for v in want]})",
              flush=True)
        del Ad, xd, yd
    hw.close()
    return 0


def ref_add(c, a, b):
    return np.array([ref.op(c, "add", int(x), int(y))[0] for x, y in zip(a, b)])


if __name__ == "__main__":
    sys.exit(main())
