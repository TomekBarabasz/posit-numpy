# posit-numpy

A NumPy-style library for [posit](https://posithub.org/docs/posit_standard-2.pdf) arithmetic on the AMD/Xilinx
Kria KV260: posit arrays are computed by programmable logic (PL) generated in SpinalHDL and driven from Python
running on the processing system (Ubuntu with `xmutil`, registers through UIO; no PYNQ).
Plan: [posit-numpy on the KV260: architecture and implementation plan](https://claude.ai/artifact/JtAzXExTzWmnyTq6d8kmym).
How the KV260 integration works (PS ↔ PL, registers, AXI, DMA, the engine's own master, buffers), written for
learning (in Polish): [doc/kv260/](doc/kv260/README.md).

**No board? No problem.** The Python package `positnp` has a software backend that is bit-exact with the
hardware, so everything runs on an ordinary PC:

```bash
git clone https://github.com/TomekBarabasz/posit-numpy
cd posit-numpy/sw
pip install -e .
python3 examples/00_hello_posit.py
```

```python
import numpy as np, positnp as pn

x = pn.array(np.linspace(-2, 2, 5))       # posit<32,2>; pn.array(x, pn.P16) for posit16
y = x * x - x / 3 + 1                     # every operation rounded once
np.sum(y), np.dot(x, y)                   # exact in the quire, rounded once
A = pn.array(np.random.rand(64, 64))
A @ A, np.fft.fft(x), np.convolve(x, y)   # matmul, FFT, FIR: every dot product exact
np.var(x), x ** 3, np.cumsum(x)           # exact, rounded once
np.linalg.solve(A, pn.array(np.ones(64)))  # LU from the quire, refined with a quire residual
```

**Start with the examples:** [`sw/examples/README.md`](sw/examples/README.md) - a guided tour from `+ - * /` to
gemm, FIR and FFT, plus problems where posits beat floats of the same width. All of them run on a PC; with
`--hw` they run on the KV260.

```
hw/                     sbt, SpinalHDL 1.15.0 (from lib/sbt/HwBuild.scala)
  lib/                  submodule fpga-hw-lib: vertebra (testplans, SimEnv)
  posit-core/           package posit         format, decoder/encoder, add/sub/mul/div, dividers (from posit-spinal)
  posit-vector/         package posit.vector  vector engine: ALU, streams, quire
  posit-kv260/          package posit.kv260   AXI, IP wrappers, RTL generators
  top-modules/<id>/     package posit.top.<id> top levels for Vivado (sbt: top-<id>)
sw/                     Python bindings (positnp): ref.py, regs.py, backend_hw.py
sw/examples/            examples from the basics to gemm / FIR / FFT and comparisons with floats (no board needed)
```

Status:
- stage 1 (ALU behind AXI4-Lite, called from Python): verified on the board - `hw/top-modules/alu/README.md`;
- stage 2 (arrays over AXI4-Stream and AXI DMA): verified on the board - `hw/top-modules/vec/README.md`;
- stage 3 (`PositArray` with the NumPy protocols, `vecops`, conversions, `stats()`, notebook): verified on the board - `sw/README.md`;
- stage 4 (quire: exact `sum`, `dot`, `mean`, `norm`): verified on the board - `hw/top-modules/vec/README.md`;
- stage 5 (sqrt, FMA / AXPY, float32 conversions in the PL; `np.sqrt`, `pn.fma`, `pn.axpy`): verified on the board - `hw/top-modules/vec/README.md`;
- several engines in one bitstream (posit8/16/32), div / sqrt / FMA lanes, `pn.board`: verified on the board
  (`posit32_16_8_vec`) - `hw/top-modules/vec/README.md`, `sw/README.md`;
- stage 6 (own AXI4 master instead of AXI DMA, FMA3 on three arrays, interrupt): verified on the board
  (`posit32_16_8_mem`, posit32 add 59.5 M/s vs 25.4 with AXI DMA; about 92 M/s with double buffering) -
  `hw/top-modules/vec/README.md`;
- wider engine (L elements per clock: posit32 ×2, posit16 ×4, posit8 ×8): verified on the board - add 121 / 229 / 400 M/s,
  posit8 faster than NumPy float32 - `hw/top-modules/vec/README.md`;
- zero-copy arrays (device memory, `x.to_device()`): verified on the board - add 147 / 297 / 538 M/s,
  posit16 and posit8 faster than NumPy float32; fan controlled from Linux as in the factory application;
- stage 7a (exact `A @ B` / `np.matmul` on the quire: OP_XLOAD + OP_GEMV, two quires alternating): verified on the board -
  0.999 MAC per clock, about 98 M MAC/s per engine (posit32, posit16, posit8), every element bit for bit with the quire - `hw/top-modules/vec/README.md`;
- faster matmul (5.2: P = 4 columns of B per pass of A, 8 quires): verified on the board - 3.98 MAC per clock,
  about 388 M MAC/s per engine (4× more than with one column); OOC posit32 35 119 LUT, 103.6 MHz;
- elementary functions (5.3: `np.exp`, `np.log`, `np.sin`, `np.tanh`, `pn.sigmoid`, … - 21 functions): posit8 / posit16 from
  correctly rounded tables in the engine's BRAM (OP_TABLE, up to L elements per clock), posit32 from float64 in software;
  verified on the board - posit16 `exp` 332 M/s, posit8 648 M/s (NumPy float32 `np.exp` on the A53: 23 M/s);
- FIR / convolution (5.4: `np.convolve`, `np.correlate`, exact in the quire): gemv with a window sliding over the signal -
  the signal is read once, P outputs per row; verified on the board - 3.37 / 3.82 / 3.95 MAC per clock for 16 / 64 / 256 taps
  (up to 394 M MAC/s), 1.7-3.5× faster than float32 `np.convolve` on the A53;
- FFT (`pn.fft.fft / ifft / rfft`, `np.fft.fft`): DFT blocks exact in the quire through matmul, twiddles through FMA3, no new
  hardware - posit32 6-7× more accurate than float32 `np.fft`; verified on the board (bit for bit), 13-19× slower
  than `np.fft` on the A53 (e.g. 18 455 64-point transforms / s);
- hardware radix-4 FFT (5.5, optional `VecKv260 --fft 6:32`, with `--gemv-cols 32=2,16=2,8=0` so that it fits):
  verified on the board bit for bit with the model - posit32 N = 4096: 2 874 transforms / s (NumPy complex64: 2 352),
  N = 64: 152 550 / s, 8-21× faster than DFT blocks, about 3× more accurate than float32;
- FFT 5.6: load / compute / store at once (three sets of banks, 2.4× fewer clocks), ifft on the core (`FFT_CTRL.conj`),
  zero-copy FFT in device memory (`PositComplex.to_device()`): verified on the board - posit32 640 025 / 41 955 / 9 356
  transforms / s for N = 64 / 1024 / 4096, 1.75-4× faster than NumPy complex64 on the A53, about 3× more accurate than float32.

# How-tos
## Cloning (hw/lib is a submodule: fpga-hw-lib)
```
git clone --recurse-submodules https://github.com/TomekBarabasz/posit-numpy
git config submodule.recurse true
git config push.recurseSubmodules check
```
Existing clone: `git submodule update --init`. Before editing in `hw/lib`: `git switch main`.

## Start the container
from the `hw` directory
```bash
docker run --rm -ti -v .:/workspace -w /workspace -v .\.sbt:/sbt ghcr.io/spinalhdl/docker:master
```

## Tests (from `hw/`)
```
sbt test                                         # all blocks and top levels (without the lib/ tests)
sbt posit-core/test
sbt "posit-core/testOnly *PositUnitTestplan -- -z p8"
sbt "{lib}vertebra/test"                         # library projects with the {lib} prefix
```

Simulation switches for the sbt session (`lib/sbt/HwBuild.scala`):

| task | effect |
| --- | --- |
| `wavesOn` / `wavesOff` | FST in `simWorkspace/<name>_<backend>` |
| `backendVrl` / `backendGhdl` | simulation backend (GHDL by default) |
| `simStatus` | current settings |
| `simClean` | removes the run workspaces and the block's `hw/gen` |

From the shell: `VERTEBRA_BACKEND=verilator`, `VERTEBRA_WAVES=1`, `SIM_SEED=random|<number>`.
`POSIT_SIM_FULL=1` restores the vector counts of posit-spinal (300k posit16, 100k posit32); by default
the quick set, posit⟨8,*⟩ always exhaustively.

## Test rules
- A block test is `class <Dut>Testplan extends TestplanSuite` (vertebra) in `<block>/hw/spinal/test`:
  the plan (`Testpoint`: name, stage V1/V2/V3, `stimulus`, required `checking`) in `object <Dut>Plan`,
  then `testpoint(name, variant) { ... }` or `unimplemented(name, reason)`.
  `testplan completeness` fails when a testpoint has neither a test nor `unimplemented`, or when V1 is incomplete.
- `Stream` ports: `++ StreamConformance.testpoints("<port>")` in the plan and the checkers from `StreamConformance`.
- Simulation only through `SimEnv(posit.Conventions.spinal, "<unique name>")`: backend and waves from the switches,
  the same reset as in synthesis (synchronous, active low).
- Seed: `doSim(name, seed = SimSeed(name))` and `new Random(SimSeed(name))`.
- Reference: a reference model (`PositRef` in `posit-core/hw/spinal/test`), compared bit for bit.
  Shared driver and scoreboard for Stream units: `PositSimKit`.

## RTL generation
```
sbt "posit-kv260/runMain posit.kv260.PositGen"            # -> posit-kv260/hw/gen
sbt "posit-kv260/runMain posit.kv260.PositGen 24 2 mul"
sbt "top-ooc/runMain posit.top.ooc.OocUnits"              # stage 0: units for OOC synthesis
```

## Python (from `sw/`)
```
pip install -e .[test]
python3 -m pytest tests          # ref.py vs digests from Scala, driver on fakes; test_board.py only on the board
```

## Stage 1 on the board (no PYNQ: Ubuntu + xmutil + UIO)
```
sbt "top-alu/runMain posit.top.alu.AluKv260"     # from hw/
vivado -mode batch -nojournal -log build.log -source build.tcl    # from hw/top-modules/alu/vivado
scripts/install_overlay.sh posit32_alu           # on the KV260, from sw/
sudo python3 -m pytest -v tests/test_board.py    # on the KV260, from sw/
```

## Array engines on the board (stage 2+)
One or several formats in one bitstream (`-engines`, details in `hw/top-modules/vec/README.md`):
```
sbt "top-vec/runMain posit.top.vec.VecKv260 32 16 8"                                  # from hw/
vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -engines "32 16 8"   # from hw/top-modules/vec/vivado
scripts/build_udmabuf.sh                         # on the KV260, from sw/, once per kernel (again after a kernel update)
scripts/install_overlay.sh posit32_16_8_mem      # on the KV260, from sw/ (stage 6; with -io dma: posit32_16_8_vec)
sudo python3 -m pytest -v -s tests/test_board_vec.py
```
On a development board it is best to disable automatic Ubuntu updates: replacing the kernel, `systemd` and Python
during tests once hung the board, and the new kernel has no u-dma-buf module after a reboot
(`sudo systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer`; update manually,
then `scripts/build_udmabuf.sh`). After a hard reset during an update: `sudo dpkg --configure -a`.
From Python (`pn.board`): `pn.board.apps()`, `pn.board.load("posit32_16_8_mem")` (every engine becomes
the backend of its format), `pn.board.describe()`, `pn.board.unload()`; described in `sw/README.md`.

## OOC synthesis (stage 0)
from `hw/top-modules/ooc/vivado`, options and method: `hw/top-modules/ooc/README.md`
```
vivado -mode batch -nojournal -log vivado.log -source ooc_synth.tcl -tclargs -impl
```
