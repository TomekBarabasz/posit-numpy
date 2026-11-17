"""FIR filters: np.convolve / np.correlate on posits, every output exact in the quire.

    python3 examples/21_fir_filter.py [--sim | --hw [APP]]

A low-pass FIR removes an interfering tone from a noisy signal. Each output sample is a dot product of
the taps with a window of the signal; positnp computes it exactly in the quire and rounds once (on the
KV260: gemv with a window sliding over the signal, the signal read once, about 4 MAC per clock).
Floats round after every multiply and add, so the error of an output grows with the number of taps.

Two errors per format:
- arithmetic: the filter applied to the already rounded signal and taps, vs the exact result;
- total: vs the ideal float64 filter of the unrounded signal (adds quantization of the inputs).
"""

import time

import numpy as np

import _common
import positnp as pn


def lowpass(taps: int, cutoff: float) -> np.ndarray:
    """Windowed-sinc low-pass FIR (cutoff as a fraction of the sample rate), unit DC gain."""
    m = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff * m) * np.hamming(taps)
    return h / h.sum()


def snr_db(y, ref) -> float:
    y, ref = _common.f64(y), _common.f64(ref)
    return float(10 * np.log10(np.sum(ref ** 2) / np.sum((y - ref) ** 2)))


def fir_float16(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """'same' convolution with every multiply and add in float16 (what a float16 MAC loop does)."""
    xs, hs = np.float16(x), np.float16(h)
    T = hs.size
    xp = np.concatenate([np.zeros(T // 2, np.float16), xs, np.zeros(T - 1 - T // 2, np.float16)])
    y = np.zeros(xs.size, np.float16)
    with np.errstate(all="ignore"):
        for j in range(T):
            y = y + hs[T - 1 - j] * xp[j:j + xs.size]
    return y


def main(argv=None):
    args = _common.start(__doc__, argv)
    rnd = np.random.default_rng(3)
    fs, N = 8000.0, 4096
    t = np.arange(N) / fs
    clean = np.sin(2 * np.pi * 50 * t) + 0.5 * np.sin(2 * np.pi * 300 * t)
    x = clean + 0.8 * np.sin(2 * np.pi * 2500 * t) + 0.05 * rnd.standard_normal(N)
    h = lowpass(127, 600 / fs)
    ideal = np.convolve(x, h, "same")

    _common.section(f"low-pass filter, {h.size} taps, {N} samples")
    rows = []
    for name, cfg in (("posit32 (quire)", pn.P32), ("posit16 (quire)", pn.P16), ("posit8 (quire)", pn.P8)):
        xp, hp = pn.array(x, cfg), pn.array(h, cfg)
        y = np.convolve(xp, hp, "same")
        rows.append((name, snr_db(y, np.convolve(xp.to_float(), hp.to_float(), "same")), snr_db(y, ideal)))
    xf, hf = np.float32(x), np.float32(h)
    rows.insert(1, ("float32", snr_db(np.convolve(xf, hf, "same"),
                                      np.convolve(xf.astype(float), hf.astype(float), "same")),
                    snr_db(np.convolve(xf, hf, "same"), ideal)))
    x16, h16 = np.float16(x).astype(float), np.float16(h).astype(float)
    y16 = fir_float16(x, h)
    rows.insert(3, ("float16 (float16 MACs)", snr_db(y16, np.convolve(x16, h16, "same")), snr_db(y16, ideal)))
    _common.table(rows, ["format", "arithmetic SNR [dB]", "total SNR [dB]"], fmt="{:.1f}")
    snr = {r[0].split()[0]: r[1] for r in rows}
    print("  higher is better; 6 dB = 1 bit. With the quire the arithmetic adds only the final rounding, so")
    print("  the total is set by how well the format holds the signal and the taps.")

    _common.section("the filter works")
    y = np.convolve(pn.array(x, pn.P16), pn.array(h, pn.P16), "same").to_float()
    spec = lambda v: np.abs(np.fft.rfft(v * np.hanning(N)))      # noqa: E731
    f = np.fft.rfftfreq(N, 1 / fs)
    for tone in (50, 300, 2500):
        k = int(np.argmin(np.abs(f - tone)))
        print(f"  {tone:>5} Hz: input {20 * np.log10(spec(x)[k]):6.1f} dB -> posit16 output {20 * np.log10(spec(y)[k]):6.1f} dB")

    _common.section("np.correlate: find a known pulse in noise (matched filter)")
    pulse = np.sin(2 * np.pi * np.arange(64) / 16) * np.hanning(64)
    sig = 0.7 * rnd.standard_normal(2000)
    sig[1234:1234 + 64] += pulse
    c = np.correlate(pn.array(sig, pn.P16), pn.array(pulse, pn.P16), "valid")
    print(f"  pulse inserted at 1234, correlation peak at {int(np.argmax(c))}  (posit16, modes: full / same / valid)")

    if args.hw is not None and args.engines:
        _common.section("speed on the KV260")
        for n, hw in sorted(args.engines.items(), reverse=True):
            if getattr(hw, "gemv_max_k", 0) and hw.version >= 0x0504:
                macs = hw.time_correlate(1 << 20, 64) * 64          # outputs / s x taps
                print(f"  posit{n}: {macs / 1e6:.0f} M MAC/s (64 taps)")
        a, b = np.float32(rnd.standard_normal(1 << 20)), np.float32(rnd.standard_normal(64))
        t0 = time.perf_counter()
        np.convolve(a, b, "valid")
        print(f"  NumPy float32 np.convolve: {a.size * b.size / (time.perf_counter() - t0) / 1e6:.0f} M MAC/s")
    return snr


if __name__ == "__main__":
    main()
