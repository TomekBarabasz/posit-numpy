"""FFT on posits: pn.fft.fft / ifft / rfft (and np.fft.* on PositArray).

    python3 examples/22_fft.py [--sim | --hw [APP]]

Results are PositComplex (.real, .imag: PositArray; .to_complex() for NumPy). Three ways to compute:
- method="blocks" (default without an FFT core): DFT blocks of up to 64 points, each output exact in the
  quire (matmul), twiddle factors applied with FMA - few roundings per output;
- method="exact" / exact=True: one direct DFT, every output rounded once from the exact sum (N <= 2048);
- method="core": the radix-4 FFT core of 5.5 bitstreams (N = 4^S), modelled bit for bit in software;
- method="auto": the core where the bitstream has one and N = 4^S, otherwise blocks.
"""

import time

import numpy as np

import _common
import positnp as pn


def main(argv=None):
    args = _common.start(__doc__, argv)
    rnd = np.random.default_rng(5)

    _common.section("the API")
    x = pn.array(np.cos(2 * np.pi * 3 * np.arange(16) / 16))
    X = pn.fft.fft(x)
    print(f"  X = pn.fft.fft(x): {X}, |X| = {np.round(np.abs(X.to_complex()), 6)}")
    re, im = X                                           # unpacks into two PositArray
    print(f"  re, im = X -> {type(re).__name__}, {type(im).__name__}")
    print(f"  np.fft.fft(x) gives the same bits: {np.fft.fft(x).real.equal_bits(re)}")
    back = pn.fft.ifft(X)
    print(f"  ifft(fft(x)) max error: {np.max(np.abs(back.to_complex() - x.to_float())):.1e}")
    print(f"  rfft: {pn.fft.rfft(x).shape} outputs for {x.size} real inputs")
    z = pn.fft.fft(np.exp(2j * np.pi * np.arange(8) / 8))   # complex NumPy input, or a (re, im) pair
    print(f"  complex input: peak at k = {int(np.argmax(np.abs(z.to_complex())))}")

    _common.section("spectrum of two tones with a 1e-4 component")
    N, fs = 1024, 1024.0
    t = np.arange(N) / fs
    sig = np.sin(2 * np.pi * 100 * t) + 1e-4 * np.sin(2 * np.pi * 317 * t)
    S = np.abs(pn.fft.rfft(pn.array(sig)).to_complex()) / (N / 2)
    for k in (100, 317, 200):
        print(f"  {k:>4} Hz: {S[k]:.3e}")

    _common.section("accuracy: relative error vs float64 np.fft (same rounded input)")
    rows = []
    for N in (64, 256, 1024, 4096):
        sig = rnd.standard_normal(N)
        p32 = pn.array(sig)
        ref = np.fft.fft(p32.to_float())
        f32 = np.fft.fft(p32.to_float32())                   # NumPy 2: complex64 computed in float32
        row = [N, _common.rel_err(pn.fft.fft(p32, method="blocks"), ref)]
        row.append(_common.rel_err(pn.fft.fft(p32, method="core"), ref))
        row.append(_common.rel_err(pn.fft.fft(p32, exact=True), ref) if N <= 1024 else "-")
        row.append(_common.rel_err(f32, ref))
        p16 = pn.array(sig, pn.P16)
        ref16 = np.fft.fft(p16.to_float())
        row.append(_common.rel_err(pn.fft.fft(p16, method="blocks"), ref16))
        rows.append(row)
    _common.table(rows, ["N", "posit32 blocks", "posit32 core", "posit32 exact", "float32", "posit16 blocks"],
                  fmt="{:.2e}")
    blk = [r[4] / r[1] for r in rows]
    core = [r[4] / r[2] for r in rows]
    print(f"  float32 np.fft (pocketfft) is already good; in the same 32 bits posit32 has {min(blk):.0f}-{max(blk):.0f}x"
          f" less error with blocks,\n  {min(core):.0f}-{max(core):.0f}x with the core (radix 4, every butterfly"
          " operation rounded, built for speed);")
    print("  exact=True is the correctly rounded DFT of the rounded twiddles.")

    if args.hw is not None and 32 in args.engines:
        _common.section("speed on the KV260 (posit32)")
        for N in (64, 1024, 4096):
            batch = pn.array(rnd.standard_normal((max(1, 65536 // N), N)))
            t0 = time.perf_counter()
            pn.fft.fft(batch)
            dt = time.perf_counter() - t0
            a = np.complex64(rnd.standard_normal((batch.shape[0], N)))
            t0 = time.perf_counter()
            np.fft.fft(a)
            dn = time.perf_counter() - t0
            print(f"  N = {N:>4}: {batch.shape[0] / dt:9.0f} transforms/s;  NumPy complex64: {batch.shape[0] / dn:9.0f}/s")


if __name__ == "__main__":
    main()
