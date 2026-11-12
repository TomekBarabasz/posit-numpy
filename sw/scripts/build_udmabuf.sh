#!/usr/bin/env bash
# Modul jadra u-dma-buf (github.com/ikwzm/udmabuf) na KV260, raz na jadro.
# Daje ciagle bufory pod AXI DMA bez PYNQ: wezel "ikwzm,u-dma-buf" w overlayu
# -> /dev/udmabuf-posit + /sys/class/u-dma-buf/udmabuf-posit/phys_addr.
#
#   sw/scripts/build_udmabuf.sh [tag]      domyslnie: najnowszy tag z repozytorium
#
# Instaluje do /lib/modules/$(uname -r)/extra, depmod, ladowanie przy starcie
# (/etc/modules-load.d/u-dma-buf.conf). Po aktualizacji jadra uruchomic ponownie.
set -euo pipefail

KVER=$(uname -r)
TAG=${1:-}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

sudo apt-get install -y build-essential git "linux-headers-$KVER"

git clone -q https://github.com/ikwzm/udmabuf "$WORK/udmabuf"
cd "$WORK/udmabuf"
if [[ -z "$TAG" ]]; then TAG=$(git describe --tags --abbrev=0); fi
git checkout -q "$TAG"
echo "u-dma-buf $TAG dla $KVER"

# Makefile repozytorium buduje dla biezacego jadra; gdyby nie, kbuild wprost
make || make -C "/lib/modules/$KVER/build" M="$PWD" modules
[[ -f u-dma-buf.ko ]] || { echo "brak u-dma-buf.ko po kompilacji" >&2; exit 1; }
sudo install -D -m 644 u-dma-buf.ko "/lib/modules/$KVER/extra/u-dma-buf.ko"
sudo depmod -a
echo u-dma-buf | sudo tee /etc/modules-load.d/u-dma-buf.conf >/dev/null
sudo modprobe u-dma-buf
lsmod | grep -w u_dma_buf && echo "ok: u-dma-buf zaladowany"
