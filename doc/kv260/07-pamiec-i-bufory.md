# 7. Pamięć i bufory: od kopii do tablic bez kopii

Ten rozdział jest najważniejszy dla wydajności. Silnik w PL widzi tylko **adresy fizyczne** DDR i nie
widzi pamięci podręcznej procesora. Wszystkie usprawnienia z etapów 2–7 to odpowiedzi na dwa pytania:
gdzie leżą dane, które czyta silnik, i ile razy procesor musi je dotknąć.

## 7.1 Adres wirtualny, fizyczny i dlaczego potrzebny jest specjalny bufor

Tablica NumPy w procesie Pythona ma **adres wirtualny**. Jądro tłumaczy go na strony fizyczne po 4 KB,
które zwykle są **rozrzucone** po DDR. Master w PL (DMA albo nasz mover) nie ma MMU — podaje adresy
fizyczne i czyta je po kolei. Seria 256 B pod kolejne adresy fizyczne musi więc trafić w kolejne bajty
naszych danych.

Potrzebny jest zatem bufor **fizycznie ciągły**, którego adres fizyczny znamy. Daje go:

- **CMA** (Contiguous Memory Allocator) — obszar DDR, który jądro rezerwuje na ciągłe przydziały
  (`grep Cma /proc/meminfo`);
- moduł **u-dma-buf** (github.com/ikwzm/udmabuf) — dla węzła `compatible = "ikwzm,u-dma-buf"` z device tree
  przydziela z CMA bufor o zadanym rozmiarze i wystawia:
  - `/dev/udmabuf-posit-p<n>` — do `mmap`,
  - `/sys/class/u-dma-buf/udmabuf-posit-p<n>/phys_addr`, `size` — adres fizyczny i rozmiar,
  - `sync_offset`, `sync_size`, `sync_direction`, `sync_for_device`, `sync_for_cpu` — zarządzanie cache.

```
proces Pythona                         DDR
  np.frombuffer(mmap) ───── mmap ─────► [ bufor u-dma-buf: phys_addr .. phys_addr + 64 MB ]  (ciągły, z CMA)
  (adres wirtualny)                       ▲ ten sam bufor widzi silnik pod adresem fizycznym
                                          │ MEM_SRC_A = phys_addr + offset
                                        silnik (HP0)
```

`UDmaBuf.view(dtype, offset, count)` (`udmabuf.py`) to `np.frombuffer` na mapowaniu — **zwykła tablica
NumPy bez kopii**, której bajty silnik widzi pod `phys_addr + offset`. Rozmiar bufora: build.tcl
`-buf_mb` (domyślnie 64 MB na silnik), wpisywany do `.dtsi`.

## 7.2 Pamięć podręczna — problem spójności

A53 ma cache L1/L2. Port **HP0 nie jest spójny**: silnik czyta DDR, nie pytając cache procesora.
Dwa zagrożenia:

1. Procesor zapisał dane wejściowe — są w cache (dirty), w DDR jeszcze nie. Silnik przeczyta **stare** dane.
   → przed poleceniem: **clean** (wypchnij dirty linie do DDR) = `sync_for_device`.
2. Silnik zapisał wynik do DDR, a procesor ma w cache **starą kopię** tych linii.
   → po poleceniu: **invalidate** (unieważnij linie) = `sync_for_cpu`.

Jest też zagrożenie subtelniejsze: dirty linia procesora nad obszarem **wyniku** może zostać
wypchnięta do DDR *po* zapisie silnika i nadpisać wynik. Dlatego przed poleceniem synchronizujemy
także obszar wyniku (`sync_for_device(dst, …, 0)` w `_mem_device`).

Dlaczego nie bufor bez cache (`O_SYNC`)? Na arm64 takie mapowanie to pamięć typu Device, a `memcpy`
i NumPy używają niewyrównanych dostępów i `DC ZVA` — kończy się SIGBUS (komentarz w `udmabuf.py`).
Zostajemy przy pamięci cache'owanej i jawnej synchronizacji.

Kierunek (`sync_direction`): 1 = do urządzenia (clean), 2 = z urządzenia (invalidate), 0 = oba.

## 7.3 Koszt synchronizacji i pierwsza optymalizacja

Pierwsza wersja robiła każdą synchronizację jako 4 × open / write / close na plikach sysfs:
**ok. 1 ms na wywołanie**, niezależnie od rozmiaru obszaru. Przy wielu porcjach (7.5) to dominowało.

Poprawka (`UDmaBuf._set`): deskryptory plików sysfs otwierane **raz**, zapis przez `os.pwrite`,
a niezmienione atrybuty (`sync_size`, `sync_direction`) nie są zapisywane ponownie.
Efekt: **para sync_for_device + sync_for_cpu: 57 µs** (było ok. 2 ms). `UDmaBuf.time_sync()` mierzy to na płytce.

Lekcja: koszt często nie leży w samej operacji (clean 4 MB to ułamek ms), tylko w stałym narzucie wywołania.

## 7.4 Kopia przez bufor roboczy (staging) — etapy 2–6

Najprostszy schemat dla tablic użytkownika (zwykła pamięć procesu):

```
tablica a (proces) ──kopia──► bufor[off_a] ┐
tablica b (proces) ──kopia──► bufor[off_b] ┤  sync_for_device → polecenie MEM → sync_for_cpu
                                            ┘
bufor[off_a] ──kopia──► wynik (proces)        (wynik silnik pisze w miejsce a: MEM_DST = SRC_A)
```

- Etap 2 (DMA) zamiast dwóch kopii miał **przeplatanie** {b, a} — 82,9 ms na 4 M elementów.
- Etap 6: dwie zwykłe kopie ciągłe — 14,6 ms. Regiony w buforze wyrównane do 4 KB (`ALIGN`), żeby
  różne tablice nie dzieliły linii cache ani strony.
- Długie tablice idą **porcjami** (`_layout`): bufor dzielimy na regiony, każda porcja = jedno polecenie.
  MEM_LEN ma 32 bity, ale porcja jest mniejsza niż bufor roboczy.

## 7.5 Podwójny bufor: kopie równolegle z pracą silnika

Silnik i procesor mogą pracować naraz, jeśli nie dotykają tych samych bajtów. Bufor roboczy dzielimy
na **dwie połowy** (`_mem_pipeline`):

```
czas ─────────────────────────────────────────────────────────────►
procesor:  kopia 0 │ start 0 │ kopia 1 (połowa B) │ czekaj 0 │ start 1 │ odbiór 0 │ kopia 2 (połowa A) │ ...
silnik:            │ liczy porcję 0 (połowa A) ─────────────┤ liczy porcję 1 (połowa B) ──────────────┤
```

Kolejność dla porcji j: wypełnij j+1 w drugiej połowie (silnik liczy j) → czekaj na j → start j+1 →
odbierz wynik j (silnik już liczy j+1). Połowa jest nadpisywana dopiero po zakończeniu jej polecenia
i odbiorze wyniku. Tę gwarancję sprawdza test na atrapie, która wykonuje polecenie dopiero przy
czekaniu (`FakeVecMmio(lazy_mem=True)`): każde wcześniejsze nadpisanie połowy daje zły wynik.

Liczba porcji: za mało → mało nakładania; za dużo → stały koszt na porcję (sync, polecenie).
Domyślnie `MEM_CHUNKS = 8` porcji, każda co najmniej `MEM_CHUNK_MIN = 1 MB`. Pomiar (add, 4 M elementów):
pojedynczy bufor 58,5 / 72,1 / 81,3 M/s → podwójny ok. 92 / 94 / 96 M/s (posit32 / 16 / 8).

## 7.6 Pamięć urządzenia: tablice bez kopii

Skoro silnik i tak czyta bufor u-dma-buf, najlepiej trzymać tam **same tablice użytkownika**.
Bufor dzieli się na dwie części:

```
bufor u-dma-buf (64 MB)
├── 0 .. staging_size        bufor roboczy podwójnego bufora (min(połowa, STAGING_MAX = 24 MB))
└── staging_size .. koniec   sterta urządzenia (DeviceHeap, devmem.py), bloki po 4 KB
```

`DeviceHeap` (`devmem.py`):
- **first-fit** po wolnych blokach, rozmiary zaokrąglone w górę do 4 KB (dwie tablice nigdy nie dzielą
  linii cache, więc synchronizacja jednej nie psuje drugiej), łączenie sąsiednich wolnych bloków;
- `empty(count, dtype)` zwraca zwykłą tablicę NumPy (`np.frombuffer` na mapowaniu), a
  `weakref.finalize` zwalnia blok, gdy znika ostatni widok — sterta „sprząta się” sama jak NumPy;
- `offset_of(x)`: jeśli `x` jest ciągłym widokiem w stercie wyrównanym do 16 B, zwraca jego offset
  w buforze — to jest pytanie „czy silnik może to przeczytać w miejscu?”.

Użycie:

```python
with pn.using(hw):
    xd = x.to_device()          # jedna kopia do pamięci urządzenia
    y = xd * xd - xd / 3 + 1    # cztery polecenia, zero kopii: wejścia i wyniki w pamięci urządzenia
    y.on_device                 # True; y.to_float(), np.sum(y) działają normalnie (to widok bufora)
```

`_mem_device`: jeśli choć jedno wejście jest na urządzeniu, całe działanie idzie jednym poleceniem,
wejścia z hosta są kopiowane do tymczasowych bloków, **wynik dostaje nowy blok na stercie**.
Łańcuch działań nie wraca więc na procesor. Synchronizacja dalej jest potrzebna (procesor mógł
zmienić tablicę), ale bez kopii: add posit32 / 16 / 8 → 147 / 297 / 538 M/s.

## 7.7 Ten sam pomysł dla FFT: układ danych rdzenia jako tablica urządzenia

Rdzeń FFT czyta i pisze partie w układzie (B, 2, N): N części rzeczywistych, potem N urojonych.
`PositComplex.to_device()` tworzy jeden bufor (…, 2, N) na stercie, a `.real` / `.imag` są
**widokami z odstępem** (`PositArray._wrap(view=True)`). `PositVecHw.fft_device_offset` rozpoznaje,
że obie części to połówki jednego bufora, i podaje jego adres wprost w MEM_SRC_A; wynik ląduje w nowym
buforze w tym samym układzie. `ifft` liczy rdzeń z bitem `FFT_CTRL.conj` (negacja części urojonej
przy wczytaniu i zapisie), więc też bez kopii. Na płytce: N = 64 × 1024 — z kopiami 275 tys.,
bez kopii 640 tys. transformat / s (NumPy: 366 tys.).

## 7.8 Podsumowanie drogi danych

| wersja | co robi procesor na 4 M elementów add posit32 | M el. / s |
| --- | --- | ---: |
| etap 2–5: AXI DMA, przeplot {b, a} | przeplot 83 ms, kopia wyniku 32 ms | 25 |
| etap 6: master, osobne tablice | dwie kopie 15 ms, kopia wyniku 7 ms | 60 |
| + szybka synchronizacja, podwójny bufor | kopie w cieniu pracy silnika | 92 |
| + 2 tory (L = 2) | jak wyżej, silnik 2 × szybszy | 121 |
| + pamięć urządzenia | tylko synchronizacja (ok. 3 ms) | 147 |

## Do zapamiętania

- Silnik zna tylko adresy fizyczne → bufor fizycznie ciągły z CMA (u-dma-buf), `phys_addr` z sysfs.
- HP0 nie jest spójny z cache: **clean przed**, **invalidate po** (także nad obszarem wyniku przed).
- Narzut wywołania bywa większy niż sama praca: synchronizacja z 2 ms na 57 µs dała więcej niż optymalizacja kopii.
- Podwójny bufor ukrywa kopie za pracą silnika, ale tylko wtedy, gdy porcji jest „w sam raz”.
- Najlepsza kopia to brak kopii: tablice od razu w buforze urządzenia (`to_device`), wyniki też tam.
