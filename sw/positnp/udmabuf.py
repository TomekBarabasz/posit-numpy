"""Physically contiguous DMA buffer from the u-dma-buf kernel module (no PYNQ allocate).

The overlay's .dtsi has a node `compatible = "ikwzm,u-dma-buf"` named udmabuf-posit;
with the module loaded (sw/scripts/build_udmabuf.sh) the kernel creates
/dev/udmabuf-posit and /sys/class/u-dma-buf/udmabuf-posit/{phys_addr,size,...}.

The mapping is cached. The HP0 port is not coherent with the CPU caches, so every DMA
transfer is bracketed with explicit cache maintenance through sysfs:
    sync_for_device(off, size)   after the CPU writes, before the device reads/writes
    sync_for_cpu(off, size)      after the device writes, before the CPU reads
(An uncached O_SYNC mapping would avoid this, but on arm64 it is Device memory, where
the unaligned accesses and DC ZVA inside memcpy/numpy fault with SIGBUS.)
"""

from __future__ import annotations

import mmap
import os
from pathlib import Path
from typing import Optional

import numpy as np

SYS_CLASS = Path("/sys/class/u-dma-buf")
DEFAULT_NAME = "udmabuf-posit"


class UDmaBuf:
    def __init__(self, name: str = DEFAULT_NAME):
        self.name = name
        self.sys = SYS_CLASS / name
        if not self.sys.exists():
            raise FileNotFoundError(f"{self.sys} missing: u-dma-buf module not loaded or overlay without "
                                    f"the {name} node (sw/scripts/build_udmabuf.sh, install_overlay.sh)")
        self.phys_addr = int(self._get("phys_addr"), 0)
        self.size = int(self._get("size"), 0)
        fd = os.open(f"/dev/{name}", os.O_RDWR)
        try:
            self._mm = mmap.mmap(fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            os.close(fd)
        # sysfs attributes of the sync: opened once, written with pwrite; unchanged values are not
        # rewritten (each sync was 4 x open / write / close, about 1 ms on the KV260)
        self._fds = {}
        self._last = {}

    def _get(self, attr: str) -> str:
        return (self.sys / attr).read_text().strip()

    def _set(self, attr: str, value, cache: bool = True) -> None:
        if cache and self._last.get(attr) == value:
            return
        fd = self._fds.get(attr)
        if fd is None:
            fd = self._fds[attr] = os.open(self.sys / attr, os.O_WRONLY)
        os.pwrite(fd, str(value).encode(), 0)
        self._last[attr] = value

    def _sync(self, which: str, offset: int, size: int, direction: int) -> None:
        if offset < 0 or size <= 0 or offset + size > self.size:
            raise ValueError(f"sync {offset}+{size} outside the buffer ({self.size} B)")
        self._set("sync_offset", offset)
        self._set("sync_size", size)
        self._set("sync_direction", direction)  # 0 bidirectional, 1 to device, 2 from device
        self._set(which, 1, cache=False)

    def sync_for_device(self, offset: int, size: int, direction: int = 0) -> None:
        self._sync("sync_for_device", offset, size, direction)

    def sync_for_cpu(self, offset: int, size: int, direction: int = 0) -> None:
        self._sync("sync_for_cpu", offset, size, direction)

    def view(self, dtype, offset: int, count: int) -> np.ndarray:
        """numpy view of `count` elements at byte `offset` (no copy)."""
        return np.frombuffer(self._mm, dtype=dtype, count=count, offset=offset)

    def close(self) -> None:
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()
        self._mm.close()

    def time_sync(self, calls: int = 200, size: int = 1 << 16) -> float:
        """Seconds per sync_for_device + sync_for_cpu pair on `size` bytes (measures the fixed cost)."""
        import time
        t0 = time.perf_counter()
        for i in range(calls):
            off = (i % 2) * size
            self.sync_for_device(off, size, 1)
            self.sync_for_cpu(off, size, 2)
        return (time.perf_counter() - t0) / calls


class FakeUDmaBuf:
    """UDmaBuf on a bytearray at a made-up physical address, for tests without the board.

    Records the sync calls so a test can check the cache maintenance protocol.
    """

    def __init__(self, size: int = 1 << 20, phys_addr: int = 0x7000_0000, memory: Optional[bytearray] = None):
        self.name = "fake"
        self.size = size
        self.phys_addr = phys_addr
        self.memory = memory if memory is not None else bytearray(size)
        self.syncs = []

    def sync_for_device(self, offset: int, size: int, direction: int = 0) -> None:
        self.syncs.append(("device", offset, size))

    def sync_for_cpu(self, offset: int, size: int, direction: int = 0) -> None:
        self.syncs.append(("cpu", offset, size))

    def view(self, dtype, offset: int, count: int) -> np.ndarray:
        return np.frombuffer(self.memory, dtype=dtype, count=count, offset=offset)

    def close(self) -> None:
        pass
