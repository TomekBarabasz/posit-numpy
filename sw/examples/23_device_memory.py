"""Device memory: keep arrays in the engine's buffer and chain operations without copies.

    sudo python3 examples/23_device_memory.py --hw [APP]    # KV260, a *_mem application
    python3 examples/23_device_memory.py --sim              # the same code on simulated engines

Engines with their own AXI4 master (stage 6, *_mem applications) read and write a DMA buffer
(u-dma-buf) themselves. x.to_device() copies an array there once; an operation whose inputs are all on
the device runs without any copy and leaves its result there too (y.on_device). y.to_host() copies
back. Without such an engine (PositSw, older *_vec bitstreams) to_device() returns the array unchanged,
so the same code runs everywhere.
"""

import time

import numpy as np

import _common
import positnp as pn


def pipeline(x, y):
    """A few element-wise steps, a reduction and a matmul - typical "keep it on the device" work."""
    z = pn.fma(x, y, 1.0)              # x * y + 1
    z = z * z - x
    return z, np.sum(z)


def main(argv=None):
    args = _common.start(__doc__, argv)
    rnd = np.random.default_rng(2)
    n = 1 << 20 if args.hw is not None else 1 << 12
    x = pn.array(rnd.standard_normal(n), pn.P16)
    y = pn.array(rnd.standard_normal(n), pn.P16)

    _common.section("to_device / on_device / to_host")
    xd, yd = x.to_device(), y.to_device()          # or pn.to_device(x)
    print(f"  x.on_device = {x.on_device}, xd.on_device = {xd.on_device}")
    zd, s = pipeline(xd, yd)
    print(f"  result on the device: {zd.on_device};  zd.to_host().on_device = {zd.to_host().on_device}")
    z, s2 = pipeline(x, y)
    print(f"  same bits as the host path: {zd.equal_bits(z) and s.equal_bits(s2)}")
    if not args.engines:
        print("  (PositSw: there is no device memory, to_device() is a no-op - try --sim or --hw)")

    _common.section("time per call (host arrays vs device arrays)")
    if args.hw is None:
        print("  only meaningful on the board (--hw): there, device arrays skip the copies in and out of the")
        print("  DMA buffer - posit16 add runs at ~300 M elements/s from device memory (hw/top-modules/vec/README.md)")
        return
    for label, a, b in (("host", x, y), ("device", xd, yd)):
        t0 = time.perf_counter()
        for _ in range(5):
            pipeline(a, b)
        print(f"  {label:<7} {(time.perf_counter() - t0) / 5 * 1e3:8.2f} ms per pipeline of {n} elements")
    hw = args.engines.get(16) or next(iter(args.engines.values()))
    for dev in (False, True):
        print(f"  {hw.config} hw.time_array('add', device={dev}): {hw.time_array('add', 1 << 20, device=dev) / 1e6:.0f} M elements/s")

if __name__ == "__main__":
    main()
