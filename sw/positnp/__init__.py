"""positnp: NumPy-style posit arrays, computed in KV260 PL or in software.

    import numpy as np, positnp as pn
    x = pn.array(np.linspace(-2, 2, 5))       # posit<32,2>; pn.array(x, pn.P16) for posit<16,2>
    (x * x - 1).to_float()
"""

from . import funcs, ref, regs, vecops
from . import fft, linalg
from .array import (P8, P16, P32, PositArray, active_backends, array, axpy, clear_stats, fma, from_bits, get_backend,
                    convolve, correlate, func, load, matmul, nar, save, sigmoid, quire, set_backend, stats, to_device, using, zeros)
from . import board
from .npapi import arange, empty, eye, full, identity, linspace, ones
from .backend_sw import PositSw
from .ref import Flags, PositConfig

__version__ = "0.17.0"

__all__ = ["ref", "regs", "vecops", "Flags", "PositConfig", "PositArray", "PositSw", "P8", "P16", "P32",
           "array", "from_bits", "zeros", "ones", "full", "empty", "eye", "identity", "arange", "linspace", "nar", "quire", "fma", "axpy", "matmul", "convolve", "correlate", "func", "sigmoid", "funcs", "fft", "linalg", "to_device", "save", "load", "set_backend", "get_backend", "active_backends", "board", "using", "stats", "clear_stats",
           "__version__"]
