# posit-vector (pakiet `posit.vector`)

Silnik wektorowy, niezależny od magistral (Stream/Flow z spinal.lib); opakowania AXI są w `posit.kv260`.
Plan: PositStreamAlu (etap 2), quire i redukcje (etap 4), sqrt/FMA/konwersje (etap 5).

| plik | rola |
| --- | --- |
| `PositStreamAlu.scala` | etap 2: tablice przez `Stream(Fragment(a, b))` i ścieżka rejestrowa na tym samym ALU (arbiter, FIFO znaczników źródło/last), tablica ⊕ skalar; 1 element / takt dla add/sub/mul |
| `PositAlu.scala` | etap 1: `cmd(op, a, b) -> rsp(bits, flags)`; wspólny dekoder i enkoder z flagami, add/sub na jednym sumatorze, mul, dzielnik iteracyjny (k bitów / takt), `PositAluOp` (kody), `PositAluConfig` |

```
sbt posit-vector/test            # PositAluTestplan, PositStreamAluTestplan
```
Testy widzą `PositRef` i `PositSimKit` z posit-core (test->test).
