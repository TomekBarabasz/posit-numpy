"""PositArray, first version (plan, stage 2): raw posit bits + format + backend.

    import positnp as pn
    x = pn.PositArray.from_float([1.0, 2.5, -3.0], pn.PositConfig(32))
    y = x * x + x                      # each operator is one array_op on the backend
    y.to_float()                       # numpy float64
    pn.set_backend(PositVecHw.open())  # the KV260; default: PositSw (ref.py)

Conversions float <-> posit are exact (round to nearest, ties to even, through ref.encode),
element by element and slow; the vectorised ones come in stage 3, as do the NumPy protocols.
"""

from __future__ import annotations

import math
from typing import Optional, Union

import numpy as np

from . import ref
from .backend_sw import DTYPE, PositSw
from .ref import PositConfig

_backend = None


def set_backend(backend) -> None:
    """Backend for new operations: an object with config, ops and array_op (PositSw, PositVecHw)."""
    global _backend
    _backend = backend


def get_backend(config: PositConfig):
    """The current backend if it computes `config`, otherwise a PositSw for it."""
    if _backend is not None and _backend.config == config:
        return _backend
    return PositSw(config)


def float_to_bits(c: PositConfig, x: float) -> int:
    """One float -> posit bits, exact rounding; nan and inf -> NaR."""
    if math.isnan(x) or math.isinf(x):
        return c.nar
    if x == 0.0:
        return 0
    num, den = float(x).as_integer_ratio()   # den is a power of two
    return ref.encode(c, (num, -(den.bit_length() - 1)))


class PositArray:
    def __init__(self, bits, config: PositConfig = PositConfig(32), backend=None):
        dtype = DTYPE.get(config.n, np.uint64)
        self.bits = (np.asarray(bits).astype(np.int64) & config.mask).astype(dtype)
        self.config = config
        self.backend = backend

    # ---- construction and conversion ----------------------------------------------------

    @classmethod
    def from_float(cls, values, config: PositConfig = PositConfig(32), backend=None) -> "PositArray":
        x = np.asarray(values, dtype=np.float64)
        bits = np.fromiter((float_to_bits(config, v) for v in x.ravel().tolist()), dtype=np.int64, count=x.size)
        return cls(bits.reshape(x.shape), config, backend)

    def to_float(self) -> np.ndarray:
        flat = [ref.to_float(self.config, int(v)) for v in self.bits.ravel().tolist()]
        return np.array(flat, dtype=np.float64).reshape(self.bits.shape)

    # ---- operations -----------------------------------------------------------------------

    def _backend(self):
        return self.backend if self.backend is not None else get_backend(self.config)

    def _binary(self, name: str, other: Union["PositArray", float, int]) -> "PositArray":
        be = self._backend()
        if isinstance(other, PositArray):
            if other.config != self.config:
                raise TypeError(f"{self.config} {name} {other.config}: formats differ")
            if other.bits.shape != self.bits.shape:
                raise ValueError(f"shapes {self.bits.shape} and {other.bits.shape} (broadcasting comes in stage 3)")
            r = be.array_op(name, self.bits, other.bits)
        else:
            r = be.array_op(name, self.bits, scalar=float_to_bits(self.config, float(other)))
        return PositArray(r, self.config, self.backend)

    def add(self, other):
        return self._binary("add", other)

    def sub(self, other):
        return self._binary("sub", other)

    def mul(self, other):
        return self._binary("mul", other)

    def div(self, other):
        return self._binary("div", other)

    __add__ = add
    __sub__ = sub
    __mul__ = mul
    __truediv__ = div

    # ---- container --------------------------------------------------------------------------

    @property
    def shape(self):
        return self.bits.shape

    def __len__(self) -> int:
        return len(self.bits)

    def __getitem__(self, idx) -> "PositArray":
        return PositArray(np.atleast_1d(self.bits[idx]), self.config, self.backend)

    def __eq__(self, other) -> bool:
        return isinstance(other, PositArray) and other.config == self.config and np.array_equal(other.bits, self.bits)

    def __repr__(self) -> str:
        vals = self.to_float() if self.bits.size <= 8 else self[:8].to_float()
        more = "" if self.bits.size <= 8 else f", ... ({self.bits.size} elements)"
        return f"PositArray({np.array2string(vals.ravel(), separator=', ')}{more}, {self.config})"
