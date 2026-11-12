"""positnp: NumPy-style posit arrays, computed in KV260 PL or in software."""

from . import ref, regs
from .array import PositArray, get_backend, set_backend
from .backend_sw import PositSw
from .ref import Flags, PositConfig

__version__ = "0.2.0"

__all__ = ["ref", "regs", "Flags", "PositConfig", "PositArray", "PositSw", "set_backend", "get_backend",
           "__version__"]
