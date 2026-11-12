# posit-vector (pakiet `posit.vector`)

Silnik wektorowy, niezależny od magistral (Stream/Flow z spinal.lib); opakowania AXI są w `posit.kv260`.
Plan: PositStreamAlu (etap 2), quire i redukcje (etap 4), sqrt/FMA/konwersje (etap 5).

| plik | rola |
| --- | --- |
| `PositAlu.scala` | etap 1: `cmd(op, a, b) -> rsp(bits, flags)`; wspólny dekoder i enkoder z flagami, add/sub na jednym sumatorze, mul, dzielnik iteracyjny (k bitów / takt), `PositAluOp` (kody), `PositAluConfig` |

```
sbt posit-vector/test            # PositAluTestplan
```
Testy widzą `PositRef` i `PositSimKit` z posit-core (test->test).
