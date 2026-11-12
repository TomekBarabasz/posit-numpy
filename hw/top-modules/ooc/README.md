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

### ALU z etapu 1, sama synteza (2026-11-12)

Vivado 2026.1, `xck26-sfvc784-2LV-c`, 100 MHz, `-only "alu_*/*"`. `PositAluAxiLite` z dzielnikiem k = 4
i potokiem full, razem z rejestrami AXI4-Lite i licznikami.

| wariant | LUT | FF | CARRY8 | DSP | BRAM | WNS ns | Fmax MHz (synteza) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `alu_p16` | 776 | 430 | 36 | 1 | 0 | 3.926 | 164.6 |
| `alu_p32` | 1 474 | 676 | 70 | 4 | 0 | 2.450 | 132.5 |

- Obie konfiguracje mają zapas przy 100 MHz; po samej syntezie opóźnienia połączeń są szacowane, wiążące będą liczby z `-impl`.
- DSP zgadzają się z mnożarką znaczących: 15 × 15 b mieści się w jednym DSP48E2 (27 × 18), 29 × 29 b potrzebuje czterech.
- ALU posit32 zajmuje ok. 1,3% LUT układu.

Jednostki posit-core (`comb`, `op`, `full`, `div`): do uzupełnienia.
