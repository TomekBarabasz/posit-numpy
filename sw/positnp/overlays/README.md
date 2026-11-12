# overlays

Pary `.bit` + `.hwh` o tej samej nazwie (PYNQ czyta `.hwh` obok `.bit`), jedna na format:
`posit32_alu.bit`, `posit32_alu.hwh`, `posit8_alu.*`, ...

Powstają w `hw/top-modules/alu/vivado/build.tcl`, który kopiuje je tutaj.
Pliki binarne są poza gitem (`.gitignore`); na płytkę kopiuje się cały katalog `sw/`.
