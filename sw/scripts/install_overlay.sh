#!/usr/bin/env bash
# Instalacja i wgranie aplikacji PL na KV260 (Ubuntu + xmutil / dfx-mgr), bez PYNQ.
#
#   sw/scripts/install_overlay.sh [nazwa]        domyslnie posit32_alu
#   sw/scripts/install_overlay.sh --unload       zdejmuje biezaca aplikacje
#
# Zrodlo: sw/positnp/overlays/<nazwa>/ z build.tcl (.bit.bin, .dtsi, shell.json, .json).
# Kroki: dtc .dtsi -> .dtbo, kopia do /lib/firmware/xilinx/<nazwa>/, xmutil unloadapp,
# xmutil loadapp <nazwa>, sprawdzenie, ze jest /dev/uioN o nazwie posit_alu.
# Wymaga: dtc (apt install device-tree-compiler), xmutil, sudo.
set -euo pipefail

FW=/lib/firmware/xilinx
UIO_NAME=posit_alu

if [[ "${1:-}" == "--unload" ]]; then
    sudo xmutil unloadapp
    exit 0
fi

name=${1:-posit32_alu}
src="$(cd "$(dirname "$0")/.." && pwd)/positnp/overlays/$name"

for f in "$name.bit.bin" "$name.dtsi" shell.json; do
    [[ -f "$src/$f" ]] || { echo "brak $src/$f (build.tcl -n ... na komputerze z Vivado)" >&2; exit 1; }
done
command -v dtc >/dev/null    || { echo "brak dtc: sudo apt install device-tree-compiler" >&2; exit 1; }
command -v xmutil >/dev/null || { echo "brak xmutil" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
dtc -@ -q -O dtb -o "$tmp/$name.dtbo" "$src/$name.dtsi"

echo "instalacja: $FW/$name"
sudo mkdir -p "$FW/$name"
sudo rm -f "$FW/$name"/*
sudo cp "$src/$name.bit.bin" "$tmp/$name.dtbo" "$src/shell.json" "$FW/$name/"
[[ -f "$src/$name.json" ]] && sudo cp "$src/$name.json" "$FW/$name/"

# dfx-mgr wykrywa nowe katalogi sam (inotify); gdyby nie: restart uslugi
if ! sudo xmutil listapps | grep -qw "$name"; then
    echo "dfx-mgr nie widzi $name - restart dfx-mgr"
    sudo systemctl restart dfx-mgr
    sleep 2
fi

sudo xmutil unloadapp >/dev/null 2>&1 || true   # bez bledu, gdy nic nie bylo wgrane
sudo xmutil loadapp "$name"

for u in /sys/class/uio/uio*; do
    if [[ "$(cat "$u/name" 2>/dev/null)" == "$UIO_NAME" ]]; then
        dev="/dev/$(basename "$u")"
        echo "ok: $name wgrany, $UIO_NAME -> $dev (map0: $(cat "$u/maps/map0/addr"), $(cat "$u/maps/map0/size") B)"
        echo "test: cd sw && sudo python3 -m pytest -v tests/test_board.py"
        exit 0
    fi
done
echo "loadapp przeszedl, ale nie ma /dev/uio* o nazwie $UIO_NAME - sprawdz: dmesg | tail -30" >&2
exit 1
