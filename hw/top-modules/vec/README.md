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

## Etap 6: własny master AXI4 (wersja 5.0, domyślnie)

Silnik sam czyta argumenty z DDR i zapisuje wynik — bez AXI DMA i bez pakowania `{b, a}` po stronie
procesora. Python podaje adresy fizyczne (w buforze u-dma-buf) i długość, startuje polecenie i czeka na
MEM_STATUS.done (albo przerwanie).

```
PS --HPM0--> smartconnect_ctl --> posit_vec_p<n>/s_axi      (rejestry, polecenia MEM_*)
DDR <--HP0-- smartconnect_data <-- posit_vec_p<n>/m_axi     (AXI4 128 b: odczyt a, b, c i zapis wyniku)
             pl_ps_irq0[k]     <-- posit_vec_p<n>/irq        (GIC SPI 89 + k)
```

`PositMemMover` (posit-kv260):
- do 3 strumieni odczytu (a, b, c — każdy z własnym ID AXI) i jeden zapisu; serie INCR do 16 słów
  po 16 B, nigdy przez granicę 4 KB; ostatnie słowo zapisu z WSTRB (bajty za tablicą nietknięte);
- **kredyty**: seria idzie na AR tylko, gdy FIFO strumienia (64 słowa) ma miejsce na wszystkie zamówione
  słowa, więc kanał R nigdy nie czeka na silnik — dane a, b, c mogą przychodzić w dowolnym przeplocie
  bez zakleszczenia;
- **działanie w miejscu**: MEM_DST = MEM_SRC_A działa (zapis zawsze za odczytem); `PositVecHw` tak robi;
- **bezpieczeństwo**: polecenie z adresem spoza [MEM_LO, MEM_HI) albo niewyrównanym do 16 B kończy się
  błędem w MEM_STATUS bez żadnej transakcji; po resecie zakres jest pusty, `PositVecHw` wpisuje w niego
  swój bufor; odpowiedź SLVERR / DECERR → `err_resp`;
- FMA3 (OP 12): a · b + c na trzech tablicach w jednym przebiegu (wcześniej w `PositSw`).

| offset | rejestr | |
| --- | --- | --- |
| 0x60 | MEM_LEN | elementy polecenia |
| 0x64 / 0x68 / 0x6C | MEM_SRC_A / B / C | adresy fizyczne (32 b, wyrównane do 16 B) |
| 0x70 | MEM_DST | adres wyniku |
| 0x74 / 0x78 | MEM_LO / MEM_HI | dozwolony zakres [LO, HI) |
| 0x7C | MEM_CFG | bit 0 read_b, 1 read_c, 2 write, 3 irq_en |
| 0xC0 | MEM_STATUS | bit 0 busy, 1 done, 2 err_range, 3 err_align, 4 err_resp |
| 0xC4 | MEM_CYCLES | takty ostatniego polecenia |
| 0x08 | CTRL | bit 5 mem_start, bit 6 mem_ack (kasuje done i przerwanie) |
| 0x04 | CONFIG | bit 14 has_master; maska z FMA3 (bit 12) |

ID_VERSION = `0x5053_0500`. Sekwencja w `PositVecHw._mem`: kopie a, b, c do osobnych regionów bufora
(ciągłe, bez przeplotu), `sync_for_device`, MEM_*, `CTRL.mem_start`, MEM_STATUS.done, `sync_for_cpu`, kopia
wyniku (z regionu a). Redukcje: to samo bez zapisu, potem `quire_round` jak w etapie 4.

Symulacja (`PositVecMemTestplan`, model pamięci `AxiMemModel`: losowe ready / przerwy na każdym kanale,
przeplot ID, sprawdzanie protokołu): wszystkie operacje bit w bit dla posit32 / 16 / 8, długości 1 … 3000,
bufory przecinające 4 KB, w miejscu, skalar, redukcje, odrzucanie, SLVERR, przerwanie;
**add posit32: 4096 elementów w 4142 taktach (0,99 / takt)** — dwa strumienie odczytu i zapis równolegle.

**Na płytce (2026-11-14, `posit32_16_8_mem`: posit32 z 4 torami, posit16, posit8)**: `test_board_vec.py` przechodzi
w całości. Jedno `array_op` na 4 194 304 elementach, M elementów / s (w nawiasie: AXI DMA z etapu 5):

| | posit32 | posit16 | posit8 |
| --- | ---: | ---: | ---: |
| add | **59,5** (25,4) | 72,0 (39,3) | 80,0 (51,4) |
| div | 31,3 (22,7) | 15,6 (14,3) | 23,6 (21,3) |
| sqrt | 33,7 (26,7) | 16,0 | 24,0 |
| FMA / AXPY | 58,3 / 59,1 (34,3) | 29,5 | 30,7 |
| FMA3 (trzy tablice) | 37,0 (wcześniej program) | 25,1 | 28,3 |
| F2P / P2F | 68,6 / 68,3 (44,6) | | |
| quire dot | 66,8 (41,1) | 78,6 (55,5) | 83,6 (62,7) |

Rozbicie add posit32: kopie wejść 14,6 ms (było 82,9 ms pakowania `{b, a}`), synchronizacja 6,2 ms,
**silnik 42,0 ms = dokładnie 1 element / takt**, kopia wyniku 7,3 ms (było 31,5 ms). Silnik pracuje teraz
ok. 57% czasu; reszta to kopie do i z bufora po stronie procesora.

- div / sqrt posit32 (4 tory): 104,9 ms = 2,5 taktu / element — ogranicza silnik, nie procesor;
  posit16 / posit8 z jednym torem: 6 i 4 takty, FMA 3 takty — tu kolejne tory dadzą 2–4 ×.
- FMA3: kopia trzeciej tablicy (pack 47,6 ms) — pierwszy dotyk stron trzeciego regionu bufora;
  pierwsze wywołanie `array_op` w procesie ma ten sam koszt (test_throughput: add 36,7 M/s, pack 50,9 ms).
- Dalej: obie kopie da się usunąć, trzymając bity `PositArray` od razu w buforze u-dma-buf (bez kopii),
  albo ukryć, nakładając kopię kolejnej porcji na pracę silnika (podwójny bufor) — wtedy add zbliży się
  do granicy silnika, 100 M / s; NumPy float32 add: 250 M / s (było 10 ×, jest 4 × szybszy).

**Podwójny bufor** (`PositVecHw.double_buffer = True`, domyślnie): bufor dzielony na dwie połowy, ok.
`MEM_CHUNKS` = 16 porcji na wywołanie (co najmniej 256 KB na tablicę; `MEM_CHUNK_BYTES` ustala rozmiar ręcznie). Kolejność: kopia porcji 0, start; potem dla każdej porcji j: kopia porcji
j + 1 do drugiej połowy (silnik liczy j), czekanie na j, start j + 1, odbiór wyniku j (silnik liczy j + 1).
Silnik i procesor nigdy nie pracują na tej samej połowie: połowa jest nadpisywana dopiero po zakończeniu jej
polecenia i odbiorze wyniku. Test na atrapie wykonującej polecenie dopiero przy czekaniu (najpóźniejszy
możliwy odczyt danych) wykrywa każde naruszenie. `test_master_double_buffer_gain` mierzy zysk na płytce.

Pierwszy pomiar na płytce (add, 4 194 304 elementy, M el. / s; porcja = bajty na tablicę), synchronizacja jeszcze
po 4 × open / write / close:

| | pojedynczy | 256 KB | 1 MB | 4 MB | 8 MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| posit32 | 53,7 | 25,9 | 58,2 | **77,4** | 69,4 |
| posit16 | 67,7 | 51,2 | **89,2** | 80,0 | 68,9 |
| posit8 | 77,3 | **91,2** | 88,5 | 80,4 | 79,0 |

Koszt nie zależał od rozmiaru synchronizowanego zakresu, tylko od liczby wywołań: ok. 1 ms na
`sync_for_device` / `sync_for_cpu` (4 × open / write / close na plikach sysfs u-dma-buf). Stąd: deskryptory sysfs
otwierane raz i `pwrite`, bez ponownego zapisu niezmienionych atrybutów, oraz liczba porcji zamiast stałego
rozmiaru (optimum: posit32 ok. 4 porcje, posit16 ok. 8, posit8 ok. 16 — tańsze kopie znoszą więcej porcji).

Po zmianie synchronizacji: **para `sync_for_device` + `sync_for_cpu` 57 µs** (było ok. 2 ms), więc opłaca się więcej porcji:

| | pojedynczy | 6 porcji | 256 KB | 1 MB | 4 MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| posit32 | 58,5 | 87,5 | 90,2 | **92,0** | 84,0 |
| posit16 | 72,1 | 92,5 | **94,0** | 93,6 | 83,3 |
| posit8 | 81,3 | 94,4 | **95,8** | 94,3 | 84,9 |

Domyślnie 16 porcji (posit32 1 MB, posit16 512 KB, posit8 256 KB): **92–96 % granicy silnika** (100 M / s =
1 element / takt przy 100 MHz). Względem AXI DMA z etapu 5 (add posit32 25,4 M / s) to 3,6 ×; NumPy float32
add (ok. 250 M / s) jest już tylko 2,7 × szybszy. Dalej ogranicza sam silnik — więcej daje tylko szerszy
silnik (kilka elementów na takt) albo wyższy zegar.

Stara ścieżka (AXI4-Stream + AXI DMA) zostaje: `VecKv260 --dma ...` i `build.tcl -io dma` (aplikacje `*_vec`);
`PositVecHw` rozpoznaje tryb po CONFIG.has_master, a `pn.board` obsługuje obie.

Po drodze naprawione: rejestry sterujące są teraz zerowane resetem (`readAndWrite` zamiast `driveAndRead`,
które w SpinalHDL dokłada niezerowany rejestr pośredni — po resecie OPERAND_B, STREAM_CFG itd. były
przypadkowe, co na FPGA maskowało zerowanie przy konfiguracji).

## Etap 7a: dokładny `A @ B` na quire (wersja 5.1)

Każdy element iloczynu macierzy to iloczyn skalarny wiersza A i kolumny B, policzony dokładnie w quire
i zaokrąglony raz — jak `np.dot` z etapu 4, ale z jednym poleceniem na całą kolumnę B zamiast jednego na element.

```
OP_XLOAD:  m_axi R --> x (kolumna B, K elementów) --> bufor x (BRAM, maxK = 4096 elementów)
OP_GEMV:   m_axi R --> A wiersz po wierszu, element A[i, j] --> odczyt x[j] (1 takt) --> quire i mod 2
                       quire 0: wiersze 0, 2, 4 ...   koniec wiersza: round --> kolejka 0 --+--> po kolei --> grupy po L --> m_axi W
                       quire 1: wiersze 1, 3, 5 ...   koniec wiersza: round --> kolejka 1 --+    (y, M elementów)
```

- dwa quire na zmianę: zaokrąglenie wiersza i (ok. 20 taktów dla posit32) idzie w cieniu wiersza i + 1, więc
  silnik bierze 1 element A na takt także dla krótkich wierszy; quire zeruje się w takcie wyniku;
- wynik wchodzi do quire tylko przy wolnym miejscu w jego kolejce, kolejki scala licznik wierszy — kolejność
  y zachowana przy dowolnym czekaniu zapisu;
- rejestry: `MEM_WLEN` (0xC8) = M, elementy do zapisu (zapis movera liczy teraz osobno `wlen`), `GEMV_K` (0xCC) = K,
  `GEMV_CFG` (0xD0, RO) = maxK; `MEM_LEN` = M · K; `MEM_STATUS.err_shape` (bit 5): `MEM_LEN ≠ M · K`, K = 0,
  K > maxK albo XLOAD dłuższy niż maxK — polecenie odrzucone bez transakcji; CONFIG bit 15 `has_gemv`, opMask bity 13 / 14;
- koszt: bufor x (posit32: 4 BRAM36), drugi quire i dwie małe kolejki; `VecKv260 --gemv-k N` zmienia maxK,
  `--gemv-k 0` buduje silnik bez gemv; OOC: `vec_p32_mem_L2_gemv`;
- Python: `A @ B`, `np.matmul`, `np.dot`, `pn.matmul`, `hw.matmul` / `hw.gemv`: kolumny B po kolei (XLOAD + GEMV),
  A, Bᵀ i C w pamięci urządzenia (A z `to_device()` czytana w miejscu), za duże macierze w blokach wierszy
  i kolumn; K > maxK albo bitstream bez gemv: jeden dot quire na element.

Symulacja (`PositVecMemTestplan.mem_gemv*`, model pamięci z losowymi opóźnieniami): kształty 1×1, 1×5, 3×1, 2×2, 5×3,
7×17, 33×10, 9×maxK, 40×1 i losowe, elementy z rozkładu `stress` (NaR, zera, skrajne skale) — y bit w bit
z `QuireRef` dla posit32 ×1 / ×2, posit16 ×4, posit8 ×8; bajty za y nietknięte; redukcje po gemv poprawne;
odrzucenia kształtu; **64 × 64 posit32: 4 133 takty (0,99 MAC / takt)**.

**Na płytce (2026-11-15, `posit32_16_8_mem` z gemv)**: `test_board_vec.py -k matmul` przechodzi na wszystkich trzech
silnikach (kształty 1×1×1 … 17×4096×2, każdy element bit w bit z `PositSw`, także z A w pamięci urządzenia);
`scripts/diag_gemv.py` — XLOAD / GEMV krok po kroku z rejestrami. M MAC / s:

| | posit32 | posit16 | posit8 | MAC / takt silnika |
| --- | ---: | ---: | ---: | ---: |
| 256×256 @ 256×16, A kopiowana | 81,9 | 82,0 | 82,9 | 0,994 |
| 256×256 @ 256×16, A w pamięci urządzenia | 83,8 | 84,0 | 84,1 | 0,994 |
| 1024×1024 @ 1024×8, A w pamięci urządzenia | **97,6** | **97,8** | **98,0** | 0,999 |

- Silnik pracuje praktycznie bez przerw (zaokrąglenia wierszy w cieniu drugiego quire); stały koszt to XLOAD
  i dwa polecenia na kolumnę B, widoczne przy mniejszych macierzach (ok. 12,4 ms zamiast 10,5 ms).
- Wszystkie formaty mają tę samą szybkość: quire bierze 1 element na takt niezależnie od n i od torów L.
  Szybszy matmul wymagałby kilku par quire na kolumny B naraz (np. 2–4 kolumny w jednym przejściu A).
- To dużo mniej niż BLAS float32 na A53, ale każdy element jest dokładnym iloczynem skalarnym z jednym zaokrągleniem.

### P kolumn naraz (wersja 5.2)

Jedno przejście A liczy P kolumn wyniku: każda kolumna ma własny bufor x i własną parę quire, element A[i, j]
idzie jednocześnie do wszystkich aktywnych kolumn — **P mnożeń z dodawaniem na takt** przy tym samym odczycie A.

```
OP_XLOAD (MEM_LEN = K · cols):  x_0[0..K), x_1[0..K), ... po kolei --> bufory x_0 .. x_{cols-1}
OP_GEMV:  A[i, j] --+--> x_0[j] --> quire (bank i mod 2, kolumna 0) --> kolejka --+
                    +--> x_1[j] --> quire (bank i mod 2, kolumna 1) --> kolejka --+--> wierszami: y[i, 0..cols) --> zapis
                    ...                                                            |   (M · cols elementów)
                    +--> x_{P-1}[j] -----------------------------------------------+
```

- `GEMV_COLS` (0xD4, RW) = cols ≤ P (po resecie 1: sterownik 5.1 działa bez zmian), `GEMV_CFG[23:16]` = P;
  XLOAD czyta cols kolumn po K elementów (`MEM_LEN = K · cols`), GEMV zapisuje Y (M × cols) wierszami;
  `err_shape` także dla XLOAD `MEM_LEN ≠ K · cols` i cols poza 1..P;
- element wchodzi do banku tylko razem do wszystkich aktywnych kolumn (każda gotowa i z miejscem w kolejce),
  więc kolumny idą krok w krok; scalanie wyników: wiersz po wierszu, w wierszu kolumny po kolei;
- koszt: 2 P quire i P buforów x; posit32, P = 4 (OOC `vec_p32_mem_L2_gemv4`): 35 119 LUT, 44 DSP, bufory x w URAM,
  103,6 MHz — ok. 2 900 LUT i 4 DSP na quire posit32; `VecKv260 --gemv-cols P`, domyślnie 4;
- Python: `matmul` dzieli B na grupy po P kolumn (Bᵀ grupami w pamięci urządzenia, C grupami wierszy),
  jedna para XLOAD + GEMV na grupę; `hw.gemv_cols`.

Symulacja: wszystkie kształty z `mem_gemv` dla P = 4 (cols = 4, 1, 3, 2) na posit32 ×1 / ×2, posit16 ×4,
posit8 ×8 bit w bit z `QuireRef`; **64 × 64 posit32 na 4 kolumny: 4 160 taktów = 3,94 MAC / takt**
(jedna kolumna: 4 141 taktów, 0,99).

**Na płytce (2026-11-15, `posit32_16_8_mem`, P = 4 we wszystkich trzech silnikach)**, M MAC / s
(w nawiasie: P = 1, wersja 5.1):

| | posit32 | posit16 | posit8 | MAC / takt silnika |
| --- | ---: | ---: | ---: | ---: |
| 256×256 @ 256×16, A kopiowana | 250,9 (81,9) | 258,7 (82,0) | 262,7 (82,9) | 3,93 |
| 256×256 @ 256×16, A w pamięci urządzenia | 267,8 (83,8) | 274,7 (84,0) | 277,6 (84,1) | 3,93 |
| 1024×1024 @ 1024×8, A w pamięci urządzenia | 370,0 (97,6) | 373,5 (97,8) | 375,0 (98,0) | 3,98 |
| 1024×1024 @ 1024×64, A w pamięci urządzenia | **386,8** | **387,9** | **388,6** | 3,98 |

- 4 kolumny dają dokładnie 4 × przepustowość: silnik 3,984 MAC / takt z 4 możliwych, przy 100 MHz ok. 390 M MAC/s
  na silnik; 1024 × 1024 · 1024 × 64 (67 M MAC) w 173 ms, z tego 170 ms pracy silnika.
- Przy małych macierzach znowu waży stały koszt poleceń (XLOAD + GEMV na grupę kolumn, synchronizacja cache).
- Dalej: więcej kolumn (P = 8: ok. +20 000 LUT dla posit32, zmieści się) albo L elementów A na takt
  (wymaga L quire na kolumnę).

## Sprzętowe FFT radix-4 (wersja 5.5, blok opcjonalny)

`PositFftCore` (`posit-kv260/.../PositFftCore.scala`): partia B transformat N = 4^S (16 … 4^maxS) jednym poleceniem MEM,
jeden motylek radix-4 na takt, dane w BRAM silnika.

```
LOAD   grupy po L (L = 1, 2, 4) z movera -> pozycja rev4(n) (odwrócone cyfry w bazie 4)
COMP   S etapów DIT × N / 4 motylków, między etapami opróżnienie potoku (15 taktów)
STORE  pozycje 0 .. N − 1 po kolei (porządek naturalny), grupy po L -> zapis
banki: pozycja i -> bank (suma cyfr i w bazie 4) mod 4, adres i >> 2: 4 wejścia motylka i 4 kolejne pozycje
       zawsze w 4 różnych bankach (odczyt port A, zapis port B, bez konfliktów)
twiddle: T[e] = W_N^e, e < 3N/4, 3 kopie (w1, w2, w3 na takt), ładowane z Pythona raz na N
motylek: P_p = w_p · y_p (4 mul + 2 add / sub), s0 = P0 + P2, d0 = P0 − P2, s1 = P1 + P3, d1 = P1 − P3,
         X0 = s0 + s1, X2 = s0 − s1, X1 = d0 − i d1, X3 = d0 + i d1 — 12 mul + 22 add, potok 12 taktów
```

- motylek klasyczny (każde działanie zaokrąglone, `PositOps` z potokiem jak `PositPipeline.full`): pełny fused (jedno
  zaokrąglenie na wyjście motylka) wymagałby akumulatora szerokości quire na każdej z 8 składowych — za drogie;
  model (`positnp.fft.fft_core_model`, `fft_fused_model`) pokazał, że klasyczny posit32 dalej ok. 3 × dokładniejszy niż float32;
- rejestry: `FFT_CTRL` (0xE4) bit 0 fft / bit 1 twiddle, `FFT_CFG` (0xE8) = S, `FFT_INFO` (0xEC) = maxS; dane
  transformaty: N części rzeczywistych, potem N urojonych (wynik tak samo); `MEM_LEN = 2N · MEM_WLEN`; twiddle
  `MEM_LEN = 3N / 2`; `err_shape` dla złej długości, S < 2, S > maxS, B = 0; w trybie FFT inne ścieżki stoją;
- koszt (posit32, maxS = 6): 12 mul + 22 add ≈ 27 tys. LUT i 48 DSP, banki 4 × 2 × 1024 × 32 b, twiddle 3 kopie
  (do 3N/4) — OOC: `vec_p32_mem_L2_fft6`; `VecKv260 --fft 6` (posit16 i posit32) albo `--fft 6:32` (tylko posit32);
- **miejsce**: trzy silniki z gemv × 4 (8 quire w każdym), tablicami i rdzeniem FFT posit32 się nie mieszczą
  (2026-11-16: placer chciał 10 925 CLB z 9 894 dostępnych); mieści się np.
  `VecKv260 --fft 6:32 --gemv-cols 32=2,16=2,8=0 32:2:4:4:2 16:2:4:2:4 8:2:4:1:8` (gemv na 2 kolumny w posit32 / posit16,
  posit8 bez gemv — ok. 23 tys. LUT mniej); `--gemv-cols` przyjmuje też jedną liczbę dla wszystkich silników;
- Python: `pn.fft.fft` (method `"auto"`) używa rdzenia, gdy jest w bitstreamie i N = 4^S ≤ 4^maxS, inaczej bloków DFT;
  `method="core"` liczy ten sam algorytm w programie (`PositSw.fft_core` = model, bit w bit), `"blocks"`, `"exact"`;
  tablica twiddli ładowana raz na N.

Dokładność modelu rdzenia wobec float64 `np.fft.fft` (największy błąd / największy moduł):

| N | posit32: rdzeń | fused | bloki DFT | float32 `np.fft` | posit16: rdzeń | bloki DFT |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 64 | 6,9e-9 | 3,9e-9 | 3,9e-9 | 2,6e-8 | 3,2e-4 | 3,0e-4 |
| 1024 | 1,1e-8 | 6,5e-9 | 6,4e-9 | 3,3e-8 | 1,0e-3 | 4,1e-4 |
| 4096 | 1,3e-8 | 6,5e-9 | 5,4e-9 | 4,1e-8 | 8,4e-4 | 3,8e-4 |

Symulacja (`PositVecMemTestplan.mem_fft`): posit32 ×1 (maxS = 2), ×2 (maxS = 4), posit16 ×4 (maxS = 3); N = 16, 64, 256,
partie 1 i 3, wejścia z `stress` — wynik bit w bit z radix-4 DIT na `PositRef`; odrzucenia; zwykłe `add` po FFT.
Takty na transformatę: 2N / L (wczytanie) + S (N / 4 + 15) + 2N / L (zapis), np. posit32 ×2, N = 256: ok. 870.
Oczekiwane na płytce (100 MHz), transformaty / s:

| N | posit32 ×2 | posit16 ×4 | bloki DFT (zmierzone, posit32) | NumPy complex64 na A53 |
| ---: | ---: | ---: | ---: | ---: |
| 64 | ~450 000 | ~600 000 | 18 455 | 234 000 |
| 1024 | ~29 000 | ~42 000 | 740 | 14 100 |
| 4096 | ~6 900 | ~10 000 | 134 | 2 300 |

Wczytanie i zapis nie zachodzą jeszcze na obliczenia: z podwójnym buforem banków rdzeń byłby ograniczony samym
liczeniem (N = 1024: 1 355 zamiast 3 400 taktów, ok. 74 000 transformat / s).

**Na płytce (2026-11-16, `--fft 6:32 --gemv-cols 32=2,16=2,8=0`, rdzeń tylko w posit32)**: `test_fft_matches_sw` bit w bit
z modelem rdzenia (`PositSw.fft_core`). posit32, transformaty / s (czas partii) i błąd względny wobec float64:

| N × partia | rdzeń | bloki DFT (poprzednio) | NumPy complex64 |
| --- | ---: | ---: | ---: |
| 64 × 1024 | 152 550 (6,7 ms), 8,6e-9 | 18 823 | 346 570 |
| 1024 × 64 | 11 665 (5,5 ms), 1,2e-8 | 750 | 13 346 |
| 4096 × 16 | **2 874** (5,6 ms), 1,6e-8 | 135 | 2 352 |

- 8–21 × szybciej niż bloki DFT; dla N = 4096 szybciej niż `np.fft` na A53, dla N = 1024 prawie tyle samo.
- Silnik według rachunku taktów to ok. 2,3 ms na partię (np. 1024 × 64: 1024 · 221 taktów), reszta (3–4 ms) to Python:
  kopie danych do bufora urządzenia i z powrotem, synchronizacja cache i otoczka `pn.fft` (na PC ok. 0,5 ms);
  `test_fft_throughput` wypisuje teraz rozbicie z `hw.last_timing`.
- posit16 / posit8 bez rdzenia (bloki DFT przy gemv × 2, posit8 bez gemv: pominięte).

### Wersja 5.6: fazy naraz i FFT w pamięci urządzenia

- **Trzy zestawy banków**: w slocie t wczytanie transformaty t, liczenie t − 1 i zapis t − 2, każde w swoim zestawie;
  po slocie role się obracają. Partia B trwa ok. (B + 2) · max(2N / L, S (N / 4 + 15)) taktów zamiast
  B · (4N / L + S (N / 4 + 15)): posit32 ×2, N = 64 — 114 zamiast 221 taktów na transformatę (symulacja, partia 8),
  N = 1024 — ok. 1 355 zamiast 3 400. Koszt: 3 × banki danych (posit32, N = 4096: ok. 24 BRAM36).
- **`FFT_CTRL.conj`** (bit 2): część urojona negowana przy wczytaniu i zapisie — rdzeń liczy N · ifft = conj(fft(conj x))
  bez kopii danych (negacja positu jest dokładna); `ifft` dzieli potem przez N na silniku.
- **Pamięć urządzenia**: `PositComplex.to_device()` kładzie dane w jednym buforze (…, 2, N) — dokładnie układ rdzenia;
  `.real` / `.imag` to widoki. `pn.fft.fft` / `ifft` na takiej tablicy czyta ją w miejscu (`hw.fft_device_offset`) i zostawia
  wynik w nowym buforze urządzenia w tym samym układzie (`X.on_device`), więc łańcuchy fft → mnożenie → ifft nie wracają
  na procesor; `X.to_host()` kopiuje.

**Na płytce (2026-11-17, 5.6, posit32 ×2)**: bit w bit z modelem; transformaty / s:

| N × partia | z kopiami | **pamięć urządzenia** | NumPy complex64 | silnik: takty na transformatę (rachunek) |
| --- | ---: | ---: | ---: | ---: |
| 64 × 1024 | 275 476 | **640 025** (1,75 ×) | 366 075 | 94 (93) |
| 1024 × 64 | 26 162 | **41 955** (3,3 ×) | 12 877 | 1 389 (1 355) |
| 4096 × 16 | 6 194 | **9 356** (4,0 ×) | 2 349 | 6 752 (7 013 z napełnieniem) |

- Silnik robi dokładnie tyle, ile rachunek slotów: 2,4 × mniej taktów niż 5.5 (np. N = 64: 94 zamiast 221).
- W pamięci urządzenia rdzeń jest szybszy niż `np.fft` we wszystkich rozmiarach — 1,75 × (N = 64) do 4 × (N = 4096);
  z kopiami dopiero od N = 1024 (2 ×), bo kopie do i z bufora (ok. 1,7 ms dla 64 × 1024) to więcej niż sam silnik (0,96 ms).
- Wobec 5.5: N = 64 z kopiami 152 550 → 275 476, w pamięci urządzenia 640 025 (4,2 ×); N = 4096: 2 874 → 9 356 (3,3 ×).
- W wariancie bez kopii zostaje ok. 0,6 ms ponad silnik na wywołanie (synchronizacja cache ok. 0,2 ms i Python) —
  przy większych partiach waży mniej.

## FFT z bloków DFT (bez nowego sprzętu)

`pn.fft.fft / ifft / rfft` i `np.fft.fft` na `PositArray` (`sw/positnp/fft.py`) składają FFT z tego, co silnik już ma:

- **bloki DFT przez matmul**: wiersz [re(R), im(R)] · M (2R × 2R, M = [[C, −S], [S, C]]) = [Re X, Im X] — każde wyjście
  bloku to dokładna suma quire po twiddlach zaokrąglonych do positów, zaokrąglona raz; wierszami są sygnały / kolumny
  macierzy czterech kroków, M siedzi w buforach x silnika, więc przejście A daje 4 kolumny (P = 4) przy małej liczbie poleceń;
- **duże N**: Cooley–Tukey w czterech krokach, N = N1 · N2 (N1 ≤ 64), transpozycje na procesorze; poziomów DFT
  log₆₄ N (2 dla N ≤ 4096);
- **twiddle**: mnożenie zespolone jako `fma(xr, wr, −(xi · wi))`, `fma(xr, wi, xi · wr)` — FMA3 silnika, dwa zaokrąglenia
  na składową;
- `exact=True`: jedno bezpośrednie DFT (N ≤ 2048, K = 2N ≤ 4096) — każde wyjście poprawnie zaokrąglone z dokładnej sumy;
- `ifft` = sprzężenie · fft · sprzężenie / N (1 / N: jedno zaokrąglenie więcej), `rfft` = pierwsze N / 2 + 1 wyjść, wsadowo
  po dowolnej osi, `n` przycina / dopełnia zerami jak NumPy; długości z dużym czynnikiem pierwszym > 2048: błąd.

Dokładność wobec float64 `np.fft.fft` tych samych wejść (największy błąd / największy moduł wyjścia):

| N | posit16 | float16 (radix-2) | posit32 | float32 `np.fft` |
| ---: | ---: | ---: | ---: | ---: |
| 64 | 1,1e-4 | 5,8e-4 | 2,9e-9 | 3,5e-8 |
| 1024 | 3,6e-4 | 8,3e-4 | 6,1e-9 | 4,2e-8 |
| 4096 | 3,3e-4 | 6,9e-4 | 5,3e-9 | 3,3e-8 |

posit32 jest 6–7 × dokładniejszy niż float32, posit16 ok. 2 × niż float16 — dzięki dokładnym blokom (jedno zaokrąglenie
na 64-punktowe DFT zamiast sześciu poziomów motylków). Na płytce działa na bitstreamie 5.2+ (matmul + FMA3).

**Na płytce (2026-11-16, `posit32_16_8_mem` 5.4)**: `test_fft_matches_sw` bit w bit z PositSw na wszystkich trzech
silnikach. Transformaty / s (wsad) i błąd względny wobec float64:

| | posit32 | posit16 | posit8 | NumPy complex64 na A53 |
| --- | ---: | ---: | ---: | ---: |
| N = 64 × 1024 | 18 455 (4,5e-9) | 19 586 (3,1e-4) | 19 940 (7e-2) | 234 000 – 385 000 |
| N = 1024 × 64 | 740 (7,1e-9) | 826 (4,5e-4) | 867 (1e-1) | 12 700 – 14 100 |
| N = 4096 × 16 | 134 (6,9e-9) | 143 (5,2e-4) | 149 (1,1e-1) | 2 000 – 2 400 |

- 13–19 × wolniej niż `np.fft` — FFT z bloków DFT kosztuje 4 · N · ΣNᵢ MAC (N = 64: 16 K MAC na transformatę,
  1024 transformat = 16,8 M MAC = 43 ms silnika z 55 ms), a `np.fft` robi O(N log N) działań we float SIMD;
  wartość tej ścieżki to dokładność (posit32 6–7 × lepszy niż float32), nie szybkość.
- Mniejszy blok (`radix=16`: N = 4096 = 16³, 192 zamiast 512 MAC na wyjście) da ok. 2,5 × mniej pracy silnika kosztem
  jednego poziomu twiddli (dwa zaokrąglenia więcej); szybciej niż `np.fft` byłby dopiero sprzętowy rdzeń FFT.
- posit8 (3–4 bity ułamka) nie nadaje się do FFT poza zabawą: błąd 7–11 %.

## FIR / splot: GEMV z oknem (wersja 5.4)

y[i] = Σⱼ x[i + j] · h[j] to gemv, w którym wiersz A jest oknem sygnału: wiersz r = x[r·P .. r·P + K), K = T + P − 1,
a kolumna p ma odczepy przesunięte o p (bufor x_p[m] = h[m − p]) — jeden wiersz daje P kolejnych wyjść.
Nowe jest tylko okno: sygnał idzie z pamięci raz, do bufora kołowego, a nakładające się wiersze czyta silnik.

```
OP_XLOAD (cols · K):  x_p[m] = h[m − p] (zera poza)  -->  bufory x_0 .. x_{P-1}
OP_GEMV, GEMV_MODE.window (MEM_LEN = (M − 1) · cols + K):
   m_axi R --> x po kolei --> bufor kołowy (2 maxK)          zapis czeka, gdy nadpisałby element >= rowStart
   wiersz r: element rowStart + m (gdy już zapisany) --+--> quire kolumny 0 (tylko m w [0, T))   --> y[r·P]
                                                       +--> quire kolumny 1 (tylko m w [1, 1 + T)) --> y[r·P + 1] ...
```

- kolumna liczy tylko elementy w zakresie swoich odczepów, resztę wiersza dostaje jako 0: zera w buforze x wciągałyby
  inaczej NaR z sąsiednich wyjść (0 · NaR = NaR) — wyszło na posit8 z rozkładem `stress`;
- rejestr `GEMV_MODE` (0xE0) bit 0 window, wersja 5.4; `err_shape` dla `MEM_LEN ≠ (M − 1)·cols + K`, M = 0, cols > K;
  koszt: bufor kołowy 2 · maxK × n (posit32: 256 Kb), reszta to gemv z 5.2;
- przepustowość: P · T MAC na K = T + P − 1 taktów — przy T ≫ P prawie P MAC / takt, czyli P / K wyjść na takt;
- Python: `np.convolve`, `np.correlate` (tryby full / same / valid jak w NumPy), `pn.convolve`, `pn.correlate`,
  `hw.correlate(x, h)`, `hw.time_correlate`; NaR w odczepach Python wstawia jako 0 i ustawia NaR dokładnie
  w wyjściach, w których ten odczep jest składnikiem sumy (jak `nan` w NumPy); odczepy dłuższe niż maxK − P + 1:
  PositSw.

Symulacja (`PositVecMemTestplan.mem_fir`): posit32 ×1 (P = 1) i ×2, posit16 ×4, posit8 ×8 (P = 4); T od 1 do
maxK − P + 1, cols 1 … P, sygnał wielokrotnie dłuższy niż bufor kołowy; każde wyjście bit w bit z `QuireRef`, sygnał
czytany raz (liczba słów R), odrzucenia kształtu. 61 odczepów posit32, P = 4: 256 wyjść w 4 156 taktach — **3,76 MAC / takt**.

**Na płytce (2026-11-16, `posit32_16_8_mem` 5.4, P = 4)**, 1 048 576 próbek, M wyjść / s (w nawiasie M MAC / s):

| odczepy | posit32 | posit16 | posit8 | MAC / takt silnika (teoria 4T / (T + 3)) | NumPy float32 `np.convolve` |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 16 | 17,53 (281) | 19,62 (314) | 20,16 (323) | 3,37 (3,37) | 5,71 |
| 64 | 5,77 (369) | 5,85 (374) | 5,90 (378) | 3,82 (3,82) | 2,76 |
| 256 | 1,53 (392) | 1,54 (393) | 1,54 (394) | 3,95 (3,95) | 0,93 |

- Silnik robi dokładnie tyle, ile wynika z okna: P · T MAC na T + P − 1 taktów; strata to tylko P − 1 taktów na wiersz.
- 1,7–3,5 × szybciej niż float32 `np.convolve` na A53 (metoda bezpośrednia), a każde wyjście jest dokładne w quire.
- Przy krótkich filtrach (16 odczepów) waży już Python i kopia sygnału (posit32 281 z 337 M MAC/s silnika); szybciej dla
  krótkich T dałoby więcej kolumn (P = 8: 8 · 16 / 23 = 5,6 MAC / takt).

## Funkcje elementarne z tablicy (wersja 5.3)

posit16 ma 65 536 wartości, posit8 — 256: każdą funkcję jednej zmiennej da się stablicować w całości, poprawnie
zaokrągloną. Tablice liczy Python (`positnp/funcs.py`), silnik tylko czyta T[a] z BRAM — tak samo szybko dla
`exp`, `log`, `sin`, `tanh` czy `sigmoid`.

```
OP_TABLE + TABLE_CTRL.load (MEM_LEN = 2^n):  pamięć --> T[0 .. 2^n) we wszystkich kopiach (po 1 elemencie na takt)
OP_TABLE:  grupa L elementów a --> porcje po 2 C --> kopia k: port A = T[a_2k], port B = T[a_2k+1] (1 takt) --> grupa y --> zapis
```

- tablica w C kopiach dwuportowej BRAM: 2 C odczytów na takt; domyślnie C = L / 2 (posit16 ×4: 2 kopie, 4 odczyty /
  takt; posit8 ×8: 4 kopie w LUTRAM); posit16: 64 K × 16 b = 32 BRAM36 na kopię (`ram_style = block`, kaskada URAM
  byłaby wolniejsza); `VecKv260 --table-copies C`, `0`: bez tablicy; OOC: `vec_p16_mem_L4_table`;
- rejestry: `TABLE_CTRL` (0xD8) bit 0 load, `TABLE_CFG` (0xDC, RO) = 2 C << 8 | n; OP_TABLE = 15 (ostatni kod 4-bitowego OP),
  bit 15 maski operacji w CONFIG; ładowanie z `MEM_LEN ≠ 2^n` → `err_shape`; wersja 5.3;
- Python: `np.exp`, `np.log`, `np.log1p`, `np.sin`, `np.tanh`, `np.arctan`, ... (20 ufunc NumPy) i `pn.sigmoid`, `pn.func(name, x)`;
  `PositVecHw.func` ładuje tablicę raz (128 KB dla posit16, ok. 0,7 ms) i trzyma ją, dopóki nie trzeba innej funkcji;
  tablice w pamięci urządzenia działają bez kopii; posit32 i silniki bez tablicy: `positnp.funcs` w programie;
- poprawność: wartość z float64 libm zaokrąglona raz; każdą pozycję sprawdza zaokrąglenie wartości zaburzonej o ±2⁻⁵⁰
  (ok. 4 ulp float64, więcej niż błąd libm) — wszystkie 21 funkcji × posit8 / posit16 mają 0 pozycji niepewnych
  (`funcs.uncertified`); jedyne remisy to wyniki dokładne (exp2 z liczby całkowitej, log2 potęgi dwójki);
- reguły positów: NaR → NaR, bieguny i poza dziedziną → NaR (log 0, arctanh 1), nadmiar → ±maxpos, niedomiar
  niezerowego wyniku → ±minpos (exp(−10⁹) = minpos).

Symulacja (`PositVecMemTestplan.mem_table`): posit16 ×1 / ×4 (1 i 2 kopie), posit8 ×8 (4 i 1 kopia), posit8 ×2 — losowa
tablica, przeładowanie drugą, długości 1 … 2L + 1, 97 i losowe, przeplot z `add`; wynik bit w bit, WSTRB, CNT_ELEMENTS,
odrzucenie złej długości ładowania. Przepustowość (4096 elementów):

| | posit16 ×1, C = 1 | posit16 ×4, C = 1 | posit16 ×4, C = 2 | posit8 ×8, C = 4 | posit8 ×8, C = 1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| elementy / takt | 0,99 | 1,97 | 3,86 | 7,54 | 1,98 |

**Na płytce (2026-11-15, `posit32_16_8_mem` 5.3: posit16 ×4 z 2 kopiami tablicy, posit8 ×8 z 4 kopiami)**, `exp`
na 4 194 304 elementach, M elementów / s:

| | posit16 (4 odczyty / takt) | posit8 (8 odczytów / takt) |
| --- | ---: | ---: |
| silnik, z kopiami | 222,8 | 498,2 |
| silnik, pamięć urządzenia | **331,9** | **647,8** |
| NumPy: ten sam odczyt tablicy `T[a]` na A53 | 33,3 | 56,2 |
| NumPy float32 `np.exp` na A53 | 23,3 | 23,4 |

- Silnik jest 10–12 × szybszy niż ten sam odczyt tablicy w NumPy (128 KB tablicy posit16 nie mieści się w L1, każdy
  element to losowy dostęp) i 14–28 × szybszy niż float32 `np.exp`, a wynik jest poprawnie zaokrąglony.
- Pamięć urządzenia: 83 % (posit16) i 81 % (posit8) teoretycznych 400 / 800 M/s przy 100 MHz; reszta to synchronizacja
  cache i polecenie. Każda funkcja kosztuje tyle samo — `sin`, `log` czy `sigmoid` jak `exp`.
- matmul na tym bitstreamie bez zmian (386,6 / 388,0 / 388,6 M MAC/s): tablica nie zabiera zegara.
- OOC (`vec_p16_mem_L4_table`): 9 331 LUT, 60 BRAM36 (2 kopie po 30), 130,3 MHz — tablica kosztuje BRAM, nie logikę.

## Pamięć urządzenia: tablice bez kopii

Przy szerszym silniku posit32 ogranicza już tylko procesor (kopie do i z bufora). Bufor u-dma-buf silnika z masterem
dzieli się teraz na dwie części: na początku obszar roboczy podwójnego bufora (`STAGING_MAX` = 24 MB, najwyżej
połowa bufora), powyżej **sterta urządzenia** (`positnp/devmem.py`, bloki po 4 KB).

```python
with pn.using(hw):
    xd = x.to_device()                # jedna kopia do pamięci urządzenia (albo pn.to_device(x))
    y = xd * xd - xd / 3 + 1          # cztery polecenia, zero kopii: wejścia i wyniki w pamięci urządzenia
    y.on_device, y.to_float(), np.sum(y)   # zwykła tablica NumPy (widok bufora); y.to_host() kopiuje
```

- działanie, którego choć jedno wejście jest w pamięci urządzenia, idzie jednym poleceniem po całej tablicy;
  wejścia z hosta są kopiowane do tymczasowych bloków, wynik dostaje nowy blok w pamięci urządzenia
  (`device_results = True`: tak samo dla wejść z hosta); bez miejsca na stercie — zwykła ścieżka z kopiami;
- koherencja: przed poleceniem `sync_for_device` wejść (procesor mógł je zmienić) i wyniku, po nim `sync_for_cpu`
  wyniku; bloki 4 KB, więc dwie tablice nigdy nie dzielą linii cache;
- blok zwalnia się, gdy znika ostatni widok NumPy tablicy (`weakref.finalize`); `hw.heap.stats()`;
- redukcje (`np.sum`, `np.dot`) na tablicach w pamięci urządzenia też bez kopii;
- pojemność: przy `-buf_mb 64` sterta ma 40 MB (np. dwie tablice posit32 po 4 M elementów + wynik nie zmieszczą
  się razem) — do dużych obliczeń `-buf_mb 256` albo więcej (CMA: `grep Cma /proc/meminfo`);
- przed `pn.board.unload()` warto usunąć tablice w pamięci urządzenia (mapowanie bufora zostaje, dopóki żyją).

Na płytce (2026-11-15, `posit32_16_8_mem`, posit32 ×2 / posit16 ×4 / posit8 ×8, `test_device_memory_zero_copy`),
M elementów / s (posit32: 2,6 M elementów — tyle mieści sterta 40 MB; posit16 / posit8: 4 M):

| | posit32 ×2 | posit16 ×4 | posit8 ×8 | NumPy float32 add |
| --- | ---: | ---: | ---: | ---: |
| add, z kopiami → **bez kopii** | 70 → **147** | 154 → **297** | 242 → **538** | ok. 250 |
| mul | 90 → 147 | 195 → 294 | 376 → 555 | |
| FMA | 91 → 146 | 198 → 223 | 225 → 236 | |
| div (ogranicza silnik) | 71 → 72 | 119 → 121 | 174 → 182 | |

- Bez kopii zostaje praca silnika i synchronizacja cache: add posit32 14,1 ms silnika (0,54 taktu / element przy 2 torach)
  + 3,1 ms synchronizacji na 2,6 M elementów.
- posit16 add w pamięci urządzenia jest już szybsze niż NumPy float32 na A53, posit8 ponad 2 ×.

## Szerszy silnik: L elementów na takt (tory elementowe, `lanes`)

W trybie z masterem silnik może liczyć L elementów na takt: master czyta i zapisuje grupy po L elementów
(L · n bitów, jedna grupa na takt), a obok siebie pracuje L kopii `PositStreamAlu`.

```
m_axi R --> a, b, c: grupy po L --> kolejka toru 0 --> PositStreamAlu 0 --+
                                --> kolejka toru 1 --> PositStreamAlu 1 --+--> grupa wynikow --> m_axi W
                                ...                                       |    (ostatnia: WSTRB)
                                --> kolejka toru L-1 -> PositStreamAlu L-1-+
            redukcje: grupa -> elementy po kolei -> quire (1 element / takt)
```

- grupa idzie do torów, gdy wszystkie kolejki torów mają miejsce; wyniki łączy StreamJoin, każdy tor zachowuje
  kolejność, więc grupy wychodzą w kolejności; ostatnia grupa może być niepełna (len mod L), zapis maskuje ją WSTRB,
  liczniki (CNT_ELEMENTS, flagi) nie liczą torów za końcem tablicy;
- każdy tor ma własne tory wielotaktowe (`slowUnits`), więc div / sqrt / FMA też przyspieszają L razy;
- tor 0 obsługuje też pojedyncze działania z rejestrów; quire zostaje przy 1 elemencie na takt;
- sensowne L: szyna HP 128 b czyta 16 B na takt, a działanie dwuargumentowe potrzebuje 2 · L · n / 8 B →
  posit32 ×2, posit16 ×4, posit8 ×8 (FMA3 z trzema tablicami będzie wtedy ograniczone pamięcią);
- ustawienie: silnik `n:es:k:u:L`, np. `VecKv260 32:2:4:4:2 16:2:4:2:4 8:2:4:1:8`; ENGINE_CFG[23:16] = L,
  w Pythonie `engine_cfg()["lanes"]`, w `pn.board.describe()` „elements/clock”; sterownik bez zmian.

Symulacja (`PositVecMemTestplan.mem_lanes`, model pamięci z losowymi opóźnieniami): wszystkie operacje
bit w bit dla długości 1 … 2L + 1 i losowych, redukcje, WSTRB, CNT_ELEMENTS; add 4096 elementów:

| | posit32, L = 2 | posit16, L = 4 | posit8, L = 8 |
| --- | ---: | ---: | ---: |
| takty | 2 124 | 1 083 | 588 |
| elementy / takt | 1,93 | 3,78 | 6,97 |

**Na płytce (2026-11-15, `posit32_16_8_mem`: posit32 ×2 tory i 4 tory div / sqrt / FMA, posit16 ×4 i 2 tory,
posit8 ×8)**: `test_board_vec.py` przechodzi w całości. M elementów / s, 4 194 304 elementy, podwójny bufor
(w nawiasie: jeden tor elementowy, podwójny bufor):

| | posit32 ×2 | posit16 ×4 | posit8 ×8 | NumPy float32 |
| --- | ---: | ---: | ---: | ---: |
| add (porcja 1–4 MB) | **121,3** (92,0) | **228,8** (94,0) | **399,8** (95,8) | 244 / 287 / 338 |
| div | 75,0 | 124,0 | 184,1 | |
| sqrt | 76,2 | 125,7 | 186,2 | 225 |
| FMA / AXPY | 115,6 / 116,4 | 206,4 / 207,6 | 238,1 / 238,0 | |
| FMA3 (trzy tablice) | 71,7 | 139,2 | 234,5 | |
| F2P / P2F | 166,8 / 166,6 | | | |
| quire dot | 93,5 | | 96,1 | 240 |

- Silnik liczy dokładnie tyle, ile wynika z konfiguracji: add 0,53 / 0,27 / 0,13 taktu na element,
  div 1,25 / 0,75 / 0,50 (10 / 6 / 4 takty przez L · slowUnits), FMA posit8 0,38 (3 takty / 8 torów).
- **posit8 add jest szybsze niż NumPy float32 na A53**; posit16 osiąga ok. 80 %.
- posit32 add: silnik czeka już tylko ok. 1 ms, cały czas to kopie (ok. 27 ms z 35) — dalej tylko bez kopii
  (bity `PositArray` od razu w buforze u-dma-buf).
- FMA3 posit32 0,8 taktu / element: trzy tablice po 8 B na takt przekraczają 16 B / takt szyny HP.
- quire (1 element / takt) jest teraz najwolniejszą ścieżką: ok. 95 M / s dla wszystkich formatów.
- Porcje: przy szerszym silniku stały koszt porcji (synchronizacja ok. 50 µs, polecenie) waży więcej; domyślnie
  8 porcji po co najmniej 1 MB (było 16 po 256 KB).

OOC (`vec_p32_mem_L2`): 11 842 LUT, 5 867 FF, 12 DSP, 104,8 MHz po routingu (z jednym torem 8 405 LUT, 113,5 MHz) —
drugi tor to ok. +3 400 LUT i +4 DSP; ścieżka krytyczna ta sama (dodawanie ALU), zapas przy 100 MHz 0,45 ns.

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

## Kilka silników na płytce (2026-11-14)

`posit32_16_8_vec` (posit32 z 4 torami, posit16, posit8): `test_board_vec.py` przechodzi na każdym silniku,
także z silnikami używanymi naprzemiennie; `pn.board.load` / `describe` działa z prawdziwym xmutil.

| 4 194 304 elementy, M el. / s | posit32 | posit16 | posit8 |
| --- | ---: | ---: | ---: |
| add | 25,5 | 39,3 | 51,4 |
| div | 22,7 (4 tory) | 14,3 | 21,3 |
| quire dot | 41,1 | 55,5 | 62,7 |

- add: DMA + silnik zawsze 42 ms (1 element / takt); węższy format to mniej pakowania i kopii po stronie
  procesora (posit8: 29 + 6 ms wobec 83 + 31 ms dla posit32), stąd 2 × więcej elementów na sekundę.
- posit16 i posit8 z jednym torem: div ograniczony silnikiem (6 i 4 takty / element) — przy następnym
  buildzie warto dać im po 2 tory (`16:2:4:2 8:2:4:2`).

## Tory wielotaktowe (`slowUnits`, wersja 4.1)

Dzielenie, sqrt i FMA liczą jednostki iteracyjne (`divCycles` = 10, `sqrtCycles` = 10, `fmaCycles` = 3 takty
dla posit32, k = 4). Silnik może mieć ich kilka kopii — **torów**: kolejne elementy idą na zmianę do kolejnych
torów, a kolejka biletów wydaje wyniki w kolejności poleceń. Add / sub / mul / konwersje dalej 1 element / takt
w jednym potoku (ich nie ma sensu powielać przed etapem 6 — ogranicza procesor, nie silnik).

```
                    +--> szybki potok (add, sub, mul, F2P, P2F) --> kolejka wyników (fastDepth) --+
dekoder -> prepare -+                                                                            +--> bilety -> enkoder
 (raz na element)   +--> tor 0 | tor 1 | ... | tor L-1  (kroki div / sqrt / FMA + finish)  ------+   (kolejność poleceń)
```

| | 1 tor | L torów |
| --- | ---: | ---: |
| div, sqrt (posit32, k = 4) | 10 taktów / element | 10 / L (np. 2,5 przy L = 4) |
| FMA, AXPY | 3 takty / element | max(1, 3 / L) |

- Wspólne dla torów: dekodery i `prepare` (np. mnożarka FMA w DSP — tory nie dokładają DSP);
  powielone: rejestry stanu, kroki rekurencji, wyrównanie FMA i `finish`.
- Wolne operacje nie blokują już potoku: szybkie wyniki za nimi czekają w kolejce (do `fastDepth` = 4).
- Przy 1 torze odstępy są takie jak wcześniej (testy `alu_div_latency`, `sqrt_p32` bez zmian).
- Ustawienie przy generowaniu: silnik `n:es:k:u`, np. `VecKv260 32:2:4:4 16:2:4:2`.
  Odczyt: rejestr ENGINE_CFG (0x5C): [7:0] k, [15:8] tory; w Pythonie `PositVecHw.engine_cfg()`.
- Na płytce (2026-11-14, `posit32_16_8_vec`: posit32 z 4 torami, posit16 i posit8 z jednym):

  | 4 194 304 elementy | M el. / s (było przy 1 torze) | DMA + silnik | silnik |
  | --- | ---: | ---: | --- |
  | posit32 div | 22,7 (8,4) | 104,9 ms (419,5) | 2,5 taktu / element, dokładnie 4 × szybciej |
  | posit32 sqrt | 26,7 (8,9) | | 2,5 taktu / element |
  | posit32 FMA / AXPY | 34,3 / 34,4 (20,2) | | 1 takt / element |
  | posit32 add | 25,5 | 42,1 ms | bez zmian (1 / takt; ogranicza procesor) |
  | posit16 div (1 tor) | 14,3 | 251,7 ms | 6 taktów / element (`divCycles` posit16, k = 4) |
  | posit8 div (1 tor) | 21,3 | 167,9 ms | 4 takty / element |

  Dla posit32 div przestał być ograniczony silnikiem: 105 ms DMA + silnik wobec ok. 80 ms pracy procesora
  (pakowanie, kopie), więc dalsze tory dla posit32 dadzą już niewiele przed etapem 6.
- Koszt (OOC, posit32): 4 tory = 10 469 LUT, 4 926 FF, 8 DSP, 113 MHz po routingu, wobec 6 550 LUT przy jednym
  torze — ok. 1 300 LUT na każdy dodatkowy tor, bez nowych DSP i bez zmiany Fmax (`hw/top-modules/ooc/README.md`).

## Bitstreamy do pakietów (extras `positnp`)

Jedna aplikacja PL na zastosowanie; każdy bitstream liczy wszystko (czego nie ma w sprzęcie, liczy `PositSw`
z tymi samymi bitami), różnią się tym, co liczą szybko. Konfiguracje są w jednym miejscu: `hw/spinal/main/VecReleases.scala`.
Wszystkie naraz, dwie komendy (Windows i Linux):

```
sbt "top-vec/runMain posit.top.vec.VecReleases"                      # z hw/ (np. w kontenerze SpinalHDL)
vivado -mode batch -nojournal -nolog -source release_builds.tcl      # z hw/top-modules/vec/vivado
```

- `VecReleases` generuje RTL każdej konfiguracji do osobnego katalogu `hw/gen/release/<nazwa>/` (+ `release.txt`:
  silniki, bufor, extra); `VecReleases <nazwa> ...` — tylko wybrane, `VecReleases --list` — lista.
- `release_builds.tcl` buduje po kolei każdą konfigurację z `hw/gen/release/` w osobnym procesie Vivado (`build.tcl`
  z `-rtl`); błąd jednej (WNS < 0, placer) nie zatrzymuje pozostałych; na końcu podsumowanie, też w `out/release_summary.txt`.
  Opcje po `-tclargs`: nazwy konfiguracji, `-jobs n`, `-threads n`, `-dry` (tylko komendy), `-out <dir>` (projekty;
  na Windows przy błędach długości ścieżek krótki katalog bez spacji, np. `-out C:/vb`).
- Wynik: `sw/positnp/overlays/<nazwa>/` (te katalogi są w gicie, reszta `overlays/` nie); projekty i logi:
  `vivado/out/<nazwa>/` (`build.log`).

| nazwa | extra | silniki | VecKv260 | dla kogo | stan |
| --- | --- | --- | --- | --- | --- |
| `posit32_16_8_mem` | `kv260` (domyślny) | 32 + 16 + 8 | gemv × 4 we wszystkich, tablice p16 / p8 | eksperymenty, porównania formatów, przykłady | sprawdzona (5.4); 5.6 zbudowana |
| `posit32_16_8_dsp` | `kv260-dsp` | 32 + 16 + 8 | `--fft 6:32 --gemv-cols 32=2,16=2,8=0` | FFT, FIR, korelacja | sprawdzona (5.6) |
| `posit16_8_ml` | `kv260-ml` | 16 + 8 | `--gemv-cols 16=8,8=8` | sieci w 8 / 16 bitach: gemm, sigmoid / tanh / exp z tablic | sprawdzona (2026-11-18) |
| `posit32_16_linalg` | `kv260-linalg` | 32 + 16 | `--gemv-cols 32=8,16=4 --table-copies 0` | układy równań, mieszana precyzja | zbudowana |
| `posit32_16_dsp` | (`kv260-dsp` v2) | 32 + 16 | `--fft 6 --gemv-cols 2` | FFT także w posit16 (porównania z float16) | eksperyment, zbudowany |

Pierwszy pełny przebieg (2026-11-17, 642 min): wszystkie pięć z WNS ≥ 0 — `posit16_8_ml` 13 min,
`posit32_16_8_mem` 19 min, `posit32_16_dsp` 22 min, `posit32_16_linalg` 36 min, `posit32_16_8_dsp` 549 min (placer
z rdzeniem FFT i gemv × 2 przy prawie pełnym układzie). `posit32_16_8_mem` wyszedł wtedy kopią `posit32_16_8_dsp`
bit w bit (RTL z poprzedniej konfiguracji we wspólnym `hw/gen/p<n>/`), przebudowany 2026-11-18 (64 DSP, 85,5 BRAM
wobec 84 / 105,5 w dsp); osobne katalogi `hw/gen/release/<nazwa>/` z `VecReleases` wykluczają taki błąd. Testy na
płytce z tymi bitstreamami: 244 passed, 13 skipped (2026-11-18). Pakiety PyPI robi z nich
`sw/packaging/make_overlay_packages.py`.

Silniki zawsze `32:2:4:4:2`, `16:2:4:2:4`, `8:2:4:1:8` (jak w sprawdzonych bitstreamach). Dwa silniki: `-buf_mb 96`
(razem 192 MB CMA, tyle co trzy po 64). Elaboracja wszystkich pięciu przechodzi (2026-11-17); sterownik z gemv
na 8 i 16 kolumn sprawdzony na atrapach (`matmul`, `np.convolve` bit w bit z `PositSw`).

Szacunek miejsca (z OOC wyżej): quire posit32 ok. 2 900 LUT, posit16 / posit8 proporcjonalnie mniej (256 / 128 b);
- `ml`: bez silnika posit32 (największego) — 16 quire posit16 i 16 posit8 powinno się zmieścić z zapasem; 8 MAC / takt
  na silnik (ok. 800 M MAC/s), 2 × więcej niż `kv260`. Gdy się zmieści łatwo: `--gemv-cols 16=16,8=16` (P ≤ 16,
  elaboracja przechodzi). Gdy WNS < 0: `16=8,8=4` albo `16=4,8=8`.
- `linalg`: posit32 P = 8 to wg OOC ok. +20 tys. LUT względem P = 4, ale bez silnika posit8 i bez tablic (ok. 60 BRAM36
  mniej). Gdy się nie zmieści: `--gemv-cols 32=6,16=4`, potem `32=4,16=4`.
- `posit32_16_dsp`: rdzeń FFT w posit16 zamiast silnika posit8; gdy placer odmówi: `--gemv-cols 32=2,16=1`, potem
  `32=1,16=1`; gdy dalej nie — zostaje `posit32_16_8_dsp`.

Zmiana konfiguracji (np. krok awaryjny): argumenty w `VecReleases.scala`, potem `VecReleases <nazwa>` i
`release_builds.tcl -tclargs <nazwa>`.

## Krok po kroku

1. RTL (z `hw/`) — jeden plik na silnik, `hw/gen/p<n>/PositVecAxi_p<n>.v`:
   ```
   sbt "top-vec/runMain posit.top.vec.VecKv260"            # posit<32,2>, master AXI4 (etap 6)
   sbt "top-vec/runMain posit.top.vec.VecKv260 32 16 8"    # trzy silniki; silnik = n[:es[:k[:u]]], np. 16:2:1
   sbt "top-vec/runMain posit.top.vec.VecKv260 --dma 32"   # stara ścieżka AXI4-Stream + AXI DMA
   sbt "top-vec/runMain posit.top.vec.VecKv260 32:2:4:4:2 16:2:4:2:4 8:2:4:1:8"   # szerszy silnik (L = 2 / 4 / 8)
   sbt "top-vec/runMain posit.top.vec.VecKv260 32:2:4:4"   # posit32 z 4 torami div / sqrt / FMA
   ```
2. Bitstream i aplikacja (z `hw/top-modules/vec/vivado`):
   ```
   vivado -mode batch -nojournal -log build.log -source build.tcl                               # posit32_mem
   vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -engines "32 16 8"   # posit32_16_8_mem
   vivado ... -tclargs -io dma -engines "32 16 8"                                              # posit32_16_8_vec
   ```
   Wynik: `sw/positnp/overlays/<nazwa>/` (`.bit.bin`, `.dtsi`, `shell.json`, `.json`).
   `tclsh overlay_files_test.tcl` sprawdza generowanie `.dtsi` / `.json` bez Vivado (z `dtc`).
3. Na płytce, z `sw/`:
   ```
   scripts/build_udmabuf.sh                    # raz na jądro: moduł u-dma-buf (git, nagłówki jądra); po aktualizacji jądra znowu
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

## Wentylator

Sygnał `fan_en_b` wentylatora KV260 (pin A12) idzie z PS przez PL: w fabrycznej aplikacji wyjście TTC0 (EMIO, bit 2)
jest z nim połączone, a Linux steruje obrotami przez `pwm-fan`. Bitstream bez tego połączenia zostawia pin niesterowany
i wentylator kręci się na maksa. `build.tcl` (vec i alu) dokłada je domyślnie (`common/vivado/kv260_fan.tcl`:
TTC0 z wyjściem fali na EMIO, `emio_ttc0_wave_o[2]` → port `fan_en_b`, ograniczenia pinu A12 LVCMOS33);
`-fan 0` wyłącza. Krzywa obrotów zostaje po stronie Linuksa, jak w fabrycznej aplikacji.

## build.tcl

| opcja `-tclargs` | domyślnie | |
| --- | --- | --- |
| `-fan 0\|1` | `1` | wentylator: PWM z TTC0 przez EMIO na pin `fan_en_b` (bez tego po wgraniu kręci się na maksa) |
| `-io master\|dma` | `master` | etap 6: master AXI4 silnika (bez AXI DMA, przerwanie) albo stara ścieżka z AXI DMA |
| `-engines "<n> ..."` | `32` | silniki: 8, 16, 32, każdy co najwyżej raz (`tdata` AXI DMA w pełnych bajtach); `-n <n>` = jeden |
| `-name <nazwa>` | `posit<n>[_<n>…]_mem` (`_vec` z `-io dma`) | nazwa aplikacji dla xmutil |
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
| 0x5C | ENGINE_CFG | (4.1) [7:0] k, [15:8] tory div / sqrt / FMA |
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
| tory | `posit-vector/.../PositAluUnitsTestplan` | div / sqrt / FMA na 2 i 4 torach: średni odstęp = cycles / L; przeplot wszystkich operacji na 3 torach (kolejność, losowe valid / ready); add / mul nadal 1 / takt; `PositStreamAlu` z 4 torami ok. 4 razy szybszy na div |
| etap 5, IP | `posit-kv260/.../PositVecStage5Testplan` | sqrt, FMA, AXPY (też z `scalar_b`), F2P, P2F na tablicach przez AXI; sqrt i FMA przez rejestry |
| etap 6 / 7a, IP | `posit-kv260/.../PositVecMemTestplan` | master AXI4 z modelem pamięci: wszystkie operacje, tory, odrzucenia, SLVERR, irq; gemv: y kontra `QuireRef` wiersz po wierszu, kształty brzegowe, err_shape, 1 MAC / takt |
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
