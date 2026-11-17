"""Backends, flag counters, conversions: the housekeeping around every computation.

    python3 examples/03_backends_and_stats.py [--sim | --hw [APP]]

- Backends: pn.set_backend(engine) / pn.set_backend(None), and `with pn.using(...)` for one block.
  pn.board.load(...) registers every engine of the application as the backend of its format.
- Counters: every backend counts rounded results (inexact), results clamped to maxpos / minpos (sat)
  and NaR results (nar); engines also count elements computed in software (fallback).
- Same bits everywhere: PositSw is the reference of the hardware tests, so a result can be checked by
  running it again in software.
- Formats do not mix silently: convert with pn.array(x, config).
"""

import numpy as np

import _common
import positnp as pn


def compute(x):
    """Anything: a few element-wise operations, a reduction and a matmul."""
    y = (x * 3 - 1) / (x + 2)
    A = y[:64].reshape(8, 8)
    return y, np.sum(y), A @ A


def main(argv=None):
    args = _common.start(__doc__, argv)
    rnd = np.random.default_rng(1)
    data = rnd.standard_normal(1000)

    _common.section("flag counters (pn.stats)")
    pn.clear_stats(pn.P16)
    x = pn.array(data, pn.P16)
    _ = x * 3.3                                   # rounded: inexact
    _ = pn.array([1e15, 2e15], pn.P16) * 1e3      # saturates at maxpos: sat
    _ = pn.array([1.0, 0.0], pn.P16) / 0.0        # NaR
    s = pn.stats(pn.P16)
    print(f"  {s}")
    print("  inexact: result rounded; sat: clamped to +-maxpos / +-minpos; nar: NaR produced")
    pn.clear_stats(pn.P16)

    _common.section("same bits on every backend")
    x = pn.array(data)
    with pn.using(None):                          # software reference
        sw = compute(x)
    hw = compute(x) if args.engines else sw
    for name, a, b in zip(("element-wise", "sum (quire)", "matmul (quire)"), hw, sw):
        print(f"  {name:<16} identical bits: {a.equal_bits(b)}")
    print(f"  backend for posit32: {type(pn.get_backend(pn.P32)).__name__}"
          + ("" if args.engines else " - run with --sim or --hw to compare against an engine"))

    _common.section("conversions")
    x32 = pn.array([np.pi, 1e-3, 1e5])
    x16 = pn.array(x32, pn.P16)                   # posit32 -> posit16: rounded once (through the exact value)
    x8 = pn.array(x32, pn.P8)
    f32 = np.float32([np.pi, 1e-3, 1e5])
    print(f"  posit32: {x32}\n  posit16: {x16}\n  posit8:  {x8}")
    print(f"  float32 -> posit32 (F2P in the PL on the KV260): {pn.array(f32)}")
    print(f"  posit32 -> float32 (P2F): {x32.to_float32()},  -> float64: {x32.to_float()}")
    print(f"  raw bits in / out: pn.from_bits({x16.bits.tolist()}, pn.P16) = {pn.from_bits(x16.bits, pn.P16)}")
    try:
        _ = x32 + x16
    except TypeError as e:
        print(f"  x32 + x16 -> TypeError: {e}")

    _common.section("backends by hand")
    print("  pn.set_backend(engine)      # one format; pn.set_backend([e32, e16, e8]) several")
    print("  pn.set_backend(None)        # everything in software")
    print("  with pn.using(engine): ...  # for one block, the previous set restored after")
    print(f"  now: {[(str(c), type(b).__name__) for c, b in pn.active_backends().items()] or 'all PositSw'}")


if __name__ == "__main__":
    main()
