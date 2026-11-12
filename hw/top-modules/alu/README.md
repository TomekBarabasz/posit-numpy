# top-modules/alu (pakiet `posit.top.alu`)

Etap 1: `PositAluAxiLite` (posit-kv260) jako overlay PYNQ na KV260. Dane idą przez rejestry
AXI4-Lite, po jednej parze argumentów; z Pythona wywołuje się to przez `positnp.backend_hw.PositAluHw`.

```
PS (Python) --M_AXI_HPM0_FPD--> smartconnect --AXI4-Lite--> posit_alu (PositAluAxiLite)
pl_clk0 100 MHz, pl_resetn0 -> proc_sys_reset -> aresetn
```

## Krok po kroku

1. RTL (z `hw/`):
   ```
   sbt "top-alu/runMain posit.top.alu.AluKv260"          # posit<32,2>, dzielnik k = 4 -> hw/gen/p32/
   sbt "top-alu/runMain posit.top.alu.AluKv260 8 2 1"    # posit<8,2> do testu wyczerpującego na płytce
   ```
2. Bitstream (z `hw/top-modules/alu/vivado`):
   ```
   vivado -mode batch -nojournal -log build.log -source build.tcl
   vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -n 8
   ```
   Wynik: `sw/positnp/overlays/posit<n>_alu.{bit,hwh,txt}` (`.txt`: wersja Vivado, WNS, zasoby).
3. Na płytce (Kria-PYNQ): skopiować `sw/` i
   ```
   sudo -E python3 -m pytest -v sw/tests/test_board.py
   POSITNP_BITFILE=sw/positnp/overlays/posit8_alu.bit sudo -E python3 -m pytest -v sw/tests/test_board.py
   ```
   albo w Jupyterze:
   ```python
   from positnp.backend_hw import PositAluHw
   alu = PositAluHw.from_overlay()                 # overlays/posit32_alu.bit
   alu.op("div", 0x40000000, 0x4C000000)           # (bity 1/3, Flags(inexact=True))
   alu.stats()
   ```

## build.tcl

| opcja `-tclargs` | domyślnie | |
| --- | --- | --- |
| `-n <bity>` | `32` | RTL z `../hw/gen/p<n>/` |
| `-part <part>` | `xck26-sfvc784-2LV-c` | |
| `-board` | wyłączone | board part KV260 i preset PS (wymaga board files z Vivado Store) |
| `-out <dir>` | `./out` | IP, projekt, raporty |
| `-deploy <dir>` | `sw/positnp/overlays` | gdzie skopiować `.bit` + `.hwh` |
| `-jobs <n>` | `4` | |
| `-stop ip\|bd` | — | zatrzymanie po pakowaniu IP albo po block designie (do obejrzenia w GUI: `out/proj_p<n>`) |

Kolejno: `package_ip.tcl` (IP `newhope.pl:posit:PositAluAxiLite:1.0`, interfejsy `s_axi`, `aclk`,
`aresetn` wnioskowane z nazw portów i sprawdzane), block design, wrapper, synteza i implementacja,
bitstream (skompresowany), kopia `.bit` + `.hwh` pod wspólną nazwą (PYNQ szuka `.hwh` obok `.bit`).
WNS < 0 kończy się błędem bez kopiowania.

PS jest skonfigurowany minimalnie (HPM0 FPD 32 b, `pl_clk0` 100 MHz, `pl_resetn0`). Konfiguracja DDR i MIO
nie trafia do bitstreamu PL, a PYNQ bierze z `.hwh` tylko zegary i adresy, więc preset płytki nie jest potrzebny;
`-board` go włącza, jeśli board files są zainstalowane.

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
| płytka | `sw/tests/test_board.py` | MMIO kontra `ref.py`, liczniki, czas jednego `op()` |

Synteza samego IP poza kontekstem: `top-modules/ooc` (katalogi `alu_p16`, `alu_p32`).
