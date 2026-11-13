# top-modules/ooc (pakiet `posit.top.ooc`)

Etap 0: jednostki posit-core w posit⟨16,2⟩ i posit⟨32,2⟩ do syntezy poza kontekstem (OOC) na
`xck26-sfvc784-2LV-c`: tabela LUT / FF / CARRY8 / DSP / BRAM / Fmax dla każdego ustawienia potoku.

| katalog `hw/gen/` | co |
| --- | --- |
| `comb/` | `PositPipeline.combinational`: add, sub, mul, div (dzielenie rozwinięte w jeden łańcuch) |
| `op/` | `PositPipeline()`: rejestr po operacji (domyślny) |
| `full/` | `PositPipeline.full`: rejestry po dekoderze, operacji i enkoderze |
| `div/` | dzielniki sekwencyjne: `Iterative(1)`, `Iterative(4)`, `Pipelined(4)` |
| `alu_p16/`, `alu_p32/` | `PositAluAxiLite` z etapu 1 (k = 4, potok full), ta sama konfiguracja co `top-modules/alu` |
| `vec_p16/`, `vec_p32/` | `PositVecAxi` (rejestry, AXI4-Stream, quire, sqrt / FMA / konwersje), ta sama konfiguracja co `top-modules/vec` |

Każdy wariant potoku jest w osobnym katalogu, bo nazwa modułu (`PositAdd_16_2`) nie zawiera potoku.

## Uruchomienie

1. RTL (z `hw/`, np. w kontenerze SpinalHDL):
   ```
   sbt "top-ooc/runMain posit.top.ooc.OocUnits"
   ```
2. Synteza (z `hw/top-modules/ooc/vivado`, Vivado w PATH):
   ```
   vivado -mode batch -nojournal -log vivado.log -source ooc_synth.tcl
   vivado -mode batch -nojournal -log vivado.log -source ooc_synth.tcl -tclargs -impl
   vivado -mode batch -nojournal -log vivado.log -source ooc_synth.tcl -tclargs -only "*/PositMul_32_2" -period 5
   ```

| opcja `-tclargs` | domyślnie | |
| --- | --- | --- |
| `-gen <dir>` | `../hw/gen` | katalog z RTL |
| `-out <dir>` | `./out` | wyniki |
| `-part <part>` | `xck26-sfvc784-2LV-c` | |
| `-period <ns>` | `10.0` | okres zegara (100 MHz) |
| `-only <glob>` | `*` | filtr `<wariant>/<moduł>`, np. `div/*`, `*_32_2` |
| `-impl` | wyłączone | po syntezie `opt_design`, `place_design`, `route_design` i drugi wiersz z wynikiem po routingu |
| `-threads <n>` | `8` | wątki Vivado (`general.maxThreads`; Vivado domyślnie bierze 2 na Windows) |

Wyniki w `out/` (poza gitem):
- `ooc_results.csv`: wiersz na jednostkę i etap (`synth`, `impl`), dopisywany po każdej jednostce, więc przerwany przebieg zostawia to, co już policzył;
- `ooc_results.md`: ta sama tabela w markdownie, do wklejenia poniżej;
- `<wariant>/<moduł>/`: `ooc.xdc`, `utilization_*.rpt`, `timing_*.rpt`.

Błąd jednej jednostki daje wiersz `BLAD` i przebieg idzie dalej (szczegóły w `vivado.log`).

## Metoda

- Synteza w trybie non-project, `synth_design -mode out_of_context` (bez buforów I/O), każda jednostka w osobnym projekcie w pamięci.
- Zegar `clk` o okresie `-period`. Jednostki `comb` nie mają portu `clk` (SpinalHDL go nie generuje), więc dostają zegar wirtualny.
- `set_input_delay` / `set_output_delay` 0 względem `clk` na wszystkich portach: ścieżki port → rejestr,
  rejestr → port i port → port mają cały okres, tak jakby jednostkę otaczały rejestry. Clk-to-q i setup tych
  rejestrów są pominięte, więc Fmax wariantu `comb` wychodzi lekko optymistycznie.
- Fmax = 1000 / (okres − WNS). Po samej syntezie opóźnienia połączeń są szacowane; wiążące są liczby z `-impl`.
- LUT, FF, CARRY8, DSP i BRAM pochodzą z wierszy `report_utilization`. Gdy wiersza brakuje, skrypt liczy komórki po `REF_NAME`.

## Wyniki

### ALU z etapu 1 (2026-11-12)

Vivado 2026.1, `xck26-sfvc784-2LV-c`, 100 MHz, `-impl -only "alu_*/*"`. `PositAluAxiLite` z dzielnikiem k = 4
i potokiem full, razem z rejestrami AXI4-Lite i licznikami.

| wariant | etap | LUT | FF | CARRY8 | DSP | BRAM | WNS ns | Fmax MHz |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `alu_p16` | synth | 776 | 430 | 36 | 1 | 0 | 3.926 | 164.6 |
| `alu_p16` | impl | 770 | 430 | 36 | 1 | 0 | 3.404 | 151.6 |
| `alu_p32` | synth | 1 474 | 676 | 70 | 4 | 0 | 2.450 | 132.5 |
| `alu_p32` | impl | 1 447 | 676 | 70 | 4 | 0 | 1.691 | 120.4 |

- Po routingu obie konfiguracje domykają 100 MHz: posit32 z zapasem 1,7 ns (120 MHz), posit16 3,4 ns (152 MHz).
  Synteza przeszacowała Fmax o ok. 8–10%.
- DSP zgadzają się z mnożarką znaczących (1.ułamek): 12 × 12 b mieści się w jednym DSP48E2 (27 × 18), 28 × 28 b potrzebuje czterech.
- ALU posit32 zajmuje ok. 1,2% LUT układu.
- Ostrzeżenia `Synth 8-7129` (porty bez obciążenia: `s_axi_awaddr[1:0]`, `*prot`, `wstrb`, w posit16 `wdata[31:16]`)
  są oczekiwane: rejestry są wyrównane do słowa, bity ochrony nieużywane, `AxiLite4SlaveFactory` ignoruje
  `wstrb` (`backend_hw` zapisuje zawsze pełne słowo 32 b).

### Silnik z etapu 4: `PositVecAxi` 3.0 z quire (2026-11-13)

Vivado 2026.1, 100 MHz, `-impl -only "vec_*/*"`. ALU, ścieżka strumieniowa i quire (16 n bitów) razem z rejestrami AXI4-Lite.

| wariant | etap | LUT | FF | CARRY8 | DSP | BRAM | WNS ns | Fmax MHz |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `vec_p16` | synth | 2 790 | 1 641 | 106 | 2 | 0 | 2,884 | 140,5 |
| `vec_p16` | impl | 2 762 | 1 641 | 106 | 2 | 0 | 2,673 | 136,5 |
| `vec_p32` | synth | 5 143 | 3 010 | 226 | 8 | 0 | 1,694 | 120,4 |
| `vec_p32` | impl | 5 090 | 3 010 | 226 | 8 | 0 | 1,816 | 122,2 |

- Quire nie pogorszył timingu: `vec_p32` ma po routingu 122 MHz, tyle co samo ALU z etapu 1 (`alu_p32`: 120 MHz).
  Segmenty 64 b z rejestrami przeniesień zdjęły z ścieżki krytycznej sumator 512 b, a przesuwnik wyrównujący
  (~570 b) i odczyt (wybór segmentu, LZC 64 b) mieszczą się w jednym takcie.
- Koszt quire posit32 względem `alu_p32`: ok. +3 600 LUT, +2 300 FF (dwa rejestry 512 b: akumulator i odczyt,
  plus potok) i +4 DSP (mnożarka 28 × 28 b w quire, osobna od ALU). Cały silnik to ok. 4,3% LUT układu.

### Etap 5, przebieg kontrolny: `PositVecAxi` 4.0 bez nowych operacji (2026-11-13)

Do tego przebiegu `OocUnits` generował `vec_p*` jeszcze bez `sqrt / fma / convert`, więc tabela pokazuje
silnik z etapu 4 po przebudowie ALU (wspólna ścieżka iteracyjna, wynik surowy dla P2F), a nie koszt etapu 5.
Wyniki z pełną konfiguracją są wyżej.

| wariant | etap | LUT | FF | CARRY8 | DSP | BRAM | WNS ns | Fmax MHz |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `vec_p16` | synth | 2 792 | 1 657 | 106 | 2 | 0 | 2,907 | 141,0 |
| `vec_p16` | impl | 2 759 | 1 657 | 106 | 2 | 0 | 2,783 | 138,6 |
| `vec_p32` | synth | 5 062 | 3 036 | 226 | 8 | 0 | 1,859 | 122,8 |
| `vec_p32` | impl | 5 009 | 3 036 | 226 | 8 | 0 | 1,146 | 112,9 |

- Zasoby jak w etapie 4 (±2%), czyli przebudowa ALU nic nie kosztuje, gdy nowe operacje są wyłączone.
- `vec_p32` po routingu 113 MHz (było 122) przy lepszej syntezie (123 vs 120 MHz): to rozrzut place & route
  na tej samej logice, nie nowa ścieżka. Zapas przy 100 MHz nadal 1,1 ns.

### Silnik z etapu 5: `PositVecAxi` 4.0 z sqrt, FMA / AXPY i konwersjami (2026-11-13)

Vivado 2026.1, 100 MHz, `-impl -only "vec_*/*"`, konfiguracja jak bitstream (`VecKv260.config`; konwersje tylko w posit32).

| wariant | etap | LUT | FF | CARRY8 | DSP | BRAM | WNS ns | Fmax MHz |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `vec_p16` | synth | 3 350 | 1 828 | 133 | 2 | 0 | 1,891 | 123,3 |
| `vec_p16` | impl | 3 346 | 1 828 | 133 | 2 | 0 | 0,415 | 104,3 |
| `vec_p32` | synth | 6 517 | 3 423 | 278 | 8 | 0 | 0,326 | 103,4 |
| `vec_p32` | impl | 6 550 | 3 423 | 278 | 8 | 0 | 0,516 | 105,4 |

- Koszt etapu 5 w posit32 (względem przebiegu kontrolnego niżej): +1 540 LUT (+31%), +390 FF, +52 CARRY8, 0 DSP.
- Timing: 100 MHz trzymane, ale zapas spadł do ok. 0,5 ns (było 1,1–1,8 ns), w posit16 ze 139 do 104 MHz —
  nowa ścieżka krytyczna weszła razem z sqrt / FMA. Do ustalenia z `out/vec_p*/PositVecAxi/timing_impl.rpt`.

### Etap 5, przebieg kontrolny: `PositVecAxi` 4.0 bez nowych operacji (2026-11-13)

Vivado 2026.1, `xck26-sfvc784-2LV-c`, 100 MHz, `-impl` (cały zestaw z `OocUnits`, bez `vec_p*`).
W tabeli wyniki po routingu, w ostatniej kolumnie Fmax z samej syntezy; komplet w `out/ooc_results.csv`.

| wariant | moduł | LUT | FF | CARRY8 | DSP | WNS ns | Fmax MHz | 100 MHz | Fmax po syntezie |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: |
| `comb` | `PositAdd_16_2` | 478 | 0 | 7 | 0 | 0,238 | 102,4 | ✓ | 84,1 |
| `comb` | `PositAdd_32_2` | 999 | 0 | 26 | 0 | -4,050 | 71,2 | ✗ | 71,0 |
| `comb` | `PositDiv_16_2` | 501 | 0 | 29 | 0 | -8,600 | 53,8 | ✗ | 52,5 |
| `comb` | `PositDiv_32_2` | 2839 | 0 | 136 | 0 | -47,812 | 17,3 | ✗ | 21,0 |
| `comb` | `PositMul_16_2` | 305 | 0 | 2 | 1 | 0,204 | 102,1 | ✓ | 101,7 |
| `comb` | `PositMul_32_2` | 781 | 0 | 23 | 4 | -3,456 | 74,3 | ✗ | 79,6 |
| `comb` | `PositSub_16_2` | 465 | 0 | 7 | 0 | -0,020 | 99,8 | ✗ | 84,9 |
| `comb` | `PositSub_32_2` | 996 | 0 | 26 | 0 | -4,236 | 70,2 | ✗ | 72,0 |
| `op` | `PositAdd_16_2` | 359 | 27 | 7 | 0 | 0,467 | 104,9 | ✓ | 112,6 |
| `op` | `PositAdd_32_2` | 971 | 44 | 27 | 0 | 0,258 | 102,6 | ✓ | 91,6 |
| `op` | `PositDiv_16_2` | 500 | 26 | 29 | 0 | -4,957 | 66,9 | ✗ | 61,5 |
| `op` | `PositDiv_32_2` | 2848 | 43 | 136 | 0 | -34,948 | 22,2 | ✗ | 22,1 |
| `op` | `PositMul_16_2` | 270 | 36 | 2 | 1 | 2,464 | 132,7 | ✓ | 163,1 |
| `op` | `PositMul_32_2` | 707 | 69 | 23 | 4 | 0,462 | 104,8 | ✓ | 116,8 |
| `op` | `PositSub_16_2` | 382 | 27 | 7 | 0 | 1,299 | 114,9 | ✓ | 109,6 |
| `op` | `PositSub_32_2` | 1013 | 44 | 27 | 0 | 0,147 | 101,5 | ✓ | 92,7 |
| `full` | `PositAdd_16_2` | 368 | 87 | 7 | 0 | 3,303 | 149,3 | ✓ | 153,1 |
| `full` | `PositAdd_32_2` | 849 | 154 | 27 | 0 | 1,640 | 119,6 | ✓ | 134,3 |
| `full` | `PositDiv_16_2` | 470 | 86 | 29 | 0 | -2,303 | 81,3 | ✗ | 75,3 |
| `full` | `PositDiv_32_2` | 1911 | 153 | 136 | 0 | -23,708 | 29,7 | ✗ | 30,3 |
| `full` | `PositMul_16_2` | 298 | 96 | 2 | 1 | 5,934 | 245,9 | ✓ | 312,9 |
| `full` | `PositMul_32_2` | 690 | 146 | 23 | 4 | 3,742 | 159,8 | ✓ | 205,8 |
| `full` | `PositSub_16_2` | 367 | 87 | 7 | 0 | 3,102 | 145,0 | ✓ | 153,1 |
| `full` | `PositSub_32_2` | 848 | 154 | 27 | 0 | 1,687 | 120,3 | ✓ | 134,9 |
| `div` | `PositDivIter1_16_2` | 311 | 54 | 5 | 0 | 4,985 | 199,4 | ✓ | 253,2 |
| `div` | `PositDivIter1_32_2` | 671 | 104 | 24 | 0 | 3,631 | 157,0 | ✓ | 207,3 |
| `div` | `PositDivIter4_16_2` | 344 | 55 | 11 | 0 | 4,653 | 187,0 | ✓ | 245,3 |
| `div` | `PositDivIter4_32_2` | 524 | 42 | 20 | 0 | 3,749 | 160,0 | ✓ | 209,4 |
| `div` | `PositDivPipe4_16_2` | 554 | 169 | 35 | 0 | 1,743 | 121,1 | ✓ | 133,6 |
| `div` | `PositDivPipe4_32_2` | 2017 | 661 | 148 | 0 | 0,163 | 101,7 | ✓ | 110,4 |

**Wnioski (decyzje etapu 0):**

- **Domyślny potok: `PositPipeline.full`.** Add, sub i mul domykają 100 MHz w obu formatach z zapasem
  (posit32: add/sub 120 MHz, mul 160 MHz). `op` (rejestr tylko po operacji) dla posit32 add/sub mieści się
  o włos (WNS 0,15–0,26 ns), a `comb` nie domyka nic poza posit16 mul i add. Tak skonfigurowane jest już ALU
  z etapów 1–2 (`alu_p32`: 120 MHz).
- **Dzielenie kombinacyjne odpada w każdym potoku**: posit32 17–30 MHz, posit16 54–81 MHz. W silniku zostaje
  dzielnik sekwencyjny.
- **Dzielniki sekwencyjne:** `Iterative(1)` i `Iterative(4)` dla posit32 mają ok. 160 MHz przy ~520–670 LUT;
  przy 100 MHz zostaje miejsce na więcej bitów ilorazu na takt (krótsze dzielenie niż obecne 10 taktów).
  `Pipelined(4)` daje 1 dzielenie / takt, ale dla posit32 domyka 100 MHz ledwo (WNS 0,16 ns, ~2 000 LUT, 661 FF):
  jeśli dzielenie na tablicach ma być szybsze, to raczej potok z mniejszym `k` (2–3 bity na etap).
- **Synteza zawyża Fmax zwykle o ok. 10–30%** względem wyniku po routingu, a przy ścieżkach bliskich granicy potrafi
  pomylić się w obie strony (posit16 add w `comb`: −1,9 ns po syntezie, +0,24 ns po routingu). Decyzje
  podejmować na liczbach z `-impl`.
- **Zasoby są pomijalne:** największa jednostka posit32 (dzielnik kombinacyjny) to ~2 800 LUT, ok. 2,4% układu;
  mnożenie posit32 bierze 4 DSP z 1 248.
