# top-modules

Top-levele do Vivado: jeden katalog na produkt (odpowiednik `demo/` w voice-changer).
Każdy to osobny projekt sbt `top-<id>` z pakietem `posit.top.<id>` i układem
`hw/spinal/{main,test}`; generacja ląduje w `top-modules/<id>/hw/gen`.

Nowy top-level: katalog `top-modules/<id>`, jedna linijka w `hw/build.sbt`
```scala
lazy val topAlu = topModule("alu")
```
i dopisanie go do `aggregate` w `root`. Top widzi `posit-kv260` (a przez niego
`posit-core` i `posit-vector`), w testach też ich `PositSimKit` i `PositRef`.
Obok kodu: `vivado/` (skrypty Tcl, block design) i `README.md` z wynikami syntezy.

| top | etap | co |
| --- | --- | --- |
| `ooc` | 0 | jednostki posit-core i ALU do syntezy OOC na xck26 (LUT/FF/DSP/Fmax per potok), `vivado/ooc_synth.tcl` |
| `alu` | 1 | ALU za AXI4-Lite jako aplikacja PL dla `xmutil`: IP, block design, `.bit.bin` + `.dtsi` (`vivado/build.tcl`) |
| `vec` | 2 | silnik na tablicach (`PositVecAxi`) z AXI DMA i buforem u-dma-buf: `vivado/build.tcl` |
| `common` | — | `vivado/package_ip.tcl`: pakowanie IP z posit-kv260, wspólne dla wszystkich top-leveli |
