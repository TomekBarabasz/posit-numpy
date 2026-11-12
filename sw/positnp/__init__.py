"""positnp: NumPy-style posit arrays, computed in KV260 PL or in software."""

from . import ref, regs
from .ref import Flags, PositConfig

__version__ = "0.1.0"

__all__ = ["ref", "regs", "Flags", "PositConfig", "__version__"]
