#!/usr/bin/env python3
"""Bitstream packages for PyPI: one distribution per PL application, pulled in by the extras of positnp.

    python3 packaging/make_overlay_packages.py                 # every application found in positnp/overlays
    python3 packaging/make_overlay_packages.py posit16_8_ml --wheel

    pip install positnp[kv260-ml]       ->  positnp + positnp-kv260-ml (this script's output)
    positnp-board install posit16_8_ml  ->  dtc + copy to /lib/firmware/xilinx/posit16_8_ml/, then pn.board.load()

Each package holds one application directory (<name>.bit.bin, <name>.dtsi, shell.json, <name>.json, as
build.tcl / release_builds.tcl leave them in sw/positnp/overlays/<name>/) and registers it in the entry point
group "positnp.overlays", where pn.board.available() finds it. The package version is positnp's: the
bitstream and the driver of one release go together (dependency positnp>=that version).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

SW = Path(__file__).resolve().parents[1]

# application -> (distribution, extra of positnp, one line for PyPI); names as in VecReleases.scala
APPS = {
    "posit32_16_8_mem": ("positnp-kv260", "kv260",
                         "posit32 / posit16 / posit8 engines with matmul x4 and function tables"),
    "posit32_16_8_dsp": ("positnp-kv260-dsp", "kv260-dsp",
                         "posit32 / posit16 / posit8 engines with the hardware radix-4 FFT core (posit32)"),
    "posit16_8_ml": ("positnp-kv260-ml", "kv260-ml",
                     "posit16 / posit8 engines with gemm x8 and correctly rounded function tables"),
    "posit32_16_linalg": ("positnp-kv260-linalg", "kv260-linalg",
                          "posit32 / posit16 engines with gemm x8 (posit32) and x4 (posit16)"),
}
REQUIRED = ("{n}.bit.bin", "{n}.dtsi", "shell.json")


def positnp_version() -> str:
    m = re.search(r'__version__ = "([^"]+)"', (SW / "positnp" / "__init__.py").read_text())
    return m.group(1)


def make(name: str, src: Path, out: Path, version: str) -> Path:
    """Write the source tree of the package for application `name` into out/<distribution>/; returns it."""
    dist, extra, line = APPS.get(name, (f"positnp-app-{name.replace('_', '-')}", None, f"PL application {name}"))
    missing = [f.format(n=name) for f in REQUIRED if not (src / f.format(n=name)).exists()]
    if missing:
        raise SystemExit(f"{name}: missing {', '.join(missing)} in {src} (build it: VecReleases {name}, then release_builds.tcl -tclargs {name})")
    mod = dist.replace("-", "_")
    root = out / dist
    if root.exists():
        shutil.rmtree(root)
    pkg = root / mod
    shutil.copytree(src, pkg / name, ignore=shutil.ignore_patterns("*.dtbo", "*.log", "*.bit"))
    (pkg / "__init__.py").write_text(
        f'"""{line} for the Kria KV260 (positnp PL application {name!r})."""\n\n'
        "from pathlib import Path\n\n"
        f"NAME = {name!r}\n"
        "OVERLAY_DIR = Path(__file__).parent      # holds the application directory NAME/\n")
    (root / "README.md").write_text(
        f"# {dist}\n\n{line}: the PL application `{name}` for [positnp](https://pypi.org/project/positnp/) on the "
        "Kria KV260.\n\n"
        f"```\npip install positnp[{extra or 'kv260'}]\npositnp-board install {name}\npositnp-board load {name}\n```\n\n"
        "Then every positnp array of a format the bitstream has runs on its engine; see the positnp README.\n")
    (root / "pyproject.toml").write_text(f"""[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "{dist}"
version = "{version}"
description = "{line} - Kria KV260 bitstream for positnp"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.10"
dependencies = ["positnp>={version}"]

[project.entry-points."positnp.overlays"]
{name} = "{mod}"

[tool.setuptools]
packages = ["{mod}", "{mod}.{name}"]          # the application directory as a (data-only) package

[tool.setuptools.package-data]
"{mod}.{name}" = ["*"]
""")
    return root


def wheel(root: Path, dist_dir: Path, isolation: bool = True) -> None:
    """pip wheel (with build isolation pip fetches setuptools >= 77 itself)."""
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(dist_dir), str(root)]
    subprocess.run(cmd if isolation else cmd[:4] + ["--no-build-isolation"] + cmd[4:], check=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("names", nargs="*", help="applications (default: every one in --src with all files)")
    p.add_argument("--src", type=Path, default=SW / "positnp" / "overlays")
    p.add_argument("--out", type=Path, default=SW / "packaging" / "build")
    p.add_argument("--wheel", action="store_true", help="also build the wheels into <out>/dist")
    a = p.parse_args(argv)
    names = a.names or [d.name for d in sorted(a.src.iterdir())
                        if d.is_dir() and all((d / f.format(n=d.name)).exists() for f in REQUIRED)]
    if not names:
        print(f"no application in {a.src} (VecReleases + release_builds.tcl)", file=sys.stderr)
        return 1
    version = positnp_version()
    for n in names:
        root = make(n, a.src / n, a.out, version)
        print(f"{n} -> {root}")
        if a.wheel:
            wheel(root, a.out / "dist")
    if a.wheel:
        print(f"wheels in {a.out / 'dist'} (upload: twine upload {a.out / 'dist'}/*)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
