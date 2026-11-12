# posit-core (pakiet `posit`)

Generator jednostek arytmetyki posit, przeniesiony z posit-spinal. Opis architektury,
dzielników i wyników weryfikacji: [posit-spinal](https://claude.ai/artifact/JXzBwQ1AMsoGar5KTsHSJ9).

Zmiany względem posit-spinal:
- układ `hw/spinal/{main,test}` (HwBuild.hwLayout), testy na vertebrze: `PositUnitTestplan`
  zamiast `PositUnitSim` (App), wspólny sterownik i scoreboard w `PositSimKit`;
- `Conventions.spinal`: reset synchroniczny aktywny niskim (Vivado), 100 MHz, `hw/gen`;
  posit-spinal używał domyślnego SpinalConfig (asynchroniczny, aktywny wysokim);
- `PositGen` przeniesiony do `posit.kv260` (generatory są w posit-kv260).

```
sbt posit-core/test
sbt "posit-core/testOnly *PositUnitTestplan -- -z div"
POSIT_SIM_FULL=1 VERTEBRA_BACKEND=verilator sbt posit-core/test   # pełne liczby wektorów
```
