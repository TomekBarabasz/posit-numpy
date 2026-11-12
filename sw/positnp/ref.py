"""Exact reference model of the Posit Standard (2022), with the ALU flags.

A line-by-line port of PositRef.scala (hw/posit-core/hw/spinal/test): the value is
built as an exact dyadic number m * 2**e, laid out as the full posit bit string and
rounded once on n bits. Independent of the RTL algorithm.

Both models are pinned to the same digests in sw/tests/ref_digests.txt
(Scala: RefDigestTestplan, Python: tests/test_ref.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from . import regs

Dyadic = Tuple[int, int]  # (m, e): m * 2**e


@dataclass(frozen=True)
class PositConfig:
    n: int
    es: int = 2

    def __post_init__(self):
        if self.es < 0 or self.n < self.es + 4:
            raise ValueError(f"posit<{self.n},{self.es}>: need n >= es + 4")

    @property
    def mask(self) -> int:
        return (1 << self.n) - 1

    @property
    def nar(self) -> int:
        return 1 << (self.n - 1)

    @property
    def maxpos(self) -> int:
        return (1 << (self.n - 1)) - 1

    @property
    def max_scale(self) -> int:
        return (self.n - 2) << self.es

    def __str__(self) -> str:
        return f"posit<{self.n},{self.es}>"


@dataclass(frozen=True)
class Flags:
    nar: bool = False
    sat: bool = False
    inexact: bool = False

    @property
    def bits(self) -> int:
        return (regs.FLAG_NAR if self.nar else 0) | (regs.FLAG_SAT if self.sat else 0) | \
               (regs.FLAG_INEXACT if self.inexact else 0)

    @staticmethod
    def from_bits(b: int) -> "Flags":
        return Flags(bool(b & regs.FLAG_NAR), bool(b & regs.FLAG_SAT), bool(b & regs.FLAG_INEXACT))


# ---- exact dyadic arithmetic ----------------------------------------------------------

def _add(x: Dyadic, y: Dyadic) -> Dyadic:
    lo = min(x[1], y[1])
    return ((x[0] << (x[1] - lo)) + (y[0] << (y[1] - lo)), lo)


def _neg(x: Dyadic) -> Dyadic:
    return (-x[0], x[1])


def _mul(x: Dyadic, y: Dyadic) -> Dyadic:
    return (x[0] * y[0], x[1] + y[1])


def _div(x: Dyadic, y: Dyadic) -> Tuple[Dyadic, bool]:
    """Truncated quotient with plenty of bits, plus whether anything was left over."""
    extra = 64 + abs(y[0]).bit_length()
    q, r = divmod(abs(x[0]) << extra, abs(y[0]))
    sign = (1 if x[0] > 0 else -1) * (1 if y[0] > 0 else -1)
    return (q * sign, x[1] - y[1] - extra), r != 0


def _cmp(x: Dyadic, y: Dyadic) -> int:
    d = _add(x, _neg(y))[0]
    return (d > 0) - (d < 0)


# ---- decode / encode -----------------------------------------------------------------------

def decode(c: PositConfig, raw: int) -> Optional[Dyadic]:
    """Exact value of a posit; None stands for NaR."""
    n = c.n
    p = raw & c.mask
    if p == 0:
        return (0, 0)
    if p == c.nar:
        return None
    neg = (p >> (n - 1)) & 1 == 1
    v = (-p) & c.mask if neg else p
    body = [(v >> i) & 1 == 1 for i in range(n - 2, -1, -1)]
    run = 1
    while run < len(body) and body[run] == body[0]:
        run += 1
    k = run - 1 if body[0] else -run
    rest = body[run + 1:]
    exp_bits = (rest + [False] * c.es)[:c.es]
    e = 0
    for b in exp_bits:
        e = (e << 1) | int(b)
    frac = rest[c.es:]
    sig = 1
    for b in frac:
        sig = (sig << 1) | int(b)
    return (-sig if neg else sig, (k << c.es) + e - len(frac))


def encode(c: PositConfig, x: Optional[Dyadic], inexact: bool = False) -> int:
    """Round x to posit<n,es>. `inexact`: the true value lies strictly beyond x."""
    if x is None:
        return c.nar
    m, e = x
    if m == 0:
        return 0
    n, es = c.n, c.es
    mag = abs(m)
    scale = e + mag.bit_length() - 1
    k = scale >> es                      # floor division, as in Scala's Math.floorDiv
    ex = scale - (k << es)
    regime = [True] * (k + 1) + [False] if k >= 0 else [False] * (-k) + [True]
    exponent = [(ex >> i) & 1 == 1 for i in range(es - 1, -1, -1)]
    fraction = [(mag >> i) & 1 == 1 for i in range(mag.bit_length() - 2, -1, -1)]
    string = regime + exponent + fraction
    if len(string) < n:
        string += [False] * (n - len(string))

    body = 0
    for b in string[:n - 1]:
        body = (body << 1) | int(b)
    guard = string[n - 1]
    sticky = any(string[n:]) or inexact
    rounded = body + 1 if guard and ((body & 1) or sticky) else body
    r = max(1, min(rounded, c.maxpos))   # never NaR, never zero
    return (-r) & c.mask if m < 0 else r


def round_(c: PositConfig, x: Optional[Dyadic], inexact: bool = False) -> Tuple[int, Flags]:
    """Result and flags of one rounding (same definitions as PositFlags in hardware)."""
    if x is None:
        return c.nar, Flags(nar=True)
    if x[0] == 0:
        return 0, Flags()
    r = encode(c, x, inexact)
    mag = (abs(x[0]), x[1])
    maxpos = (1, c.max_scale)
    minpos = (1, -c.max_scale)
    vs_max = _cmp(mag, maxpos)
    sat = vs_max > 0 or (vs_max == 0 and inexact) or _cmp(mag, minpos) < 0
    back = decode(c, r)
    exact = not inexact and back is not None and _cmp(back, x) == 0
    return r, Flags(sat=sat, inexact=not exact)


OPS = ("add", "sub", "mul", "div")


def op(c: PositConfig, name: str, a: int, b: int) -> Tuple[int, Flags]:
    """One operation on raw posit bits: (result bits, flags)."""
    x, y = decode(c, a), decode(c, b)
    if x is None or y is None:
        return round_(c, None)
    if name == "add":
        return round_(c, _add(x, y))
    if name == "sub":
        return round_(c, _add(x, _neg(y)))
    if name == "mul":
        return round_(c, _mul(x, y))
    if name == "div":
        if y[0] == 0:
            return round_(c, None)
        q, inexact = _div(x, y)
        return round_(c, q, inexact)
    raise ValueError(f"unknown operation {name!r}, expected one of {OPS}")


def to_float(c: PositConfig, raw: int) -> float:
    """Value as a Python float (NaR -> nan); exact for every posit up to posit<32,2>."""
    x = decode(c, raw)
    if x is None:
        return float("nan")
    return math.ldexp(x[0], x[1])
