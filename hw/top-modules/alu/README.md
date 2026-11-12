# top-modules/alu (pakiet `posit.top.alu`)

Etap 1: `PositAluAxiLite` (posit-kv260) jako aplikacja PL na KV260, bez PYNQ: na płytce Ubuntu
z `xmutil` (dfx-mgr), rejestry z Pythona przez `/dev/uioN` (`positnp.backend_hw.PositAluHw`).

```
PS (Python, /dev/uioN) --M_AXI_HPM0_FPD 128 b--> smartconnect --AXI4-Lite 32 b--> posit_alu (PositAluAxiLite)
pl_clk0 100 MHz, pl_resetn0 -> proc_sys_reset -> aresetn
```

## Krok po kroku

1. RTL (z `hw/`):
   ```
   sbt "top-alu/runMain posit.top.alu.AluKv260"          # posit<32,2>, dzielnik k = 4 -> hw/gen/p32/
   sbt "top-alu/runMain posit.top.alu.AluKv260 8 2 1"    # posit<8,2> do testu wyczerpującego na płytce
   ```
2. Bitstream i aplikacja (z `hw/top-modules/alu/vivado`, Vivado w PATH):
   ```
   vivado -mode batch -nojournal -log build.log -source build.tcl
   vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -n 8
   ```
   Wynik: katalog `sw/positnp/overlays/posit<n>_alu/`:

   | plik | |
   | --- | --- |
   | `posit<n>_alu.bit.bin` | bitstream dla `fpga_manager` (`bootgen -process_bitstream bin`) |
   | `posit<n>_alu.dtsi` | overlay device tree: region FPGA z bitstreamem, zegar PL0, szerokość HPM0 (AFI), węzeł `generic-uio` |
   | `shell.json` | typ aplikacji dla dfx-mgr (`XRT_FLAT`, cały PL) |
   | `posit<n>_alu.json` | adres i rozmiar IP, zegar, wersja Vivado, WNS, zasoby |

3. Na płytce: skopiować `sw/` (np. `scp -r sw ubuntu@kria:`), potem z `sw/`:
   ```
   sudo apt install device-tree-compiler python3-pytest python3-numpy   # raz
   scripts/install_overlay.sh posit32_alu      # dtc -> .dtbo, /lib/firmware/xilinx/posit32_alu/, xmutil loadapp
   sudo python3 -m pytest -v tests/test_board.py
   ```
   albo z Pythona:
   ```python
   from positnp.backend_hw import PositAluHw
   alu = PositAluHw.open()                         # /dev/uioN o nazwie posit_alu
   alu.op("div", 0x40000000, 0x4C000000)           # (bity 1/3, Flags(inexact=True))
   alu.stats()
   ```
   Powrót do aplikacji startowej: `scripts/install_overlay.sh --unload`, potem `sudo xmutil loadapp k26-starter-kits`.

## build.tcl

| opcja `-tclargs` | domyślnie | |
| --- | --- | --- |
| `-n <bity>` | `32` | RTL z `../hw/gen/p<n>/` |
| `-part <part>` | `xck26-sfvc784-2LV-c` | |
| `-board` | wyłączone | board part KV260 i preset PS (wymaga board files z Vivado Store) |
| `-out <dir>` | `./out` | IP, projekt, raporty, `.bit` i `.hwh` do podglądu |
| `-deploy <dir>` | `sw/positnp/overlays` | gdzie zapisać katalog aplikacji |
| `-jobs <n>` | `8` | równoległe przebiegi (synteza IP z block designu) |
| `-threads <n>` | `8` | wątki Vivado na przebieg (`general.maxThreads`; Vivado domyślnie bierze 2 na Windows) |
| `-stop ip\|bd` | — | zatrzymanie po pakowaniu IP albo po block designie (do obejrzenia w GUI: `out/proj_p<n>`) |

Kolejno: `package_ip.tcl` (IP `newhope.pl:posit:PositAluAxiLite:1.0`, interfejsy `s_axi`, `aclk`,
`aresetn` wnioskowane z nazw portów i sprawdzane), block design, wrapper, synteza i implementacja,
bitstream (skompresowany), `bootgen` (jest w `<Vivado>/bin`), `.dtsi`, `shell.json`, `.json`.
WNS < 0 kończy się błędem bez tworzenia katalogu aplikacji.

## PS i device tree

Bez PYNQ nikt nie czyta `.hwh` i nie konfiguruje PS przy ładowaniu, więc wszystko, czego PL potrzebuje
od PS, jest w overlayu `.dtsi`:

- **zegar**: `clocking0` (`xlnx,fclk`) włącza PL0_REF i ustawia **zadaną** częstotliwość `pl_clk0`
  (`PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ`, 100 000 000 Hz). Nie „faktyczną” z Vivado (`ACT_FREQMHZ`,
  99 999 001 Hz): ta jest liczona dla PLL z block designu, a na płytce PL0 taktuje PLL z firmware Kria
  (1500 MHz) i sterownik wybiera dzielnik nie przekraczający zadanej wartości — 99 999 001 Hz dawało
  1500/16 = 93,75 MHz, 100 MHz daje 1500/15. Sprawdzenie: `sudo cat /sys/kernel/debug/clk/pl0_ref/clk_rate`;
- **reset**: `resets = <&zynqmp_reset 116>` na regionie FPGA (`pl_resetn0`);
- **szerokość HPM0 FPD**: siedzi w rejestrach PS (AFI), nie w bitstreamie. Block design zostawia domyślne
  128 b, a `afi0` (`config-afi = <14 0xa00>`) wpisuje ten sam stan; smartconnect zwęża 128 b do AXI4-Lite 32 b.
  Gdyby port PS miał inną szerokość niż w block designie, rejestry pod offsetami ≠ 0 czytałyby się źle;
- **dostęp z Linuksa**: węzeł `posit_alu@<adres>` z `compatible = "generic-uio"`. Cmdline Kria ma
  `uio_pdrv_genirq.of_id=generic-uio`, więc powstaje `/dev/uioN` o nazwie `posit_alu`.

Konfiguracja DDR i MIO w block designie nie ma znaczenia (nie trafia do bitstreamu PL); `-board` włącza
preset płytki, jeśli board files są zainstalowane.

## Mapa rejestrów

`posit.kv260.PositAluRegs` = `sw/positnp/regs.py` (zgodność sprawdza `PositAluAxiLiteTestplan.regs_python_match`).

| offset | rejestr | |
| --- | --- | --- |
| 0x00 | ID_VERSION | `0x5053_0100`: ID "PS", wersja 1.0 |
| 0x04 | CONFIG | [7:0] n, [11:8] es, [31:16] maska operacji (bit = kod) |
| 0x08 | CTRL | impulsy: bit 0 start, bit 1 miękki reset, bit 2 zerowanie liczników |
| 0x0C | STATUS | bit 0 busy, bit 1 done, bit 2 error, [10:8] flagi ostatniego wyniku (nar, sat, inexact) |
| 0x10 | OP | 0 add, 1 sub, 2 mul, 3 div |
| 0x14, 0x18 | OPERAND_A, OPERAND_B | [n-1:0] |
| 0x1C | RESULT | [n-1:0] |
| 0x40–0x4C | CNT_NAR, CNT_SAT, CNT_INEXACT, CYCLES | liczniki od ostatniego zerowania |

- Start w trakcie pracy jest ignorowany; argumenty są zatrzaskiwane przy starcie.
- Kod spoza maski CONFIG: STATUS = done + error, bez liczenia.
- Miękki reset przerywa dzielenie (ALU w `ResetArea`) i zeruje busy/done/error.
- Dzielenie iteracyjne: `divSteps / k + 2` taktów etapu operacji; CYCLES pokazuje to wprost.

## Weryfikacja przed płytką

| poziom | gdzie | |
| --- | --- | --- |
| ALU | `posit-vector/.../PositAluTestplan` | wynik i flagi bit w bit z PositRef: posit8 wyczerpująco, posit16/32 losowo, latencja dzielenia |
| rejestry | `posit-kv260/.../PositAluAxiLiteTestplan` | sterownik AXI4-Lite, ta sama sekwencja co `backend_hw.py`; nazwy portów dla IP packagera |
| model | `sw/tests/test_ref.py` + `RefDigestTestplan` | `ref.py` i `PositRef.scala` dają te same skróty SHA-256 (`sw/tests/ref_digests.txt`) |
| sterownik | `sw/tests/test_backend_fake.py` | `PositAluHw` na atrapie rejestrów (`FakeAluMmio`) |
| płytka | `sw/tests/test_board.py` | UIO kontra `ref.py`, liczniki, czas jednego `op()` |

Synteza samego IP poza kontekstem: `top-modules/ooc` (katalogi `alu_p16`, `alu_p32`).
