# posit-kv260 (pakiet `posit.kv260`)

Wszystko, co zależy od KV260 i Vivado: rejestry AXI4-Lite (etap 1), AXI4-Stream + DMA (etap 2),
własny master AXI4 (etap 6), konwencje nazw portów dla IP packagera oraz generatory RTL.

| plik | rola |
| --- | --- |
| `Kv260.scala` | oznaczenie układu (`xck26-sfvc784-2LV-c`), zegar PL, `Kv260.spinal` |
| `PositGen.scala` | generacja jednostek posit-core do Verilog/VHDL (`UnitJob`, `DividerJob`) |

```
sbt "posit-kv260/runMain posit.kv260.PositGen"
```
