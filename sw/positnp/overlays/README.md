# overlays

Katalogi aplikacji PL dla `xmutil` (dfx-mgr), po jednym na format i etap: `posit32_alu/` (etap 1),
`posit32_vec/` (etap 2), `posit8_alu/`, ...

| plik | |
| --- | --- |
| `<nazwa>.bit.bin` | bitstream dla `fpga_manager` |
| `<nazwa>.dtsi` | overlay device tree (na płytce: `dtc` -> `.dtbo`) |
| `shell.json` | typ aplikacji dla dfx-mgr |
| `<nazwa>.json` | adres i rozmiar IP, zegar, wersja Vivado, WNS |

Powstają w `hw/top-modules/alu/vivado/build.tcl`; na płytce instaluje je `sw/scripts/install_overlay.sh <nazwa>`
(do `/lib/firmware/xilinx/<nazwa>/` i `xmutil loadapp`). Pliki są poza gitem (`.gitignore`).
