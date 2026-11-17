# 10. Wydajność: gdzie idzie czas i czego nas to nauczyło

## 10.1 Jak mierzymy

- **`MEM_CYCLES`** (rejestr): takty samego polecenia w sprzęcie — czysta praca silnika i pamięci, bez procesora.
- **`hw.last_timing`** (Python): czas każdej fazy ostatniego wywołania — `pack` (kopie do bufora),
  `sync_device`, `dma` (czekanie na silnik), `sync_cpu`, `copy_out`, `total`, `engine_cycles`.
- **`time_array`, `time_matmul`, `time_func`, `time_correlate`, `UDmaBuf.time_sync`**: gotowe pomiary.
- Odniesienie: 100 MHz × L elementów / takt — tyle może dać silnik; NumPy float32 na A53 — tyle daje procesor.

Pierwsze pytanie przy każdym pomiarze: **czy silnik pracuje cały czas?** Jeśli `engine_cycles / 100 MHz`
to mała część `total`, wąskim gardłem jest procesor i nie ma sensu przyspieszać sprzętu.

## 10.2 Historia na jednym przykładzie (add, 4 194 304 elementy)

| krok | posit32 | posit16 | posit8 | co się zmieniło | rozdział |
| --- | ---: | ---: | ---: | --- | --- |
| etap 2: AXI DMA, pierwsza wersja | 10,6 | | | rzutowania i pakowanie {b, a} w NumPy, sync całego bufora | 4 |
| etapy 3–5: AXI DMA po poprawkach | 25,4 | 39,3 | 51,4 | bity bez kopii, pakowanie dwiema kopiami z krokiem | 4.5 |
| etap 6: własny master | 59,5 | 72,0 | 80,0 | osobne tablice, bez przeplotu | 5 |
| szybka synchronizacja + podwójny bufor | 92 | 94 | 96 | kopie w cieniu pracy silnika | 7.3, 7.5 |
| tory (×2 / ×4 / ×8) | 121 | 229 | 400 | silnik L razy szybszy | 5.6 |
| pamięć urządzenia | **147** | **297** | **538** | bez kopii | 7.6 |
| NumPy float32 na A53 | ok. 250 | | | | |

M elementów na sekundę. posit8 bez kopii jest ponad 2 razy szybsze niż float32 w NumPy na tym samym procesorze.

## 10.3 Prawo Amdahla w praktyce

W etapie 5 silnik z DMA zajmował 42 ms z 164 ms. Nawet nieskończenie szybki silnik dałby najwyżej
164 / 122 = 1,3 ×. Pięciokrotne przyspieszenie przyszło z **usuwania pracy procesora**:
przeplatanie → kopie → kopie w cieniu → brak kopii. Dopiero gdy procesor przestał dominować, opłaciło się
dodać tory w sprzęcie.

## 10.4 Stały koszt wywołania

Każde polecenie ma narzut niezależny od długości danych:

| składnik | koszt na płytce |
| --- | --- |
| zapis / odczyt rejestru przez HPM0 | rzędu µs |
| para sync_for_device + sync_for_cpu | 57 µs (pierwotnie ok. 2 ms) |
| Python wokół (PositArray, sprawdzenia, widoki) | dziesiątki µs – ok. 1 ms (FFT) |

Konsekwencje, które widzieliśmy:
- podwójny bufor z porcjami 256 KB był *wolniejszy* niż bez niego, dopóki synchronizacja kosztowała 1 ms —
  po zmianie na 57 µs optymalne porcje się zmniejszyły;
- matmul na małych macierzach i FFT dla małych N tracą na stałym koszcie poleceń — stąd **partie**
  (wiele transformat w jednym poleceniu) i pamięć urządzenia;
- FFT N = 64 × 1024: silnik 0,96 ms, kopie 1,7 ms, razem 3,7 ms (275 tys. / s) — bez kopii 1,6 ms (640 tys. / s).

## 10.5 Kiedy ogranicza silnik

Gdy procesor przestaje dominować, liczby zgadzają się z rachunkiem taktów co do procenta:

| operacja | rachunek | zmierzone |
| --- | --- | --- |
| add, 1 tor | 1 takt / element | 42,0 ms na 4 M = 1,00 |
| div posit32, 4 tory | 10 taktów / 4 tory | 2,5 taktu / element |
| matmul P = 4 | 4 MAC / takt | 3,984 MAC / takt |
| FIR 64 odczepy, P = 4 | 4·64 / 67 = 3,82 MAC / takt | 3,82 |
| FFT N = 64, 5.6 | max(64, 93) = 93 takty / transformatę | 94 |

To jest najlepszy test poprawności integracji: jeśli sprzęt nie robi tyle, ile wynika z jego budowy,
coś w drodze danych czeka (backpressure, za małe FIFO, za krótkie serie).

## 10.6 Lekcje

1. **Najpierw zmierz, potem optymalizuj.** Rozbicie na fazy (`last_timing`) wskazało procesor, nie sprzęt.
2. **Dane przez pamięć, nie przez rejestry.** Rejestry są do poleceń (rozdział 3).
3. **Koszt stały bywa większy niż praca.** Synchronizacja 1 ms → 57 µs dała więcej niż niejedna zmiana w sprzęcie.
4. **Najlepsza kopia to brak kopii.** Tablice w buforze urządzenia, wyniki też tam (rozdział 7.6).
5. **Partie** rozkładają narzut polecenia (FFT, matmul po kolumnach).
6. **Miejsce w PL jest skończone.** Każdy blok kosztuje LUT; trzeba wybierać, co trafia do bitstreamu (rozdział 8.2).
7. **Sprawdzaj, co naprawdę działa.** Nieudany build zostawia stary bitstream; testy przechodzą, bo Python
   liczy brakujące rzeczy w programie. `hw.version`, `pn.board.describe()`, `stats()["fallback"]`.
