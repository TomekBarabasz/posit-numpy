"""Hello, posit: check the installation and compute something.

    pip install -e sw            # once, from the repository root (or run from sw/ without installing)
    python3 examples/00_hello_posit.py

positnp has one array class (PositArray) and interchangeable backends: PositSw computes in
software with NumPy (any PC), PositVecHw drives the posit engines in the KV260 programmable logic.
Both give the same bits, so everything in these examples works without a board.
"""

import numpy as np

import _common
import positnp as pn


def main(argv=None):
    _common.start(__doc__, argv)

    print(f"positnp {pn.__version__}, NumPy {np.__version__}")
    print(f"default format: {pn.P32}; also {pn.P16}, {pn.P8}, or any pn.PositConfig(n, es) with n <= 32")
    print(f"backend for posit32: {pn.get_backend(pn.P32)!r}")

    x = pn.array([0.1, 0.5, 1.0, 3.0])         # float64 -> posit32, rounded to nearest (ties to even)
    y = x * x + 1                              # arithmetic like NumPy, every operation rounded once
    print(f"\nx         = {x}")
    print(f"x * x + 1 = {y}")
    print(f"as float  = {y.to_float()}")       # float64 copy, NaR -> nan
    print(f"raw bits  = {[hex(b) for b in y.bits.tolist()]}")
    print(f"sum (exact in the quire, rounded once) = {float(np.sum(y))}")
    print(f"flag counters of the backend: {pn.stats()}")


if __name__ == "__main__":
    main()
