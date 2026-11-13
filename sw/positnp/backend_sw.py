"""Software backend: the same array operations as PositVecHw, element by element on ref.py.

Bit for bit equal to the hardware by construction (both are checked against PositRef);
slow (pure Python), meant as the reference in tests and as the fallback without a board.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from . import ref
from .ref import PositConfig

DTYPE = {8: np.uint8, 16: np.uint16, 32: np.uint32}


class PositSw:
    def __init__(self, config: PositConfig = PositConfig(32)):
        self.config = config
        self.ops = ref.OPS
        self.dtype = DTYPE.get(config.n, np.uint64)

    def _bits(self, x) -> np.ndarray:
        return (np.asarray(x).astype(np.int64) & self.config.mask).ravel()

    def op(self, name: str, a: int, b: int):
        return ref.op(self.config, name, a, b)

    def array_op(self, name: str, a, b=None, scalar: Optional[int] = None) -> np.ndarray:
        shape = np.shape(a)
        av = self._bits(a)
        if scalar is None:
            if b is None:
                raise ValueError("array_op needs b or scalar")
            bv = self._bits(b)
            if bv.size != av.size:
                raise ValueError(f"a has {av.size} elements, b {bv.size}")
            pairs = zip(av.tolist(), bv.tolist())
        else:
            s = int(scalar) & self.config.mask
            pairs = ((x, s) for x in av.tolist())
        out = np.fromiter((ref.op(self.config, name, x, y)[0] for x, y in pairs), dtype=self.dtype, count=av.size)
        return out.reshape(shape)
