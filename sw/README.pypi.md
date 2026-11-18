# positnp

NumPy-style [posit](https://posithub.org/docs/posit_standard-2.pdf) arithmetic: posit8, posit16 and posit32
arrays with the quire (exact dot products and sums), computed in software on any PC, or by posit engines in
the programmable logic of an AMD/Xilinx **Kria KV260** board - with the same bits either way.

## Install

```bash
pip install positnp
```

Python 3.10 or newer and NumPy; nothing else. Without a board everything runs in the software backend.

## Quick start

```python
import numpy as np, positnp as pn

x = pn.array(np.linspace(-2, 2, 5))       # posit<32,2>; pn.array(x, pn.P16) for posit16
y = x * x - x / 3 + 1                     # every operation rounded once
y.to_float()                              # back to float64 (NaR -> nan)

np.sum(y), np.dot(x, y), np.var(x)        # exact in the quire, rounded once
A = pn.array(np.random.rand(64, 64))
A @ A                                     # every element: one exact dot product, rounded once
np.fft.fft(x), np.convolve(x, y)          # FFT and FIR on posits
np.linalg.solve(A, pn.ones(64))           # LU from the quire + iterative refinement
pn.linalg.solve(A, pn.ones(64), lu=pn.P16)   # mixed precision: posit16 LU, posit32 residuals

x16 = x.astype(pn.P16)                    # other formats; any pn.PositConfig(n, es)
pn.stats()                                # NaR / saturated / inexact counters
```

- **NumPy protocols**: `np.*` functions and ufuncs, broadcasting, `axis` / `keepdims` / `out=`, shape
  functions; anything without a posit implementation raises `TypeError` instead of computing on the bits.
- **Exact where it counts**: sums, dot products, matmul, FIR, `var` / `std`, `median`, `cumsum`, linear
  layers - every result rounded once from the exact value.
- **More**: `PositComplex` (FFT, complex matmul and convolution), `pn.linalg` (solve, inv, det, cholesky,
  lstsq), `pn.signal` (lfilter, convolve2d), `pn.nn` (linear, conv2d, softmax, ...), elementary functions
  correctly rounded for posit8 / posit16.

## Supported boards

**AMD/Xilinx Kria KV260 Vision AI Starter Kit** (K26 SOM, Zynq UltraScale+) with Ubuntu 24.04 for Kria. The
PL applications (bitstream + device tree overlay) come as separate packages, one per workload, installed with
an extra; one application is loaded at a time, and whatever it does not accelerate still runs, in software,
with the same bits:

| extra | application | engines | for |
| --- | --- | --- | --- |
| `kv260` | `posit32_16_8_mem` | posit32 + posit16 + posit8 | general use and experiments: element-wise ops at 147 / 297 / 538 M/s, matmul with 4 columns per pass (~390 M MAC/s per engine), correctly rounded function tables (posit16, posit8), arrays in device memory |
| `kv260-dsp` | `posit32_16_8_dsp` | posit32 + posit16 + posit8 | signal processing: radix-4 FFT core in posit32 (N = 16 .. 4096, 1.75-4x NumPy complex64 on the board's CPU), FIR / correlation, complex convolution |
| `kv260-ml` | `posit16_8_ml` | posit16 + posit8 | neural networks in 8 / 16 bits: gemm with 8 columns per pass for `pn.nn.linear` / `conv2d`, exp / tanh / sigmoid from tables |
| `kv260-linalg` | `posit32_16_linalg` | posit32 + posit16 | linear algebra: gemm with 8 columns per pass in posit32, 4 in posit16, for mixed-precision `solve` (posit16 LU, posit32 quire residuals) |

## On the board

Ubuntu does not allow `pip install` into the system Python, hence the venv (`--system-site-packages`
reuses NumPy from apt):

```bash
sudo apt install -y device-tree-compiler
python3 -m venv --system-site-packages ~/pn
~/pn/bin/pip install "positnp[kv260-dsp]"
~/pn/bin/positnp-board udmabuf                          # once per kernel: the u-dma-buf module
sudo ~/pn/bin/positnp-board install posit32_16_8_dsp    # once per application
sudo ~/pn/bin/positnp-board permissions                 # once: use the board without sudo (then log in again)
~/pn/bin/positnp-board load posit32_16_8_dsp            # loads it and describes the engines
```

```python
import positnp as pn
pn.board.load("posit32_16_8_dsp")     # every engine becomes the backend of its format
print(pn.board.describe())
```

`positnp-board permissions` lets your user load applications and use the engines without `sudo`: it creates
the group `posit`, adds you to it, allows `xmutil` and `modprobe u-dma-buf` for the group in
`/etc/sudoers.d/positnp` (checked with `visudo` before it is installed) and gives the group access to the
engines' `/dev/uio*`, `/dev/udmabuf-posit-*` and the buffers' cache-sync attributes with a udev rule
(`/etc/udev/rules.d/99-positnp.rules`). `--dry-run` shows the files and commands, `--user` / `--group` change
who gets access. Only add trusted users: an engine is a DMA master, so access to it is close to root. Without
it, run Python with `sudo`. The engines use contiguous memory from CMA (`grep Cma /proc/meminfo`; the Kria image
has enough by default).

## More

Guided examples (where posits beat floats and where they do not), the hardware sources (SpinalHDL, Vivado
scripts) and the measurements: [github.com/TomekBarabasz/posit-numpy](https://github.com/TomekBarabasz/posit-numpy).
