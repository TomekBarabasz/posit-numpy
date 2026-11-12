# posit-numpy

Biblioteka w stylu NumPy na KV260: tablice positów liczy logika PL wygenerowana
w SpinalHDL, a steruje nią Python w PS (Kria-PYNQ).
Plan: [posit-numpy na KV260: architektura i plan implementacji](https://claude.ai/artifact/JtAzXExTzWmnyTq6d8kmym).

```
hw/                     sbt, SpinalHDL 1.15.0 (z lib/sbt/HwBuild.scala)
  lib/                  submodule fpga-hw-lib: vertebra (testplany, SimEnv)
  posit-core/           pakiet posit         format, dekoder/enkoder, add/sub/mul/div, dzielniki (z posit-spinal)
  posit-vector/         pakiet posit.vector  silnik wektorowy: ALU, strumienie, quire
  posit-kv260/          pakiet posit.kv260   AXI, opakowania IP, generatory RTL
  top-modules/<id>/     pakiet posit.top.<id> top-levele do Vivado (sbt: top-<id>)
sw/                     bindingi Pythona (positnp)
```

# how to's
## klonowanie (hw/lib to submodule: fpga-hw-lib)
```
git clone --recurse-submodules https://github.com/TomekBarabasz/posit-numpy
git config submodule.recurse true
git config push.recurseSubmodules check
```
Istniejący klon: `git submodule update --init`. Przed edycją w `hw/lib`: `git switch main`.

## start container
z katalogu `hw`
```bash
docker run --rm -ti -v .:/workspace -w /workspace -v .\.sbt:/sbt ghcr.io/spinalhdl/docker:master
```

## testy (z `hw/`)
```
sbt test                                         # wszystkie bloki i top-levele (bez testów lib/)
sbt posit-core/test
sbt "posit-core/testOnly *PositUnitTestplan -- -z p8"
sbt "{lib}vertebra/test"                         # projekty biblioteki z prefiksem {lib}
```

Przełączniki symulacji na czas sesji sbt (`lib/sbt/HwBuild.scala`):

| task | działanie |
| --- | --- |
| `wavesOn` / `wavesOff` | FST w `simWorkspace/<nazwa>_<backend>` |
| `backendVrl` / `backendGhdl` | backend symulacji (domyślnie GHDL) |
| `simStatus` | bieżące ustawienia |
| `simClean` | usuwa workspace'y przebiegów i `hw/gen` bloku |

Ze shella: `VERTEBRA_BACKEND=verilator`, `VERTEBRA_WAVES=1`, `SIM_SEED=random|<liczba>`.
`POSIT_SIM_FULL=1` przywraca liczby wektorów z posit-spinal (300k posit16, 100k posit32); domyślnie
zestaw szybki, posit⟨8,*⟩ zawsze wyczerpująco.

## zasady testów
- Test bloku to `class <Dut>Testplan extends TestplanSuite` (vertebra) w `<blok>/hw/spinal/test`:
  plan (`Testpoint`: nazwa, etap V1/V2/V3, `stimulus`, wymagane `checking`) w `object <Dut>Plan`,
  potem `testpoint(nazwa, wariant) { ... }` albo `unimplemented(nazwa, powód)`.
  `testplan completeness` pada, gdy testpoint nie ma testu ani `unimplemented`, albo gdy V1 jest niekompletny.
- Porty `Stream`: `++ StreamConformance.testpoints("<port>")` w planie i checkery z `StreamConformance`.
- Symulacja tylko przez `SimEnv(posit.Conventions.spinal, "<unikalna nazwa>")`: backend i waves z przełączników,
  ten sam reset co w syntezie (synchroniczny, aktywny niskim).
- Ziarno: `doSim(nazwa, seed = SimSeed(nazwa))` i `new Random(SimSeed(nazwa))`.
- Wzorzec: model referencyjny (`PositRef` w `posit-core/hw/spinal/test`), porównanie bit w bit.
  Wspólny sterownik i scoreboard jednostek Stream: `PositSimKit`.

## generacja RTL
```
sbt "posit-kv260/runMain posit.kv260.PositGen"            # -> posit-kv260/hw/gen
sbt "posit-kv260/runMain posit.kv260.PositGen 24 2 mul"
sbt "top-ooc/runMain posit.top.ooc.OocUnits"              # etap 0: jednostki do syntezy OOC
```
