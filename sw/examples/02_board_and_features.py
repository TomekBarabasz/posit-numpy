"""The KV260 from Python: find, load and describe the posit engines, detect their features.

    python3 examples/02_board_and_features.py                     # PC: what is (not) there, then a simulated board
    sudo python3 examples/02_board_and_features.py --hw           # KV260: the loaded application
    sudo python3 examples/02_board_and_features.py --hw posit32_16_8_mem

A bitstream holds one or more engines (one per posit width). What an engine can do depends on how it
was built (VecKv260 options): element-wise ops, quire, div / sqrt / FMA lanes, matmul (gemv), FIR,
function tables, FFT core, its own AXI4 master and device memory. The engine reports all of it in its
registers; PositVecHw reads them when it opens, so code can check features instead of guessing.
Whatever an engine lacks, positnp computes in software (PositSw) - with the same bits - and counts it
in pn.stats()["fallback"].

On the board, once per board (see sw/README.md):
    sw/scripts/build_udmabuf.sh                    # u-dma-buf kernel module (device memory)
    sw/scripts/install_overlay.sh posit32_16_8_mem # install an application into /lib/firmware/xilinx
    /etc/sudoers.d/positnp: <user> ALL=(root) NOPASSWD: /usr/bin/xmutil, /usr/sbin/modprobe u-dma-buf
"""

import numpy as np

import _common
import positnp as pn


def features(hw) -> dict:
    """What one engine (PositVecHw) can do - read from its registers when it was opened."""
    cfg = hw.engine_cfg() or {}
    return {
        "format": str(hw.config),
        "version": f"{hw.version >> 8}.{hw.version & 0xFF}",
        "element-wise ops": ", ".join(hw.array_ops),
        "elements / clock": cfg.get("lanes", 1),
        "div / sqrt / FMA lanes": cfg.get("units", "?"),
        "quire (exact sum, dot)": hw.has_quire(),
        "own AXI4 master, FMA3": f"{hw.master}, {hw.fma3}",
        "device memory (zero copy)": hw.heap is not None,
        "matmul / gemv": f"K <= {hw.gemv_max_k}, {hw.gemv_cols} columns / pass" if hw.gemv_max_k else False,
        "FIR (gemv window, 5.4)": bool(hw.gemv_max_k and hw.version >= 0x0504),
        "function table (exp, log, ...)": f"{hw.table_rate} lookups / clock" if hw.has_table() else False,
        "FFT core (radix 4)": f"N = 16 .. {4 ** hw.fft_max_s}" if hw.fft_max_s else False,
    }


def probe_board() -> None:
    """What a script sees before loading anything - works on any machine."""
    apps = pn.board.apps()
    print(f"installed applications: {apps or 'none (not a KV260, or nothing installed in /lib/firmware/xilinx)'}")
    for a in apps:
        print(f"  {a.name:<24} kind={a.kind:<5} engines={[e.get('n') for e in a.engines]}")
    try:
        print(f"loaded application: {pn.board.active()}")
    except pn.board.BoardError as e:
        print(f"loaded application: unknown ({e})")


def choose_fft_method(n: int, cfg: pn.PositConfig) -> str:
    """Example of a decision made from the features: which FFT path will run for N points."""
    be = pn.get_backend(cfg)
    max_s = getattr(be, "fft_max_s", 0)
    if max_s and n in [4 ** s for s in range(2, max_s + 1)]:
        return "hardware FFT core"
    if getattr(be, "gemv_max_k", 0):
        return "DFT blocks on the engine's matmul"
    return "software (PositSw)"


def main(argv=None):
    args = _common.parser(__doc__).parse_args(argv)
    _common.section("1. probing")
    probe_board()
    if args.hw is None and not args.sim:
        print("\nNo --hw: continuing with simulated engines (the real driver on register models).")
        args.sim = True
    engines = _common.setup(args, quiet=True)
    if args.hw is not None:
        _common.section("2. pn.board.describe()")
        print(pn.board.describe())

    _common.section("3. features of every engine")
    for n, hw in sorted(engines.items(), reverse=True):
        print(f"\n  posit{n}:")
        for k, v in features(hw).items():
            print(f"    {k:<32} {v}")

    _common.section("4. which backend computes which format")
    for c in (pn.P32, pn.P16, pn.P8, pn.PositConfig(24, 2)):
        print(f"  {str(c):<12} -> {type(pn.get_backend(c)).__name__}")
    print(f"  pn.active_backends(): {sorted(str(c) for c in pn.active_backends())}")

    _common.section("5. deciding from features")
    for n in (64, 1000, 4096):
        print(f"  FFT of {n:>4} points in posit32: {choose_fft_method(n, pn.P32)}")
    pn.clear_stats(pn.P8)
    x = pn.array(np.linspace(1, 2, 1000), pn.P8)
    _ = pn.fma(x, x, x)                         # three arrays: FMA3 when the engine has it, else software
    print(f"  posit8 fma(x, x, x): {pn.stats(pn.P8)}  (fallback = elements computed in software)")

    _common.section("6. switching backends")
    with pn.using(None):                        # this block in software, then back to the engines
        print(f"  inside pn.using(None): {type(pn.get_backend(pn.P32)).__name__}")
    print(f"  after the block:       {type(pn.get_backend(pn.P32)).__name__}")
    if args.hw is not None:
        print("\n  pn.board.unload() would close the engines and unload the PL application.")
    pn.set_backend(None)


if __name__ == "__main__":
    main()
