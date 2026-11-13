"""Software backend: the same interface as PositVecHw, computed by vecops (NumPy).

Bit for bit equal to the hardware (both are pinned to PositRef through the shared digests),
fast enough for experiments without the board, and the reference in hardware tests.
Counts the same flags as the hardware counters, so positnp.stats() works on either backend.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from . import ref, vecops
from .ref import Flags, PositConfig

DTYPE = {8: np.uint8, 16: np.uint16, 32: np.uint32}


def bits_dtype(c: PositConfig):
    """Smallest unsigned dtype that holds n bits."""
    for width, dt in sorted(DTYPE.items()):
        if c.n <= width:
            return dt
    raise ValueError(f"{c}: n <= 32 supported")


class PositSw:
    def __init__(self, config: PositConfig = PositConfig(32)):
        self.config = config
        self.ops = ref.OPS
        self.array_ops = ref.OPS + ("sqrt", "fma", "f2p", "p2f")
        self.dtype = bits_dtype(config)
        self.clear_stats()

    def op(self, name: str, a: int, b: int):
        """One operation on raw bits: (bits, Flags), as PositAluHw.op."""
        r = vecops.op(self.config, name, [a], [b])
        self._count(r)
        return int(r.bits[0]), Flags.from_bits(int(r.flags[0]))

    def array_op(self, name: str, a, b=None, scalar: Optional[int] = None, c=None) -> np.ndarray:
        """Element-wise `name` on raw bits (broadcast like NumPy), the same interface as PositVecHw:
        add/sub/mul/div (a (+) b or a (+) scalar), sqrt / f2p / p2f (unary), fma (a * b + c)."""
        cfg = self.config
        if name in ("sqrt", "f2p", "p2f"):
            fn = {"sqrt": vecops.sqrt, "f2p": vecops.from_float32, "p2f": vecops.to_float32}[name]
            r = fn(cfg, a)
        elif name == "fma":
            if c is None:
                raise ValueError("fma needs c")
            other = b if scalar is None else int(scalar) & cfg.mask
            r = vecops.fma(cfg, a, other, c)
        elif name in self.ops:
            if scalar is None:
                if b is None:
                    raise ValueError("array_op needs b or scalar")
                other = b
            else:
                other = int(scalar) & cfg.mask
            r = vecops.op(cfg, name, a, other)
        else:
            raise ValueError(f"unknown operation {name!r}")
        self._count(r)
        return r.bits.astype(self.dtype if name != "p2f" else np.uint32)

    def reduce(self, mode: str, a, b=None):
        """Quire reduction (sum: a, dot: a*b, sumsq: a*a), one rounding: (bits, Flags, q, nar).

        q is the exact quire (int, units of 2**-quire_frac), as PositVecHw.reduce returns it.
        """
        q, nar = vecops.quire(self.config, mode, a, b)
        bits, f = ref.quire_round(self.config, q, nar)
        self._stats["nar"] += f.nar
        self._stats["sat"] += f.sat
        self._stats["inexact"] += f.inexact
        self._stats["elements"] += int(np.size(a))
        return bits, f, q, nar

    def _count(self, r: vecops.Rounded) -> None:
        self._stats["nar"] += int(r.nar.sum())
        self._stats["sat"] += int(r.sat.sum())
        self._stats["inexact"] += int(r.inexact.sum())
        self._stats["elements"] += int(r.bits.size)

    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def clear_stats(self) -> None:
        self._stats = {"nar": 0, "sat": 0, "inexact": 0, "elements": 0}

    def __repr__(self) -> str:
        return f"PositSw({self.config})"
