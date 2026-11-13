"""Posit arithmetic on whole NumPy arrays: decode, round/encode with flags, add/sub/mul/div.

The same algorithms as the hardware (posit-core): decoded operands (sign, scale, significand),
an exact result up to a sticky bit, one rounding in the encoder. Everything is int64 array
arithmetic, so it is fast (millions of elements per second) and bit for bit equal to ref.py
and to the FPGA; tests/test_vecops.py pins it to the shared SHA-256 digests in
tests/ref_digests.txt, the same ones PositRef.scala and ref.py match.

Limits: n <= 32 (every intermediate fits in int64), es <= 11 (float64 conversion).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .ref import PositConfig

ZERO_SCALE = -(1 << 20)   # scale given to zero: far below any real scale, so it aligns to nothing


@dataclass
class Decoded:
    """A posit array as fields: value = (-1)**sign * sig * 2**(scale - F), sig in [2**F, 2**(F+1))."""
    nar: np.ndarray
    zero: np.ndarray
    sign: np.ndarray    # bool
    scale: np.ndarray   # int64
    sig: np.ndarray     # int64, F + 1 bits with the hidden one (0 for zero and NaR)


@dataclass
class Rounded:
    bits: np.ndarray    # int64, raw posit bits
    nar: np.ndarray
    sat: np.ndarray
    inexact: np.ndarray

    @property
    def flags(self) -> np.ndarray:
        """Flag bits as in hardware and ref.Flags.bits: nar = 1, sat = 2, inexact = 4."""
        return self.nar.astype(np.int64) | (self.sat.astype(np.int64) << 1) | (self.inexact.astype(np.int64) << 2)


def _check(c: PositConfig) -> None:
    if c.n > 32:
        raise ValueError(f"{c}: vecops supports n <= 32")


def bit_length(x: np.ndarray) -> np.ndarray:
    """int.bit_length for non-negative int64 below 2**53 (exact through frexp)."""
    return np.frexp(x.astype(np.float64))[1].astype(np.int64)


def frac_bits(c: PositConfig) -> int:
    return c.n - 3 - c.es


def decode(c: PositConfig, bits) -> Decoded:
    _check(c)
    n, es, F = c.n, c.es, frac_bits(c)
    body_mask = (1 << (n - 1)) - 1
    p = np.asarray(bits).astype(np.int64) & c.mask
    nar = p == c.nar
    zero = p == 0
    sign = (p >> (n - 1)) & 1 == 1
    v = np.where(sign, (-p) & c.mask, p)
    body = v & body_mask
    r0 = (body >> (n - 2)) & 1 == 1
    x = np.where(r0, ~body & body_mask, body)          # leading zeros of x = regime run
    run = (n - 1) - bit_length(x)
    k = np.where(r0, run - 1, -run)
    remlen = np.maximum(n - 2 - run, 0)                  # bits after the regime terminator
    rest = body & ((np.int64(1) << remlen) - 1)
    has_frac = remlen >= es
    flen = np.where(has_frac, remlen - es, 0)
    e = np.where(has_frac, rest >> np.where(has_frac, remlen - es, 0), rest << np.where(has_frac, 0, es - remlen))
    f = rest & ((np.int64(1) << flen) - 1)
    scale = (k << es) + e
    sig = (np.int64(1) << F) | (f << (F - flen))
    special = nar | zero
    return Decoded(nar, zero, sign & ~special,
                   np.where(zero, ZERO_SCALE, np.where(nar, 0, scale)).astype(np.int64),
                   np.where(special, 0, sig).astype(np.int64))


def encode(c: PositConfig, sign, scale, sig, S: int, sticky, nar, zero) -> Rounded:
    """Round (-1)**sign * sig * 2**(scale - S) (+ something below, if sticky) to posit<n,es>.

    sig has its leading one at bit S (or is ignored where nar / zero). Round to nearest, ties to
    even on the encoding, never to zero or NaR; flags as PositFlags in hardware.
    """
    n, es = c.n, c.es
    W = es + S + 2                                        # 2 zero bits of slack: guard always exists
    if W > 62:
        raise ValueError(f"{c}: significand of {S + 1} bits too wide for int64 rounding")
    reg_max = n - 2
    sign = np.asarray(sign, dtype=bool)
    nar = np.asarray(nar, dtype=bool)
    zero = np.asarray(zero, dtype=bool)
    sticky = np.asarray(sticky, dtype=bool)
    scale = np.asarray(scale, dtype=np.int64)
    sig = np.asarray(sig, dtype=np.int64)
    special = nar | zero

    k = scale >> es                                       # floor division
    ex = scale - (k << es)
    frac = np.where(special, 0, sig - (np.int64(1) << S))
    tail = ((ex << S) | frac) << 2                        # exponent ++ fraction ++ 00, W bits
    above = (k >= reg_max) & ~special                     # at or beyond maxpos
    below = (k < -reg_max) & ~special                     # below minpos
    normal = ~(above | below | special)

    kk = np.where(normal, k, 0)                           # keep shift amounts in range off the normal path
    rlen = np.where(kk >= 0, kk + 2, 1 - kk)
    avail = (n - 1) - rlen                                # 0 .. n-3 bits of tail kept
    regbits = np.where(kk >= 0, ((np.int64(1) << (kk + 1)) - 1) << 1, 1)
    kept = tail >> (W - avail)
    guard = (tail >> (W - avail - 1)) & 1 == 1
    lost = (tail & ((np.int64(1) << (W - avail - 1)) - 1)) != 0
    stk = lost | sticky
    body = (regbits << avail) | kept
    rounded = body + (guard & ((body & 1 == 1) | stk))
    mag = np.minimum(np.maximum(rounded, 1), c.maxpos)
    mag = np.where(above, c.maxpos, np.where(below, 1, mag))
    out = np.where(sign, (-mag) & c.mask, mag)
    out = np.where(zero, 0, np.where(nar, c.nar, out)).astype(np.int64)

    exact_max = (k == reg_max) & (ex == 0) & (frac == 0) & ~sticky
    sat = ~special & ((above & ~exact_max) | below)
    inexact = ~special & np.where(above | below, ~exact_max, guard | stk)
    return Rounded(out, nar.copy(), sat, inexact)


def roundtrip(c: PositConfig, d: Decoded) -> Rounded:
    return encode(c, d.sign, d.scale, d.sig, frac_bits(c), False, d.nar, d.zero)


# ---- operations ---------------------------------------------------------------------------

GUARD = 3


def _add(c: PositConfig, a: Decoded, b: Decoded, negate_b: bool) -> Rounded:
    F, G = frac_bits(c), GUARD
    sign_b = b.sign ^ negate_b
    swap = (b.scale > a.scale) | ((b.scale == a.scale) & (b.sig > a.sig))
    big_scale = np.where(swap, b.scale, a.scale)
    big_sig = np.where(swap, b.sig, a.sig)
    big_sign = np.where(swap, sign_b, a.sign)
    small_scale = np.where(swap, a.scale, b.scale)
    small_sig = np.where(swap, a.sig, b.sig)
    small_sign = np.where(swap, a.sign, sign_b)

    w = F + 1 + G
    d = big_scale - small_scale
    far = d >= w
    ds = np.where(far, 0, d)
    X = big_sig << G
    Yfull = small_sig << G
    lostbits = Yfull & ((np.int64(1) << ds) - 1)
    Y = np.where(far, 0, Yfull >> ds)
    st = np.where(far, small_sig != 0, lostbits != 0)
    Y = Y | st.astype(np.int64)                             # sticky folded into the LSB
    s = np.where(big_sign != small_sign, X - Y, X + Y)      # >= 0: |big| >= |small|

    nar = a.nar | b.nar
    zero = (s == 0) & ~nar
    lp = bit_length(s) - 1
    S = F + 1 + G                                           # highest possible leading one (carry)
    shift = np.where(zero | nar, 0, S - lp)
    sig = s << shift
    scale = big_scale + lp - F - G
    return encode(c, big_sign, scale, sig, S, False, nar, zero)


def add(c: PositConfig, a: Decoded, b: Decoded) -> Rounded:
    return _add(c, a, b, False)


def sub(c: PositConfig, a: Decoded, b: Decoded) -> Rounded:
    return _add(c, a, b, True)


def mul(c: PositConfig, a: Decoded, b: Decoded) -> Rounded:
    F = frac_bits(c)
    prod = a.sig * b.sig                                    # 2F + 2 bits at most, exact
    S = 2 * F + 1
    top = (prod >> S) & 1 == 1
    sig = np.where(top, prod, prod << 1)
    scale = a.scale + b.scale + top.astype(np.int64)
    nar = a.nar | b.nar
    zero = (a.zero | b.zero) & ~nar
    return encode(c, a.sign ^ b.sign, scale, sig, S, False, nar, zero)


def div(c: PositConfig, a: Decoded, b: Decoded) -> Rounded:
    F = frac_bits(c)
    Q = F + 4
    nar = a.nar | b.nar | b.zero
    zero = a.zero & ~nar
    den = np.where(b.sig == 0, 1, b.sig)
    q, r = np.divmod(a.sig << Q, den)                       # q in [2**(Q-1), 2**(Q+1))
    hi = q >= (np.int64(1) << Q)
    sig = np.where(hi, q >> 1, q)                           # leading one at Q - 1
    sticky = (r != 0) | (hi & (q & 1 == 1))
    scale = a.scale - b.scale - (~hi).astype(np.int64)
    return encode(c, a.sign ^ b.sign, scale, sig, Q - 1, sticky, nar, zero)


OPS = {"add": add, "sub": sub, "mul": mul, "div": div}


def op(c: PositConfig, name: str, a_bits, b_bits) -> Rounded:
    """Element-wise operation on raw posit bits (broadcast like NumPy); returns bits and flags."""
    _check(c)
    a, b = np.broadcast_arrays(np.asarray(a_bits), np.asarray(b_bits))
    try:
        fn = OPS[name]
    except KeyError:
        raise ValueError(f"unknown operation {name!r}, expected one of {tuple(OPS)}") from None
    return fn(c, decode(c, a), decode(c, b))


# ---- conversions ----------------------------------------------------------------------------

def from_float(c: PositConfig, x) -> Rounded:
    """float64 (or anything castable) -> posit bits, exact rounding; nan, inf -> NaR."""
    _check(c)
    x = np.asarray(x, dtype=np.float64)
    nar = ~np.isfinite(x)
    zero = x == 0
    m, e = np.frexp(np.where(nar, 1.0, np.abs(x)))        # |x| = m * 2**e, m in [0.5, 1)
    sig = (m * 2.0 ** 53).astype(np.int64)                  # 53 bits, exact
    return encode(c, np.signbit(x) & ~zero, e.astype(np.int64) - 1, sig, 52, False, nar, zero)


def to_float(c: PositConfig, bits) -> np.ndarray:
    """posit bits -> float64 (exact for n <= 32); NaR -> nan."""
    d = decode(c, bits)
    v = np.ldexp(d.sig.astype(np.float64), np.where(d.zero | d.nar, 0, d.scale - frac_bits(c)).astype(np.int32))
    v = np.where(d.sign, -v, v)
    return np.where(d.nar, np.nan, np.where(d.zero, 0.0, v))


# ---- level 0: on the integers, no decoding --------------------------------------------------

def to_signed(c: PositConfig, bits) -> np.ndarray:
    """Posit bits as signed integers: they order like the posit values, NaR is the minimum."""
    p = np.asarray(bits).astype(np.int64) & c.mask
    return np.where(p >= c.nar, p - (np.int64(1) << c.n), p)


def neg(c: PositConfig, bits) -> np.ndarray:
    return (-np.asarray(bits).astype(np.int64)) & c.mask


def absolute(c: PositConfig, bits) -> np.ndarray:
    s = to_signed(c, bits)
    return np.abs(s) & c.mask          # |NaR| = NaR (the minimum maps to itself)


# ---- quire (stage 4) ---------------------------------------------------------------------------

LIMB = 16


def quire(c: PositConfig, mode: str, a, b=None):
    """Exact sum of terms (sum: a, dot: a*b, sumsq: a*a) as a Python int in quire units; (q, nar).

    Vectorised and exact: every term (a 2F+2 bit product at a bit position in the quire) is cut
    into 16-bit limbs, the limbs of all terms are summed per position with np.bincount (float64
    sums of integers below 2**53 are exact for up to 2**37 terms), and the positions are joined
    with Python integers at the end.
    """
    _check(c)
    from .ref import QUIRE_MODES
    if mode not in QUIRE_MODES:
        raise ValueError(f"quire mode {mode!r}, expected one of {QUIRE_MODES}")
    da = decode(c, np.asarray(a).ravel())
    if mode == "sum":
        F = frac_bits(c)
        db = Decoded(np.zeros_like(da.nar), np.zeros_like(da.zero), np.zeros_like(da.sign),
                     np.zeros_like(da.scale), np.full_like(da.sig, 1 << F))
    elif mode == "sumsq":
        db = da
    else:
        db = decode(c, np.asarray(b).ravel())
    nar = bool((da.nar | db.nar).any())
    if nar:
        return 0, True
    F = frac_bits(c)
    live = ~(da.zero | db.zero)
    prod = (da.sig * db.sig)[live]                                  # 2F + 2 bits, exact
    pos = (da.scale + db.scale)[live] + c.quire_frac - 2 * F        # bit position of the product's LSB
    sign = np.where((da.sign ^ db.sign)[live], -1.0, 1.0)
    low = pos < 0                                                   # trailing zeros below the quire LSB
    prod = np.where(low, prod >> np.where(low, -pos, 0), prod)
    pos = np.where(low, 0, pos)
    base, off = pos // LIMB, pos % LIMB
    nlimbs = c.quire_width(64) // LIMB + 8
    acc = np.zeros(nlimbs)
    mask = (1 << LIMB) - 1
    for t in range((2 * F + 2 + LIMB - 1) // LIMB):
        piece = (prod >> (LIMB * t)) & mask                         # 16-bit slice of the product
        shifted = piece << off                                      # < 2**31
        idx = base + t
        acc += np.bincount(idx, weights=sign * (shifted & mask), minlength=nlimbs)[:nlimbs]
        acc += np.bincount(idx + 1, weights=sign * (shifted >> LIMB), minlength=nlimbs)[:nlimbs]
    q = 0
    for i in range(nlimbs - 1, -1, -1):
        q = (q << LIMB) + int(acc[i])
    return q, False


def quire_round(c: PositConfig, q: int, nar: bool) -> Rounded:
    from . import ref
    bits, f = ref.quire_round(c, q, nar)
    return Rounded(np.array([bits]), np.array([f.nar]), np.array([f.sat]), np.array([f.inexact]))

