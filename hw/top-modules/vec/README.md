# top-modules/vec (pakiet `posit.top.vec`)

Etap 2: działania na całych tablicach. `PositVecAxi` (posit-kv260) dostaje strumień par argumentów
z AXI DMA i oddaje strumień wyników; kod operacji i tryb są w rejestrach z etapu 1, które dalej
obsługują też pojedyncze działania. Bez PYNQ: Ubuntu + `xmutil`, rejestry przez `/dev/uioN`,
bufory DMA przez moduł u-dma-buf.

```
PS --HPM0 FPD--> smartconnect_ctl --+--> posit_vec/s_axi   (rejestry, AXI4-Lite)
                                    +--> posit_dma/S_AXI_LITE
DDR <--HP0 FPD-- smartconnect_data <--+-- posit_dma/M_AXI_MM2S --M_AXIS_MM2S {b, a} 2n b--> posit_vec/s_axis
                                      +-- posit_dma/M_AXI_S2MM <--S_AXIS_S2MM wynik n b--- posit_vec/m_axis
pl_clk0 100 MHz dla wszystkiego
```

## Format strumienia

| port | słowo | |
| --- | --- | --- |
| `s_axis` (z MM2S) | 2n b: `{b, a}`, `a` w młodszej połowie | w pamięci: tablica `uint64` (posit32), `uint32` (posit16), `uint16` (posit8) |
| `m_axis` (do S2MM) | n b: wynik | `tlast` na ostatnim elemencie (S2MM kończy na nim transfer) |

Tablica ⊕ skalar: `STREAM_CFG.scalar_b = 1`, `b` z `OPERAND_B`, połowa `b` słowa ignorowana.
Przepustowość silnika: 1 element / takt dla add/sub/mul (2000 elementów w 2003 taktach w symulacji),
dzielenie iteracyjne `divSteps / k + 2` taktów na element.

## Rejestry (wersja 2.0)

Mapa z etapu 1 (`hw/top-modules/alu/README.md`) plus:

| offset | rejestr | |
| --- | --- | --- |
| 0x24 | STREAM_CFG | bit 0 `scalar_b` |
| 0x50 | CNT_ELEMENTS | wyniki strumienia od ostatniego zerowania |
| 0x0C | STATUS | bit 3 `stream_active`: element(y) w ALU |
| 0x04 | CONFIG | bit 12 `has_stream` |

ID_VERSION = `0x5053_0200`. Liczniki NaR / sat / inexact liczą wyniki obu ścieżek; CYCLES — takty z czymkolwiek w ALU.
`posit.kv260.PositVecRegs` = stałe etapu 2 w `sw/positnp/regs.py` (sprawdza `PositVecAxiTestplan.vec_regs_python_match`).

## Etap 4: quire (wersja 3.0)

`PositQuire` (posit-vector) w tym samym IP: OP = SUM / DOT / SUMSQ kieruje `s_axis` do akumulatora quire
zamiast do ALU. Quire to liczba stałopozycyjna w U2 o szerokości 16 n (512 b dla posit32), LSB = minpos²,
więc każda suma iloczynów positów jest w nim dokładna, a zaokrąglenie jest jedno, przy odczycie.

```
s_axis {b, a} --> dekoder --> iloczyn (DSP) --> wyrównanie --> akumulator 8 x 64 b + rejestry przeniesień
                                                                  |  CTRL.quire_round
                                                                  v
                                       moduł (segmentowo) -> wiodąca jedynka -> enkoder -> RESULT + flagi
```

- 1 element / takt; przeniesienia między segmentami 64 b idą przez rejestry (sumator 64 b zamiast 512 b),
  quire ustala się sam w ≤ 7 taktów po ostatnim elemencie; odczyt zajmuje ok. 9 taktów;
- odczyt nie niszczy quire: kolejne transfery DMA dodają się dalej, zeruje `CTRL.quire_clear`;
- NaR w dowolnym elemencie: wynik NaR (`STATUS.quire_nar`) do zerowania;
- redukcja nie wysyła nic na `m_axis`: w DMA działa tylko MM2S.

| offset | rejestr | |
| --- | --- | --- |
| 0x10 | OP | 4 SUM (Σa), 5 DOT (Σa·b), 6 SUMSQ (Σa·a) |
| 0x08 | CTRL | bit 3 `quire_clear`, bit 4 `quire_round` (wynik w RESULT, flagi w STATUS[10:8], STATUS.done) |
| 0x0C | STATUS | bit 4 `quire_busy`, bit 5 `quire_nar` |
| 0x04 | CONFIG | bit 13 `has_quire`, maska operacji z bitami 4–6 |
| 0x54 | QUIRE_CFG | [15:0] szerokość quire, [31:16] quireFrac |
| 0x80 + 4k | QUIRE[k] | słowo k surowego quire (U2), od najmłodszego |

ID_VERSION = `0x5053_0300`. CNT_ELEMENTS liczy też elementy quire. Sekwencja w `PositVecHw.reduce`:
OP, `quire_clear`, MM2S porcjami, czekanie na CNT_ELEMENTS, `quire_round`, STATUS.done, RESULT i QUIRE[k].

Synteza poza kontekstem (`top-modules/ooc`, `vec_p32`): 5 090 LUT, 3 010 FF, 8 DSP, 122 MHz po routingu —
quire nie zmienił Fmax silnika (szczegóły w `hw/top-modules/ooc/README.md`).

Na płytce (2026-11-13, `posit32_vec` 3.0): iloczyn skalarny z quire na 4 194 304 elementach — **41,1 M elementów / s**
(NumPy float32 `np.dot`, BLAS: 244 M / s). Redukcja nie ma S2MM ani kopii wyniku, więc jest szybsza niż
operacje element po elemencie (add: 25,4 M / s); silnik i DMA biorą 42 ms (1 element / takt), resztę
z ok. 100 ms zabiera pakowanie `{b, a}` po stronie procesora.

**Bitstream z etapu 2 nie ma quire** — trzeba go zbudować od nowa (te same kroki niżej); `PositVecHw.reduce`
sprawdza `CONFIG.has_quire` i bez niego zgłasza błąd.

## Kilka silników w jednym bitstreamie

Przy budowie wybierasz, które formaty mają być w PL — od jednego do trzech (posit8, posit16, posit32).
Każdy silnik ma własny `PositVecAxi_p<n>`, własne AXI DMA i własny bufor u-dma-buf, więc są niezależne:
Python może używać kilku naraz, a tablica każdego formatu idzie do swojego silnika.

| silnik | węzeł UIO rejestrów | węzeł UIO DMA | bufor |
| --- | --- | --- | --- |
| posit⟨n,2⟩ | `posit_vec_p<n>` | `posit_dma_p<n>` | `/dev/udmabuf-posit-p<n>` |

- Nazwa aplikacji wynika z listy: `posit32_vec` (jeden silnik), `posit32_16_8_vec` (trzy); `-name` ją zmienia.
- Moduły każdego silnika mają przedrostek `p<n>_` (SpinalHDL `globalPrefix`), więc trzy IP nie zderzają się
  nazwami w jednym projekcie Vivado.
- `.json` aplikacji ma listę `engines` (nazwy węzłów, adresy, bufory); `install_overlay.sh` sprawdza urządzenia
  wszystkich silników, a `positnp.backend_hw.vec_engines()` znajduje je na płytce po nazwach węzłów UIO.
- Bufory: `-buf_mb` na silnik (domyślnie 64 MB, przy trzech razem 192 MB). Pochodzą z CMA — sprawdź
  `grep Cma /proc/meminfo`; gdy brakuje, `-buf_mb 32` (porcje DMA będą mniejsze, wynik ten sam).
- Zasoby (OOC): posit32 ok. 6,5k LUT, posit16 ok. 3,3k, posit8 mniej, plus AXI DMA na silnik — trzy silniki
  to kilkanaście procent LUT układu.
- Aplikacje zbudowane wcześniej (jeden silnik, węzły `posit_vec` / `posit_dma` / `udmabuf-posit`) dalej działają.

## Krok po kroku

1. RTL (z `hw/`) — jeden plik na silnik, `hw/gen/p<n>/PositVecAxi_p<n>.v`:
   ```
   sbt "top-vec/runMain posit.top.vec.VecKv260"            # posit<32,2>
   sbt "top-vec/runMain posit.top.vec.VecKv260 32 16 8"    # trzy silniki; silnik = n[:es[:k]], np. 16:2:1
   ```
2. Bitstream i aplikacja (z `hw/top-modules/vec/vivado`):
   ```
   vivado -mode batch -nojournal -log build.log -source build.tcl                               # posit32_vec
   vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -engines "32 16 8"   # posit32_16_8_vec
   ```
   Wynik: `sw/positnp/overlays/<nazwa>/` (`.bit.bin`, `.dtsi`, `shell.json`, `.json`).
   `tclsh overlay_files_test.tcl` sprawdza generowanie `.dtsi` / `.json` bez Vivado (z `dtc`).
3. Na płytce, z `sw/`:
   ```
   scripts/build_udmabuf.sh                    # raz na jądro: moduł u-dma-buf (git, nagłówki jądra)
   scripts/install_overlay.sh posit32_16_8_vec # sprawdza /dev/uio i /dev/udmabuf-posit-p<n> każdego silnika
   sudo python3 -m pytest -v -s tests/test_board_vec.py   # każdy test na każdym silniku + silniki naraz
   ```
   z Pythona:
   ```python
   import numpy as np, positnp as pn
   from positnp.backend_hw import PositVecHw, vec_engines
   vec_engines()                          # [VecEngine('posit_vec_p32', ...), ... p16, ... p8]
   hw = PositVecHw.open(32)               # silnik posit32 (przy jednym silniku: open())
   all_hw = PositVecHw.open_all()         # {32: PositVecHw, 16: ..., 8: ...}; hw.close() przed xmutil unloadapp
   r = hw.array_op("mul", a_bits, b_bits)                  # surowe bity positów (np. uint32)
   pn.set_backend(hw)                                      # PositArray liczy teraz na płytce
   x = pn.array(np.linspace(-4, 4, 1000))
   (x * x - x / 3).to_float()
   ```

## build.tcl

| opcja `-tclargs` | domyślnie | |
| --- | --- | --- |
| `-engines "<n> ..."` | `32` | silniki: 8, 16, 32, każdy co najwyżej raz (`tdata` AXI DMA w pełnych bajtach); `-n <n>` = jeden |
| `-name <nazwa>` | `posit<n>[_<n>…]_vec` | nazwa aplikacji dla xmutil |
| `-buf_mb <MB>` | `64` | bufor u-dma-buf na silnik; połowę z grubsza zajmuje wejście, resztę wynik |
| `-out`, `-deploy`, `-part`, `-jobs`, `-threads`, `-stop ip\|bd` | | jak w `alu` |

AXI DMA (PG021): tryb prosty (bez scatter-gather), adresy 32 b, długość transferu 26 b (do 64 MB − 1),
szyna pamięci 64 b, `tdata` MM2S 2n b, S2MM n b.

## PS, device tree i pamięć podręczna

Jak w etapie 1 (zegar PL0, reset, AFI, węzły UIO), plus:

- **port HP0 FPD** (dane DMA ↔ DDR) na domyślnych 128 b, wpisane też do AFI (`config-afi = <4 0>, <5 0>`);
- **bufor DMA** (na silnik): węzeł `compatible = "ikwzm,u-dma-buf"`, `device-name = "udmabuf-posit-p<n>"` →
  `/dev/udmabuf-posit-p<n>`, adres fizyczny w `/sys/class/u-dma-buf/udmabuf-posit-p<n>/phys_addr`;
  maska DMA 32 b, więc bufor leży poniżej 4 GB, w zasięgu 32-bitowych adresów DMA;
- **spójność pamięci podręcznej**: HP0 nie jest spójny z cache CPU. `udmabuf.py` mapuje bufor z cache
  i przed każdym transferem robi `sync_for_device` (wejście i wynik), po nim `sync_for_cpu` (wynik).
  Mapowanie bez cache (`O_SYNC`) na arm64 to pamięć Device, w której `memcpy`/numpy padają z SIGBUS.

## Etap 5: sqrt, FMA / AXPY, konwersje float32 (wersja 4.0)

Nowe operacje w tym samym ALU (`PositAluConfig(sqrt, fma, convert)`), dostępne i w strumieniu,
i przez rejestry. Każda zaokrągla raz (RNE), bit w bit z `PositRef.sqrt / fma / fromFloat32 / toFloat32`.

| OP | nazwa | wynik | `s_axis {b, a}` | takty / element (posit32, k = 4) |
| ---: | --- | --- | --- | ---: |
| 7 | SQRT | √a, ujemne → NaR | `b` ignorowane | 10 (rekurencja jak dzielnik, `sqrtSteps / k + 2`) |
| 8 | FMA | a · b + OPERAND_C | `b` z tablicy albo OPERAND_B (`scalar_b`) | 3 |
| 9 | AXPY | a · OPERAND_B + b (BLAS `y = α x + y`) | `a` = x, `b` = y | 3 |
| 10 | F2P | posit32 z bitów float32 `a` | NaN, ±inf → NaR; subnormale dokładnie | 1 |
| 11 | P2F | bity float32 z posit32 `a` | NaR → 0x7fc00000 | 1 |

| offset | rejestr | |
| --- | --- | --- |
| 0x58 | OPERAND_C | trzeci argument FMA (tablica · tablica + stała) |
| 0x04 | CONFIG | maska operacji [31:16] z bitami 7–11 (F2P / P2F tylko dla posit⟨32,2⟩) |

ID_VERSION = `0x5053_0400`. Iloczyn FMA jest dokładny (2F + 2 b), suma wyrównana z bitami ochronnymi
i sticky, więc wynik różni się od `mul`, potem `add` (dwa zaokrąglenia) — test `fma_single_rounding`.
SQRT i FMA dzielą z dzielnikiem ścieżkę iteracyjną (jedna operacja naraz); pozostałe są w potoku.

Trzy tablice (`fma(x, y, z)`) wymagają trzeciego strumienia, czyli własnego mastera AXI4 z etapu 6;
do tego czasu `PositVecHw` liczy je w `PositSw` i dolicza do `stats()["fallback"]`. Tak samo każda
operacja, której wgrany bitstream nie ma (maska w CONFIG) — kod Pythona działa bez zmian na starszych bitstreamach.

Na płytce (2026-11-13, `posit32_vec` 4.0): `test_board_vec.py` przechodzi w całości — sqrt na 1 000 001 elementach,
FMA / AXPY w trzech trybach, F2P / P2F z NaN, inf i subnormalami, bit w bit z `PositSw`, `PositArray` na sprzęcie
bez przejść na `PositSw`. Przepustowość jednego wywołania na 4 194 304 elementach (z pakowaniem,
synchronizacją cache i kopiami):

| | M elementów / s | silnik (100 MHz) | |
| --- | ---: | ---: | --- |
| sqrt | 8,9 | 10 | ogranicza rekurencja (10 taktów / element), PS dokłada ok. 11% |
| FMA | 20,2 | 33 | 3 takty / element; pakowanie `{b, a}` jak w add |
| AXPY | 20,3 | 33 | to samo co FMA: `x` i `y` w jednym słowie strumienia |
| F2P | 43,4 | 100 | jednoargumentowe: pakowanie tylko połowy `a`, mniej pracy PS niż add (25,4) |
| P2F | 44,2 | 100 | |
| NumPy float32 `np.sqrt` | 224 | | |

- sqrt jest ograniczony silnikiem; szybszy wymagałby `k = 8` (6 taktów) albo rozwiniętego potoku — kosztem LUT.
- FMA / AXPY: 3 takty / element w ścieżce iteracyjnej; potokowe FMA dałoby 1 / takt, ale wtedy ograniczy PS
  (jak dla add, ok. 25 M/s) — to zysk dopiero razem z masterem AXI4 z etapu 6.
- Konwersje są ograniczone przez PS (silnik: 100 M/s); dają dokładne RNE float32 ↔ posit32 bez przejścia
  przez float64 i odciążają procesor przy danych, które i tak idą do PL.

**Bitstream z etapu 4 nie ma tych operacji** — trzeba go zbudować od nowa (te same kroki niżej).
Synteza poza kontekstem (`top-modules/ooc`, `vec_p32`): 6 550 LUT (+31% względem etapu 4), 3 423 FF, 8 DSP,
po rozdzieleniu wyrównania i dodawania w FMA 114 MHz po routingu (zapas 1,2 ns przy 100 MHz; ścieżka krytyczna
to znów dodawanie z etapu 1). Szczegóły w `hw/top-modules/ooc/README.md`.

## Weryfikacja

| poziom | gdzie | |
| --- | --- | --- |
| silnik | `posit-vector/.../PositStreamAluTestplan` | tablice o losowych długościach, `last`, tablica ⊕ skalar, przeplot z rejestrami, przepustowość |
| IP | `posit-kv260/.../PositVecAxiTestplan` | AXI4-Lite + AXI4-Stream jak z AXI DMA, długości 1, 2, 3, 1000, liczniki, miękki reset, nazwy portów |
| quire | `posit-vector/.../PositQuireTestplan` | wszystkie pary posit8 jako wektory dł. 1, losowe posit16/32, znoszenie się, przeniesienia przez cały quire, NaR, kontynuacja, permutacja, 200k × maxpos² — wynik, flagi i surowy quire kontra `QuireRef` |
| quire w IP | `posit-kv260/.../PositVecQuireTestplan` | sum/dot/sumsq przez AXI4-Lite + AXI4-Stream, kontynuacja, NaR, przeplot z tablicami |
| etap 5, ALU | `posit-vector/.../PositAluStage5Testplan` | sqrt: wszystkie posit8/16 i losowe posit32, odstęp = `sqrtCycles`; FMA z znoszeniem się; konwersje z przypadkami brzegowymi; przeplot wszystkich operacji; ALU bez etapu 5 |
| etap 5, IP | `posit-kv260/.../PositVecStage5Testplan` | sqrt, FMA, AXPY (też z `scalar_b`), F2P, P2F na tablicach przez AXI; sqrt i FMA przez rejestry |
| sterownik | `sw/tests/test_vec_fake.py` | `PositVecHw` na atrapach rejestrów, DMA i bufora: porcjowanie, protokół cache, błędy DMA |
| płytka | `sw/tests/test_board_vec.py` | długości 1 … 1 000 001, wszystkie operacje kontra `ref.py`, tablica ⊕ skalar, liczniki, przepustowość; etap 5: sqrt, FMA / AXPY, F2P / P2F kontra `PositSw`, `PositArray` na sprzęcie, przepustowość |

Bloku AXI DMA nie da się zasymulować (szyfrowane IP Xilinxa); jego zachowanie pokrywa atrapa `FakeDma`
według PG021, a resztę — test na płytce.

## Wyniki na płytce (2026-11-13)

`posit32_vec` (posit⟨32,2⟩, dzielnik k = 4, 100 MHz), Ubuntu 24.04 na KV260:
`test_board_vec.py` — 31 / 31, w tym tablice 1 … 1 000 001 elementów dla każdej operacji bit w bit z `ref.py`.

Przepustowość jednego `array_op` na 4 194 304 elementach (pakowanie, synchronizacja cache, DMA i kopie w cenie):

| | M elementów / s | |
| --- | ---: | --- |
| sprzęt, add | 10,6 | silnik: 100 M/s (1 / takt) |
| sprzęt, div | 5,8 | silnik: 10 M/s (`divSteps / k + 2` = 10 taktów) |
| `ref.py` na A53, add | 0,003 | ok. 3 500 × wolniej niż sprzęt |
| NumPy float32 na A53, add | 247 | ok. 23 × szybciej niż sprzęt, jak przewidywał plan |

- Dla add silnik i DMA zajmują ok. 40 ms z ok. 400 ms; resztę zabiera strona PS (rzutowania i pakowanie `{b, a}`
  w NumPy, synchronizacja cache na 48 MB bufora, kopia wyniku). To jest pierwsze miejsce do przyspieszenia, nie sprzęt.
- Dla div ograniczeniem jest już dzielnik iteracyjny (10 M/s), a narzut PS dokłada się do niego.

### Po zmianach w `array_op` z etapu 3 (2026-11-13)

Bity bez kopii, pakowanie dwiema kopiami z krokiem, synchronizacja cache osobno dla wejścia i wyniku.
Ten sam test, czasy faz z `PositVecHw.last_timing` (4 194 304 elementy):

| | M elementów / s | pakowanie | sync przed | DMA + silnik | sync po | kopia wyniku | razem |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| add | 25,4 (było 10,6) | 82,9 ms | 5,5 ms | 42,1 ms | 1,9 ms | 31,5 ms | 164 ms |
| div | 8,4 (było 5,8) | 57,9 ms | 5,5 ms | 419,5 ms | 2,3 ms | 15,6 ms | 501 ms |
| `PositSw` (vecops) na A53, add | 0,55 | | | | | | |
| NumPy float32 na A53, add | 211 | | | | | | |

- DMA i silnik działają dokładnie z przewidywaną prędkością: add 42 ms = 1 element / takt przy 100 MHz,
  div 420 ms = 10 taktów / element. Sprzęt nie ma tu nic do oddania.
- Synchronizacja cache jest tania (7 ms na 48 MB).
- Dla add ok. 70% czasu to procesor: pakowanie `{b, a}` (zapisy z krokiem 8 B, ok. 400 MB/s na A53) i kopia
  wyniku. Usunie je dopiero własny master AXI4 z etapu 6, który czyta `a` i `b` z dwóch osobnych tablic
  (bez pakowania) i może pisać wynik od razu do bufora użytkownika. Wcześniej da się zyskać do ok. 1,5×,
  nakładając pakowanie kolejnej porcji na DMA bieżącej (podwójny bufor).
- Sprzęt jest ok. 46 × szybszy od `PositSw` na tym samym A53; float32 w NumPy wciąż ok. 8 × szybszy od sprzętu.
