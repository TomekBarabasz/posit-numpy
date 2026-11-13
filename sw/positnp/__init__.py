"""positnp: NumPy-style posit arrays, computed in KV260 PL or in software.

    import numpy as np, positnp as pn
    x = pn.array(np.linspace(-2, 2, 5))       # posit<32,2>; pn.array(x, pn.P16) for posit<16,2>
    (x * x - 1).to_float()
"""

from . import ref, regs, vecops
from .array import (P8, P16, P32, PositArray, array, axpy, clear_stats, fma, from_bits, get_backend, nar, quire,
                    set_backend, stats, using, zeros)
from .backend_sw import PositSw
from .ref import Flags, PositConfig

__version__ = "0.5.0"

__all__ = ["ref", "regs", "vecops", "Flags", "PositConfig", "PositArray", "PositSw", "P8", "P16", "P32",
           "array", "from_bits", "zeros", "nar", "quire", "fma", "axpy", "set_backend", "get_backend", "using", "stats", "clear_stats",
           "__version__"]
