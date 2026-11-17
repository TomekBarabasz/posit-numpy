"""Shared helpers for the numbered examples: backend selection, error metrics, small tables.

Every example runs in three ways:

    python3 examples/10_arithmetic.py                  # PC or board: PositSw (software, NumPy), bit-exact with the PL
    python3 examples/10_arithmetic.py --sim            # PC: the real driver on register-level models of the engines
    sudo python3 examples/10_arithmetic.py --hw        # KV260: the application that is already loaded (pn.board.open)
    sudo python3 examples/10_arithmetic.py --hw posit32_16_8_mem   # KV260: load this application first

The results are the same bits in all three cases - that is the point of the software backend.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Iterable, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # sw/, so the examples run without pip install
import positnp as pn  # noqa: E402

LANES = {32: 2, 16: 4, 8: 8}           # elements per clock of the wide engines on the KV260


def parser(doc: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(doc or "").strip().split("\n")[0],
                                 epilog="Without --hw / --sim everything runs in software (PositSw).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--hw", nargs="?", const="", metavar="APP",
                   help="KV260: load application APP (pn.board.load), or without APP use the loaded one (pn.board.open)")
    g.add_argument("--sim", action="store_true",
                   help="no board: the hardware driver (PositVecHw) on register-level models of the engines")
    return ap


def start(doc: str, argv: Sequence[str] = None, quiet: bool = False) -> argparse.Namespace:
    """Parse the common options, register the backends; args.engines = {n: engine} ({} for PositSw)."""
    args = parser(doc).parse_args(argv)
    args.engines = setup(args, quiet)
    return args


def setup(args: argparse.Namespace, quiet: bool = False) -> Dict[int, object]:
    if getattr(args, "hw", None) is not None:
        engines = pn.board.load(args.hw) if args.hw else pn.board.open()
        if not quiet:
            print(pn.board.describe())
        return engines
    if getattr(args, "sim", False):
        engines = simulated_engines()
        pn.set_backend(list(engines.values()))
        if not quiet:
            print("backend: simulated engines (PositVecHw on positnp.fakes): "
                  + ", ".join(str(e.config) for e in engines.values()))
        return engines
    pn.set_backend(None)
    if not quiet:
        print("backend: PositSw (software, bit-exact with the KV260 engines; --hw on the board)")
    return {}


def simulated_engines(widths: Iterable[int] = (32, 16, 8), mem_bytes: int = 1 << 23) -> Dict[int, object]:
    """PositVecHw engines on register-level models (positnp.fakes): the driver code of the board, on a PC.

    Each has its own AXI4 master (stage 6), lanes as on the KV260, gemv (matmul / FIR), the function
    table for posit8 / posit16 and the FFT core for posit16 / posit32. Slower than PositSw - they model
    the registers, the device memory and the command protocol."""
    from positnp.backend_hw import PositVecHw
    from positnp.fakes import FakeVecMmio
    from positnp.udmabuf import FakeUDmaBuf
    out = {}
    for n in widths:
        mem = FakeUDmaBuf(mem_bytes)
        vec = FakeVecMmio(pn.PositConfig(n), mem=mem, lanes=LANES.get(n, 1), units=2)
        out[n] = PositVecHw(vec, None, mem)
    return out


# ---- numbers ------------------------------------------------------------------------------------

def f64(y) -> np.ndarray:
    """float64 (or complex128) values of a PositArray, PositComplex or anything NumPy takes."""
    if isinstance(y, pn.PositArray):
        return y.to_float()
    if hasattr(y, "to_complex"):
        return y.to_complex()
    return np.asarray(y).astype(np.complex128 if np.iscomplexobj(y) else np.float64)


def rel_err(y, ref) -> float:
    """||y - ref|| / ||ref|| (2-norm, in float64)."""
    y, ref = f64(y), f64(ref)
    return float(np.linalg.norm((y - ref).ravel()) / np.linalg.norm(ref.ravel()))


def digits(err: float) -> float:
    """Correct decimal digits for a relative error."""
    return float("inf") if err == 0 else float(-np.log10(err))


def round_minifloat(x, exp_bits: int, man_bits: int, saturate: bool = False) -> np.ndarray:
    """Round float64 values to a small binary float (IEEE-style, subnormals, ties to even).

    (5, 10): float16, (8, 7): bfloat16, (4, 3) with saturate: OCP float8 E4M3 (max 448, no inf),
    (5, 2): float8 E5M2 (max 57344, inf). For formats NumPy does not have."""
    x = np.asarray(x, dtype=np.float64)
    bias = (1 << (exp_bits - 1)) - 1
    if saturate and (exp_bits, man_bits) == (4, 3):        # E4M3: the top encoding is NaN, so max = 1.75 * 2**8
        max_val = 448.0
    else:
        max_val = (2 - 2.0 ** -man_bits) * 2.0 ** bias
    with np.errstate(divide="ignore", invalid="ignore"):
        e = np.floor(np.log2(np.abs(x)))
    e = np.where(np.isfinite(e), np.maximum(e, 1 - bias), 1 - bias)  # below the normal range: subnormal spacing
    q = 2.0 ** (e - man_bits)
    r = np.round(x / q) * q                                         # np.round: ties to even
    with np.errstate(invalid="ignore"):
        big = np.abs(r) > max_val
        r = np.where(big, np.sign(x) * (max_val if saturate else np.inf), r)
    return np.where(np.isnan(x), np.nan, r)


# ---- printing -----------------------------------------------------------------------------------

def section(title: str) -> None:
    print(f"\n== {title} " + "=" * max(3, 74 - len(title)))


def table(rows: Sequence[Sequence], headers: Sequence[str], fmt: str = "{:.3g}") -> None:
    """A plain text table; floats with `fmt`, everything else with str()."""
    cells = [[h for h in headers]] + [[fmt.format(v) if isinstance(v, (float, np.floating)) else str(v) for v in r]
                                      for r in rows]
    w = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    for j, r in enumerate(cells):
        print("  " + "  ".join(c.rjust(w[i]) if i else c.ljust(w[i]) for i, c in enumerate(r)))
        if j == 0:
            print("  " + "  ".join("-" * x for x in w))
