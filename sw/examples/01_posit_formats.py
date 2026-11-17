"""Posit formats: what is inside a posit, range, precision, special values.

    python3 examples/01_posit_formats.py

A posit<n, es> (Posit Standard 2022: es = 2 for every n) is   sign | regime | exponent | fraction.
The regime is a run of equal bits whose length encodes k, so the value is

    (-1)**sign * 2**(4k + e) * (1 + f)        (es = 2: scale = 4k + e)

Numbers near 1 have a short regime and many fraction bits; very large or very small numbers spend
bits on the regime and have fewer ("tapered precision"). There is one zero, no -0, no infinity and
one exception value NaR (Not a Real) - and no subnormals, no overflow to inf, no underflow to 0.
"""

import numpy as np

import _common
import positnp as pn


def split(c: pn.PositConfig, bits: int):
    """(sign, regime, exponent, fraction, k) bit strings of a posit other than 0 and NaR."""
    sign = bits >> (c.n - 1) & 1
    v = (-bits) & c.mask if sign else bits             # negative posits: fields of the two's complement
    body = format(v, f"0{c.n}b")[1:]
    run = len(body) - len(body.lstrip(body[0]))
    reg = body[:min(run + 1, len(body))]
    rest = body[len(reg):]
    k = run - 1 if body[0] == "1" else -run
    return str(sign), reg, rest[:c.es], rest[c.es:], k


def fields(c: pn.PositConfig, bits: int) -> str:
    """The bit fields of a posit as a string: sign | regime | exponent | fraction."""
    if bits == 0:
        return "0" * c.n + "   (zero)"
    if bits == c.nar:
        return "1" + "0" * (c.n - 1) + "   (NaR)"
    sign, reg, exp, frac, k = split(c, bits)
    return f"{sign} | {reg} | {exp or '-'} | {frac or '-'}   (k = {k:+d}, {len(frac)} fraction bits)"


def main(argv=None):
    _common.start(__doc__, argv, quiet=True)

    _common.section("formats")
    rows = []
    for c in (pn.P8, pn.P16, pn.P32):
        rows.append((str(c), f"2**{c.max_scale} = {2.0 ** c.max_scale:.3g}", f"2**-{c.max_scale} = {2.0 ** -c.max_scale:.3g}",
                     c.n - 3 - c.es, c.quire_width()))
    _common.table(rows, ["format", "maxpos", "minpos", "fraction bits at 1", "quire bits"])
    print("  (float16: max 65504, min normal 6.1e-05, 10 fraction bits; float32: 23 fraction bits, 1e-38 .. 3.4e38)")

    _common.section("anatomy of posit<16,2>")
    c = pn.P16
    values = [1.0, 0.75, 3.5, 1000.0, 1e-5, -2.0, np.inf]
    p = pn.array(values, c)
    for v, b, back in zip(values, p.bits.tolist(), p.to_float()):
        print(f"  {v:>8g} -> 0x{b:04x}  {fields(c, b):<58}  = {back:.6g}")
    print(f"  maxpos -> 0x{c.maxpos:04x}  {fields(c, c.maxpos)}")
    print(f"  minpos -> 0x0001  {fields(c, 1)}")

    _common.section("tapered precision: fraction bits by magnitude (posit16 vs float16)")
    rows = []
    for s in (-24, -16, -12, -8, -4, 0, 4, 8, 12, 16, 24):
        bits = int(pn.array([2.0 ** s], c).bits[0])
        f16 = 10 if -14 <= s <= 15 else (max(0, 10 - (-14 - s)) if s < -14 else "inf")
        rows.append((f"2**{s}", len(split(c, bits)[3]), f16))
    _common.table(rows, ["value", "posit16", "float16"])
    print("  posit16 has more precision than float16 in [1/16, 16), the same up to [1/256, 256), less beyond -")
    print("  but its range is 2**-56 .. 2**56 instead of 6e-8 .. 65504 (examples/30_tapered_precision.py)")

    _common.section("special values")
    x = pn.array([1.0, -1.0, 0.0])
    print(f"  1 / 0           = {pn.array(1.0) / 0.0}   (NaR, not inf)")
    print(f"  maxpos * 2      = {pn.array(2.0 ** 120) * 2}   (saturates at maxpos, no inf)")
    print(f"  minpos / 2      = {pn.array(2.0 ** -120) / 2}   (stays at minpos, no underflow to 0)")
    print(f"  nan, inf        -> {pn.array([np.nan, np.inf, -np.inf])}")
    print(f"  NaR == NaR      = {bool(pn.nar(()) == pn.nar(()))}   (unlike NaN; NaR sorts before every real)")
    print(f"  -0.0            = {pn.array(-0.0).bits} (one zero: 0x0)")
    print(f"  np.sort         = {np.sort(pn.array([2.0, np.nan, -1.0]))}")
    print(f"  isnan(x / 0)    = {np.isnan(x / 0.0)}")

    _common.section("other formats (software)")
    for c in (pn.PositConfig(12, 2), pn.PositConfig(16, 1), pn.PositConfig(24, 2)):
        v = pn.array([np.pi], c)
        print(f"  {str(c):<12} pi = {float(v):.10g}  (bits 0x{int(v.bits[0]):x}), maxpos = 2**{c.max_scale}")
    print("  The KV260 engines compute the formats of their bitstream (posit32 / 16 / 8, es = 2); any other")
    print("  PositConfig runs in PositSw.")


if __name__ == "__main__":
    main()
