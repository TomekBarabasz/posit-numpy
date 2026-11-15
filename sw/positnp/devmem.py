"""Device memory: arrays living in the u-dma-buf buffer, read and written by the engine without copies.

Part of the buffer (above the staging area of the double-buffered copy path) is a heap. An array
allocated there is an ordinary NumPy view of the buffer mapping; the engine gets its physical address,
so an operation whose inputs are all on the device runs as one command with no copy in or out, and its
result is allocated on the device too - chains like x * x - x / 3 + 1 never leave device memory.

    hw = PositVecHw.open(32)
    xd = hw.to_device(x_bits)          # one copy into device memory
    yd = hw.array_op("mul", xd, xd)    # no copies: engine reads xd, writes yd (both device)
    hw.heap.stats()                    # used / free / largest free block

Blocks are 4 KB aligned and sized, so two arrays never share a cache line: cache maintenance of
one (clean before the engine reads, invalidate after it writes) cannot touch another. A block is
freed when the last NumPy view of it goes away (weakref.finalize on the base view).
"""

from __future__ import annotations

import threading
import weakref
from typing import Dict, List, Optional, Tuple

import numpy as np


class DeviceHeap:
    """First-fit allocator over [start, start + size) of a u-dma-buf (offsets relative to the buffer)."""

    ALIGN = 4096

    def __init__(self, buf, start: int, size: int):
        self.buf = buf
        self.start = start
        self.size = size // self.ALIGN * self.ALIGN
        self._free: List[Tuple[int, int]] = [(start, self.size)] if self.size else []   # (offset, size), sorted
        self._used: Dict[int, int] = {}
        self._lock = threading.Lock()
        a = np.frombuffer(buf._mm if hasattr(buf, "_mm") else buf.memory, np.uint8, count=1)
        self.base_addr = a.__array_interface__["data"][0]     # virtual address of the buffer mapping
        del a

    # ---- allocation -------------------------------------------------------------------------

    def alloc(self, nbytes: int) -> Optional[int]:
        """Offset of a free block of at least `nbytes` (rounded to 4 KB), or None when there is none."""
        need = max(self.ALIGN, -(-nbytes // self.ALIGN) * self.ALIGN)
        with self._lock:
            for i, (off, sz) in enumerate(self._free):
                if sz >= need:
                    if sz == need:
                        del self._free[i]
                    else:
                        self._free[i] = (off + need, sz - need)
                    self._used[off] = need
                    return off
        return None

    def free(self, off: int) -> None:
        with self._lock:
            sz = self._used.pop(off, None)
            if sz is None:
                return
            self._free.append((off, sz))
            self._free.sort()
            merged: List[Tuple[int, int]] = []
            for o, s in self._free:                     # coalesce neighbours
                if merged and merged[-1][0] + merged[-1][1] == o:
                    merged[-1] = (merged[-1][0], merged[-1][1] + s)
                else:
                    merged.append((o, s))
            self._free = merged

    def empty(self, count: int, dtype) -> Optional[np.ndarray]:
        """A 1-D array of `count` elements in device memory, or None when the heap is full."""
        dtype = np.dtype(dtype)
        off = self.alloc(max(1, count) * dtype.itemsize)
        if off is None:
            return None
        arr = np.frombuffer(self.buf._mm if hasattr(self.buf, "_mm") else self.buf.memory, dtype, count, off)
        weakref.finalize(arr, self.free, off)
        return arr

    # ---- lookup -------------------------------------------------------------------------------

    def offset_of(self, x: np.ndarray) -> Optional[int]:
        """Buffer offset of `x` when it is a contiguous view into the heap aligned to 16 B, else None."""
        if not isinstance(x, np.ndarray) or x.size == 0 or not x.flags.c_contiguous:
            return None
        addr = x.__array_interface__["data"][0] - self.base_addr
        if addr < self.start or addr + x.nbytes > self.start + self.size or addr % 16:
            return None
        return addr

    def stats(self) -> Dict[str, int]:
        with self._lock:
            free = sum(s for _, s in self._free)
            return {"size": self.size, "used": self.size - free, "free": free,
                    "largest_free": max((s for _, s in self._free), default=0), "blocks": len(self._used)}

    def __repr__(self) -> str:
        s = self.stats()
        return f"DeviceHeap({s['used'] >> 20}/{s['size'] >> 20} MB used, {s['blocks']} blocks)"
