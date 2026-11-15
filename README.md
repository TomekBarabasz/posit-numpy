# posit-numpy

Biblioteka w stylu NumPy na KV260: tablice positów liczy logika PL wygenerowana
w SpinalHDL, a steruje nią Python w PS (Ubuntu z `xmutil`, rejestry przez UIO; bez PYNQ).
Plan: [posit-numpy na KV260: architektura i plan implementacji](https://claude.ai/artifact/JtAzXExTzWmnyTq6d8kmym).

```
hw/                     sbt, SpinalHDL 1.15.0 (z lib/sbt/HwBuild.scala)
  lib/                  submodule fpga-hw-lib: vertebra (testplany, SimEnv)
  posit-core/           pakiet posit         format, dekoder/enkoder, add/sub/mul/div, dzielniki (z posit-spinal)
  posit-vector/         pakiet posit.vector  silnik wektorowy: ALU, strumienie, quire
  posit-kv260/          pakiet posit.kv260   AXI, opakowania IP, generatory RTL
  top-modules/<id>/     pakiet posit.top.<id> top-levele do Vivado (sbt: top-<id>)
sw/                     bindingi Pythona (positnp): ref.py, regs.py, backend_hw.py
```

Stan:
- etap 1 (ALU za AXI4-Lite, wywołanie z Pythona): sprawdzony na płytce — `hw/top-modules/alu/README.md`;
- etap 2 (tablice przez AXI4-Stream i AXI DMA): sprawdzony na płytce — `hw/top-modules/vec/README.md`;
- etap 3 (`PositArray` z protokołami NumPy, `vecops`, konwersje, `stats()`, notatnik): sprawdzony na płytce — `sw/README.md`;
- etap 4 (quire: dokładne `sum`, `dot`, `mean`, `norm`): sprawdzony na płytce — `hw/top-modules/vec/README.md`;
- etap 5 (sqrt, FMA / AXPY, konwersje float32 w PL; `np.sqrt`, `pn.fma`, `pn.axpy`): sprawdzony na płytce — `hw/top-modules/vec/README.md`;
- kilka silników w jednym bitstreamie (posit8/16/32), tory div / sqrt / FMA, `pn.board`: sprawdzone na płytce
  (`posit32_16_8_vec`) — `hw/top-modules/vec/README.md`, `sw/README.md`;
- etap 6 (własny master AXI4 zamiast AXI DMA, FMA3 na trzech tablicach, przerwanie): sprawdzony na płytce
  (`posit32_16_8_mem`, add posit32 59,5 M/s wobec 25,4 z AXI DMA; z podwójnym buforem ok. 92 M/s) —
  `hw/top-modules/vec/README.md`;
- szerszy silnik (L elementów na takt: posit32 ×2, posit16 ×4, posit8 ×8): sprawdzony na płytce — add 121 / 229 / 400 M/s,
  posit8 szybsze niż NumPy float32 — `hw/top-modules/vec/README.md`;
- tablice bez kopii (pamięć urządzenia, `x.to_device()`): sprawdzone na płytce — add 147 / 297 / 538 M/s,
  posit16 i posit8 szybsze niż NumPy float32; wentylator sterowany z Linuksa jak w aplikacji fabrycznej.
- etap 7a (dokładny `A @ B` / `np.matmul` na quire: OP_XLOAD + OP_GEMV, dwa quire na zmianę): sprawdzony na płytce —
  0,999 MAC na takt, ok. 98 M MAC/s na silnik (posit32, posit16, posit8), każdy element bit w bit z quire — `hw/top-modules/vec/README.md`;
- szybszy matmul (5.2: P = 4 kolumny B na jedno przejście A, 8 quire): sprawdzony na płytce — 3,98 MAC na takt,
  ok. 388 M MAC/s na silnik (4 × więcej niż z jedną kolumną); OOC posit32 35 119 LUT, 103,6 MHz;
- funkcje elementarne (5.3: `np.exp`, `np.log`, `np.sin`, `np.tanh`, `pn.sigmoid`, … — 21 funkcji): posit8 / posit16 z poprawnie
  zaokrąglonych tablic w BRAM silnika (OP_TABLE, do L elementów na takt), posit32 z float64 w programie; sprawdzone
  na płytce — `exp` posit16 332 M/s, posit8 648 M/s (NumPy float32 `np.exp` na A53: 23 M/s);
- FIR / splot (5.4: `np.convolve`, `np.correlate`, dokładnie w quire): gemv z oknem przesuwanym po sygnale — sygnał
  czytany raz, P wyjść na wiersz; w symulacji 3,76 MAC na takt (61 odczepów, P = 4), czeka na płytkę.

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

## Python (z `sw/`)
```
pip install -e .[test]
python3 -m pytest tests          # ref.py kontra skróty ze Scali, sterownik na atrapie; test_board.py tylko na płytce
```

## etap 1 na płytce (bez PYNQ: Ubuntu + xmutil + UIO)
```
sbt "top-alu/runMain posit.top.alu.AluKv260"     # z hw/
vivado -mode batch -nojournal -log build.log -source build.tcl    # z hw/top-modules/alu/vivado
scripts/install_overlay.sh posit32_alu           # na KV260, z sw/
sudo python3 -m pytest -v tests/test_board.py    # na KV260, z sw/
```

## silniki na tablicach na płytce (etap 2+)
Jeden albo kilka formatów w jednym bitstreamie (`-engines`, szczegóły w `hw/top-modules/vec/README.md`):
```
sbt "top-vec/runMain posit.top.vec.VecKv260 32 16 8"                                  # z hw/
vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -engines "32 16 8"   # z hw/top-modules/vec/vivado
scripts/build_udmabuf.sh                         # na KV260, z sw/, raz na jądro (po aktualizacji jądra: znowu)
scripts/install_overlay.sh posit32_16_8_mem      # na KV260, z sw/ (etap 6; z -io dma: posit32_16_8_vec)
sudo python3 -m pytest -v -s tests/test_board_vec.py
```
Na płytce roboczej najlepiej wyłączyć automatyczne aktualizacje Ubuntu: podmiana jądra, `systemd` i Pythona w trakcie
testów zawiesiła raz płytkę, a nowe jądro po restarcie nie ma modułu u-dma-buf
(`sudo systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer`; aktualizacje ręcznie,
potem `scripts/build_udmabuf.sh`). Po twardym resecie w trakcie aktualizacji: `sudo dpkg --configure -a`.
Z Pythona (`pn.board`): `pn.board.apps()`, `pn.board.load("posit32_16_8_mem")` (każdy silnik staje się
backendem swojego formatu), `pn.board.describe()`, `pn.board.unload()`; opis w `sw/README.md`.

## synteza OOC (etap 0)
z `hw/top-modules/ooc/vivado`, opis opcji i metody: `hw/top-modules/ooc/README.md`
```
vivado -mode batch -nojournal -log vivado.log -source ooc_synth.tcl -tclargs -impl
```
