"""Elementary functions on posits (plan, stage 7): exp, log, sin, tanh, sigmoid, ...

posit8 / posit16: a table over every bit pattern (256 / 65536 entries), correctly rounded. The value
is computed with float64 libm and rounded to the posit once; the table is certified by rounding each
value perturbed by +-2**-50 (about 4 ulp of float64, more than the libm error): when both round to the
same posit the result is the correctly rounded one. The only entries where the two differ are exact
results lying on a rounding tie (exp2 of an integer, log2 of a power of two - exact in float64 as well),
which round correctly from the float64 value; anything else would be reported by `uncertified()`.

posit32: the same float64 evaluation without a table - correctly rounded except, possibly, for results
within 1 ulp of float64 of a posit32 rounding tie (not certified: "approximate", plan stage 7).

Posit rules (posit standard 2022): NaR in -> NaR out; poles and results outside the domain -> NaR
(log(0), log(-1), arctanh(1), arccosh(0.5)); overflow saturates to +-maxpos and underflow of a nonzero
result to +-minpos (exp(1e9) = maxpos, exp(-1e9) = minpos) - a posit result is never 0 or infinite
unless the exact value is 0.

On the KV260 the posit8 / posit16 engines look the table up in BRAM (OP_TABLE, PositVecAxi 5.3):
PositVecHw.func(name, x) loads the table once (65536 elements) and then runs like any array op.
"""

from __future__ import annotations

import functools
from typing import Callable, Dict, Tuple

import numpy as np

from . import vecops
from .ref import PositConfig


def _sigmoid(x):
    e = np.exp(-np.abs(x))                          # <= 1: no overflow, no cancellation on either side
    return np.where(x >= 0, 1.0 / (1.0 + e), e / (1.0 + e))


# name -> (float64 function, overflow: "sat" saturates, "pole" gives NaR; positive: exact value > 0 everywhere)
FUNCS: Dict[str, Tuple[Callable, str, bool]] = {
    "exp": (np.exp, "sat", True),
    "exp2": (np.exp2, "sat", True),
    "expm1": (np.expm1, "sat", False),
    "log": (np.log, "pole", False),
    "log2": (np.log2, "pole", False),
    "log10": (np.log10, "pole", False),
    "log1p": (np.log1p, "pole", False),
    "sin": (np.sin, "sat", False),
    "cos": (np.cos, "sat", False),
    "tan": (np.tan, "sat", False),
    "arcsin": (np.arcsin, "pole", False),
    "arccos": (np.arccos, "pole", False),
    "arctan": (np.arctan, "sat", False),
    "sinh": (np.sinh, "sat", False),
    "cosh": (np.cosh, "sat", True),
    "tanh": (np.tanh, "sat", False),
    "arcsinh": (np.arcsinh, "sat", False),
    "arccosh": (np.arccosh, "pole", False),
    "arctanh": (np.arctanh, "pole", False),
    "cbrt": (np.cbrt, "sat", False),
    "sigmoid": (_sigmoid, "sat", True),
}

TABLE_MAX_BITS = 16          # posit8 and posit16: whole tables


def _check_name(name: str) -> None:
    if name not in FUNCS:
        raise ValueError(f"unknown function {name!r}, expected one of {tuple(FUNCS)}")


def _evaluate(c: PositConfig, name: str, bits) -> Tuple[np.ndarray, np.ndarray]:
    """(posit bits of f(x), mask of results not certified by the +-4 ulp check)."""
    _check_name(name)
    f, overflow, positive = FUNCS[name]
    bits = np.asarray(bits).astype(np.int64) & c.mask
    x = vecops.to_float(c, bits)
    with np.errstate(all="ignore"):
        y = f(x)
        lo = y * (1 - 2.0 ** -50)
        hi = y * (1 + 2.0 ** -50)
    finite = np.isfinite(y)
    r = vecops.from_float(c, y).bits.astype(np.int64)
    maxpos = c.nar - 1
    if overflow == "sat":                                  # true value finite but beyond float64: +-maxpos
        r = np.where(np.isinf(y), np.where(y > 0, maxpos, (-maxpos) & c.mask), r)
    if positive:                                           # true value > 0, float64 underflowed to 0: minpos
        r = np.where(finite & (y == 0) & ~np.isnan(x), 1, r)
    r = np.where(np.isnan(x), c.nar, r)                    # NaR in -> NaR (also for "sat" functions)
    amb = finite & (y != 0) & (vecops.from_float(c, lo).bits != vecops.from_float(c, hi).bits)
    exact_tie = amb & _exact_in_float64(name, x, y)
    return r & c.mask, amb & ~exact_tie


def _exact_in_float64(name: str, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Results that are exact in float64 (so from_float rounds them correctly even on a tie)."""
    if name == "exp2":
        return np.isfinite(x) & (x == np.round(x))
    if name == "log2":
        return y == np.round(y)                            # log2 of a power of two: an integer
    if name == "cbrt":
        return y * y * y == x
    return np.zeros(np.shape(y), bool)


@functools.lru_cache(maxsize=None)
def _table_cached(c: PositConfig, name: str) -> np.ndarray:
    r, _ = _evaluate(c, name, np.arange(1 << c.n))
    t = r.astype({8: np.uint8, 16: np.uint16}[c.n])
    t.flags.writeable = False
    return t


def table(c: PositConfig, name: str) -> np.ndarray:
    """The whole table of `name` for posit<n>, n <= 16: entry i = f(posit with bits i), read-only."""
    if c.n > TABLE_MAX_BITS:
        raise ValueError(f"{c}: tables only up to {TABLE_MAX_BITS} bits (posit32: evaluate())")
    _check_name(name)
    return _table_cached(c, name)


def uncertified(c: PositConfig, name: str) -> int:
    """Entries of the posit<n> table (n <= 16) whose correct rounding the +-4 ulp check cannot certify."""
    return int(_evaluate(c, name, np.arange(1 << c.n))[1].sum())


def evaluate(c: PositConfig, name: str, bits) -> np.ndarray:
    """f(x) on posit bits, any shape: the table for n <= 16, float64 evaluation for posit32."""
    bits = np.asarray(bits)
    if c.n <= TABLE_MAX_BITS:
        return table(c, name)[bits.astype(np.int64) & c.mask]
    return _evaluate(c, name, bits)[0].astype(np.uint32).reshape(bits.shape)


__all__ = ["FUNCS", "TABLE_MAX_BITS", "table", "uncertified", "evaluate"]
