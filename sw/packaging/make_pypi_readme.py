#!/usr/bin/env python3
"""README.pypi.md (the long description of the positnp package) from examples/README.md.

    python3 packaging/make_pypi_readme.py          # rewrite sw/README.pypi.md
    python3 packaging/make_pypi_readme.py --check  # exit 1 when README.pypi.md is out of date (tests use it)

The examples README is the one source: the PyPI page gets it with the installation from PyPI instead of a
git clone, links that work outside the repository (PyPI cannot follow relative links) and the board setup of
the installed package instead of the repository's (Polish) build documentation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SW = Path(__file__).resolve().parents[1]
SRC = SW / "examples" / "README.md"
DST = SW / "README.pypi.md"
REPO = "https://github.com/TomekBarabasz/posit-numpy"
BLOB = f"{REPO}/blob/main/sw/examples/"
RAW = "https://raw.githubusercontent.com/TomekBarabasz/posit-numpy/main/sw/examples/"

INSTALL = """```bash
pip install positnp            # only needs numpy; runs everywhere with the software backend
```

The examples below are in the repository (they are not part of the package):

```bash
git clone https://github.com/TomekBarabasz/posit-numpy
python3 posit-numpy/sw/examples/00_hello_posit.py
```"""

BOARD = """## On the board (Kria KV260)

Pick the bitstream for your workload; one application is loaded at a time, and whatever a bitstream does not
accelerate still runs, in software, with the same bits:

| extra | application | engines | fast at |
| --- | --- | --- | --- |
| `kv260` | `posit32_16_8_mem` | posit32 + posit16 + posit8 | everything: matmul x4, function tables (posit16 / posit8), device memory |
| `kv260-dsp` | `posit32_16_8_dsp` | posit32 + posit16 + posit8 | hardware radix-4 FFT core (posit32), FIR / correlation |
| `kv260-ml` | `posit16_8_ml` | posit16 + posit8 | gemm / conv2d x8, sigmoid / tanh / exp tables |
| `kv260-linalg` | `posit32_16_linalg` | posit32 + posit16 | gemm x8 (posit32), x4 (posit16), mixed-precision solve |

On the KV260 with Ubuntu 24.04 for Kria (Ubuntu does not allow `pip install` into the system Python, hence
the venv; `--system-site-packages` reuses numpy from apt):

```bash
sudo apt install -y device-tree-compiler
python3 -m venv --system-site-packages ~/pn
~/pn/bin/pip install "positnp[kv260-dsp]"
~/pn/bin/positnp-board udmabuf                          # once per kernel: the u-dma-buf module
sudo ~/pn/bin/positnp-board install posit32_16_8_dsp
sudo ~/pn/bin/positnp-board load posit32_16_8_dsp       # and describes the engines
```

```python
import positnp as pn
pn.board.load("posit32_16_8_dsp")     # every engine becomes the backend of its format
print(pn.board.describe())
```

Loading needs root: run Python with `sudo`, or allow `xmutil` and `modprobe u-dma-buf` for your user in
`/etc/sudoers.d/` and give it access to `/dev/uio*` and `/dev/udmabuf-*` with a udev rule. With an application
loaded, `--hw` runs every example on the engines; the speed sections of `20`, `21`, `22` and `23` compare against
NumPy float32 on the board's Cortex-A53 (e.g. matmul ~390 M MAC/s per engine, posit16 `exp` 332 M/s vs 23 M/s
for `np.exp` in float32).

Source, hardware (SpinalHDL, Vivado scripts) and the measurements: [github.com/TomekBarabasz/posit-numpy]({repo}).
""".format(repo=REPO)


def _link(m: re.Match) -> str:
    text, target = m.group(1), m.group(2)
    if re.match(r"[a-z]+://|#|mailto:", target):
        return m.group(0)
    base = RAW if m.group(0).startswith("!") else BLOB
    url = base + target
    while "/../" in url:                                     # ../README.md -> sw/README.md
        url = re.sub(r"[^/]+/\.\./", "", url, count=1)
    return f"{'!' if m.group(0).startswith('!') else ''}[{text}]({url})"


def render() -> str:
    s = SRC.read_text(encoding="utf-8")
    s = s.replace("# positnp examples", "# positnp", 1)
    s = re.sub(r"```bash\ngit clone .*?```", lambda _: INSTALL, s, count=1, flags=re.S)
    s = re.sub(r"## On the board\n.*", lambda _: BOARD, s, count=1, flags=re.S)
    s = re.sub(r"!?\[([^\]]*)\]\(([^)\s]+)\)", _link, s)
    return s.rstrip("\n") + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    text = render()
    if "--check" in argv:
        if not DST.exists() or DST.read_text(encoding="utf-8") != text:
            print(f"{DST} is out of date: python3 packaging/make_pypi_readme.py", file=sys.stderr)
            return 1
        return 0
    DST.write_text(text, encoding="utf-8")
    print(f"{DST} <- {SRC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
