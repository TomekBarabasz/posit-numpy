# positnp

NumPy-style [posit](https://posithub.org/docs/posit_standard-2.pdf) arithmetic: posit8, posit16 and posit32
arrays with the quire (exact dot products and sums), computed in software on any PC, or in the programmable
logic of an AMD/Xilinx **Kria KV260** with the same bits.

```python
import numpy as np, positnp as pn

x = pn.array(np.linspace(-2, 2, 5))       # posit<32,2>; pn.array(x, pn.P16) for posit16
y = x * x - x / 3 + 1                     # every operation rounded once
np.sum(y), np.dot(x, y), np.var(x)        # exact in the quire, rounded once
A = pn.array(np.random.rand(64, 64))
A @ A, np.fft.fft(x), np.convolve(x, y)   # matmul, FFT, FIR: every dot product exact
np.linalg.solve(A, pn.array(np.ones(64)))                   # LU from the quire + refinement
pn.linalg.solve(A, pn.array(np.ones(64)), lu=pn.P16)        # mixed precision: posit16 LU, posit32 residuals
pn.nn.conv2d(images, kernels, bias, padding=1)              # bias inside the quire: one rounding per output
```

`PositArray` follows the NumPy protocols (`np.*` functions and ufuncs, broadcasting, `axis` / `keepdims` /
`out=`); anything without a posit implementation raises `TypeError` instead of silently computing on the bits.
`PositComplex` covers complex data (FFT, complex matmul and convolution). Results are bit-identical between the
software backend and the hardware; every operation with a quire inside (sums, dot products, matmul, FIR, FFT
blocks, var / std, linear layers) rounds once.

## Install

On a PC (software backend, everything works, just slower):

```
pip install positnp
```

On a Kria KV260 with Ubuntu 24.04 for Kria, pick the bitstream for your workload (one is loaded at a time;
operations a bitstream does not accelerate still run, in software, with the same bits):

| extra | application | engines | fast at |
| --- | --- | --- | --- |
| `kv260` | `posit32_16_8_mem` | posit32 + posit16 + posit8 | everything, matmul x4, function tables (posit16 / posit8) |
| `kv260-dsp` | `posit32_16_8_dsp` | posit32 + posit16 + posit8 | hardware radix-4 FFT (posit32), FIR / correlation |
| `kv260-ml` | `posit16_8_ml` | posit16 + posit8 | gemm / conv2d x8, sigmoid / tanh / exp tables |
| `kv260-linalg` | `posit32_16_linalg` | posit32 + posit16 | gemm x8 (posit32), x4 (posit16), mixed-precision solve |

```
sudo apt install device-tree-compiler
pip install "positnp[kv260-dsp]"
positnp-board udmabuf                     # once per kernel: builds the u-dma-buf module (needs kernel headers)
sudo positnp-board install posit32_16_8_dsp
```

```python
import positnp as pn
pn.board.load("posit32_16_8_dsp")         # every engine becomes the backend of its format
print(pn.board.describe())
```

Loading needs root: run Python with `sudo`, or allow `xmutil` and `modprobe u-dma-buf` for your user in
`/etc/sudoers.d/` and give it access to `/dev/uio*` and `/dev/udmabuf-*` with a udev rule. The engines need
contiguous memory from CMA (`grep Cma /proc/meminfo`; the Kria image has enough by default).

## More

Source, hardware (SpinalHDL, Vivado scripts), guided examples and the measurements:
[github.com/TomekBarabasz/posit-numpy](https://github.com/TomekBarabasz/posit-numpy).
