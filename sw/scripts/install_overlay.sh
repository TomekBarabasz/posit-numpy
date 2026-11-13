#!/usr/bin/env bash
# Instalacja i wgranie aplikacji PL na KV260 (Ubuntu + xmutil / dfx-mgr), bez PYNQ.
#
#   sw/scripts/install_overlay.sh [nazwa]        domyslnie posit32_alu; silniki: posit32_vec, posit32_16_8_vec, ...
#   sw/scripts/install_overlay.sh --unload       zdejmuje biezaca aplikacje
#
# Zrodlo: sw/positnp/overlays/<nazwa>/ z build.tcl (.bit.bin, .dtsi, shell.json, .json).
# Kroki: dtc .dtsi -> .dtbo, kopia do /lib/firmware/xilinx/<nazwa>/, xmutil unloadapp,
# xmutil loadapp <nazwa>, sprawdzenie urzadzen z <nazwa>.json: /dev/uioN dla rejestrow i DMA
# kazdego silnika, /dev/<udmabuf> dla jego bufora DMA (modul u-dma-buf: build_udmabuf.sh).
# Wymaga: dtc (apt install device-tree-compiler), xmutil, sudo.
set -euo pipefail

FW=/lib/firmware/xilinx

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

# nazwy urzadzen z <nazwa>.json (bez jq: python3): "engines" (silniki PositVecAxi, kazdy z DMA
# i buforem) albo starsze pola "ip" / "dma" / "udmabuf" na najwyzszym poziomie
json_names() {
    python3 - "$src/$name.json" "$1" <<'PY' 2>/dev/null || true
import json, sys
d = json.load(open(sys.argv[1]))
keys = {"uio": ("ip", "dma"), "buf": ("udmabuf",)}[sys.argv[2]]
items = d.get("engines") or [d]
print(" ".join(e[k] for e in items for k in keys if e.get(k)))
PY
}
UIO_NAMES=$(json_names uio)
[[ -n "${UIO_NAMES// /}" ]] || UIO_NAMES=posit_alu
UDMABUFS=$(json_names buf)
if [[ -n "$UDMABUFS" ]]; then
    sudo modprobe u-dma-buf || { echo "brak modulu u-dma-buf: scripts/build_udmabuf.sh" >&2; exit 1; }
fi

# dfx-mgr wykrywa nowe katalogi sam (inotify); gdyby nie: restart uslugi
if ! sudo xmutil listapps | grep -qw "$name"; then
    echo "dfx-mgr nie widzi $name - restart dfx-mgr"
    sudo systemctl restart dfx-mgr
    sleep 2
fi

sudo xmutil unloadapp >/dev/null 2>&1 || true   # bez bledu, gdy nic nie bylo wgrane
sudo xmutil loadapp "$name"

ok=1
for want in $UIO_NAMES; do
    dev=""
    for u in /sys/class/uio/uio*; do
        if [[ "$(cat "$u/name" 2>/dev/null)" == "$want" ]]; then
            dev="/dev/$(basename "$u") (map0: $(cat "$u/maps/map0/addr"), $(cat "$u/maps/map0/size") B)"
        fi
    done
    if [[ -n "$dev" ]]; then echo "ok: $want -> $dev"; else echo "brak /dev/uio* o nazwie $want" >&2; ok=0; fi
done
for buf in $UDMABUFS; do
    s=/sys/class/u-dma-buf/$buf
    if [[ -e /dev/$buf ]]; then
        echo "ok: /dev/$buf ($(cat $s/size) B @ $(cat $s/phys_addr))"
    else
        echo "brak /dev/$buf (modul u-dma-buf, wezel w .dtsi; za malo CMA? grep Cma /proc/meminfo)" >&2; ok=0
    fi
done
if [[ $ok == 1 ]]; then
    echo "$name wgrany; test: cd sw && sudo python3 -m pytest -v tests/"
    exit 0
fi
echo "loadapp przeszedl, ale brakuje urzadzen - sprawdz: dmesg | tail -30" >&2
exit 1
