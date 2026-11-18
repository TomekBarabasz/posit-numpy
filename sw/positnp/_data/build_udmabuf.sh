#!/usr/bin/env bash
# The u-dma-buf kernel module (github.com/ikwzm/udmabuf) on the KV260, once per kernel.
# It gives the posit engines physically contiguous buffers: the overlay's "ikwzm,u-dma-buf" node
# -> /dev/udmabuf-posit-p<n> + /sys/class/u-dma-buf/udmabuf-posit-p<n>/phys_addr.
#
#   positnp-board udmabuf [tag]       (or this script directly; default: the latest tag of the repository)
#
# Installs into /lib/modules/$(uname -r)/extra, runs depmod, loads the module at boot
# (/etc/modules-load.d/u-dma-buf.conf). Run it again after a kernel update.
# Needs network access (git clone), the kernel headers (installed here with apt) and sudo.
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
echo "u-dma-buf $TAG for kernel $KVER"

# the repository's Makefile builds for the running kernel; if it does not, kbuild directly
make || make -C "/lib/modules/$KVER/build" M="$PWD" modules
[[ -f u-dma-buf.ko ]] || { echo "no u-dma-buf.ko after the build" >&2; exit 1; }
sudo install -D -m 644 u-dma-buf.ko "/lib/modules/$KVER/extra/u-dma-buf.ko"
sudo depmod -a
echo u-dma-buf | sudo tee /etc/modules-load.d/u-dma-buf.conf >/dev/null
sudo modprobe u-dma-buf
lsmod | grep -w u_dma_buf && echo "ok: u-dma-buf loaded"
