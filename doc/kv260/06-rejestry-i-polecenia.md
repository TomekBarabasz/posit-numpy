# 6. Rejestry i polecenia: jak PS steruje PL

Ten rozdział jest przekrojem przez wszystkie etapy: jak wygląda interfejs sterowania, dlaczego tak,
i jak Python dowiaduje się, z czym rozmawia.

## 6.1 Warstwy połączenia PS → PL

```
Python:  hw.write(regs.MEM_LEN, n)
   │     memoryview(mmap)[0x60 >> 2] = n          (backend_hw._MappedWords)
   ▼
Linux:   strona z mmap(/dev/uioN) = okno fizyczne rejestrów silnika (z device tree)
   ▼
A53:     zapis do adresu 0xA000_xxxx → transakcja AXI na M_AXI_HPM0_FPD
   ▼
PL:      smartconnect_ctl: dekoduje okno 4 KB, zwęża 128 b → 32 b → s_axi silnika
   ▼
silnik:  AxiLite4SlaveFactory: offset 0x60 → rejestr `len` (bus.readAndWrite(len, MEM_LEN))
```

Adresy okien rozdziela Vivado (`assign_bd_address`), build.tcl odczytuje je (`reg_window`)
i wpisuje do `.dtsi` (`reg = <…>`) oraz `.json`. Python nie zna adresów na sztywno — szuka urządzeń
po nazwie (6.6).

## 6.2 Rodzaje rejestrów

| rodzaj | przykład | w Spinalu | zachowanie |
| --- | --- | --- | --- |
| RW parametr | OP, MEM_LEN, MEM_SRC_A, GEMV_K | `bus.readAndWrite(reg, adres)` | zapis ustawia, odczyt zwraca to samo |
| RO stan | STATUS, MEM_STATUS, RESULT, liczniki | `bus.read(sygnał, adres)` | odczyt bieżącej wartości |
| WO impuls | CTRL | `bus.onWrite(CTRL) { start := dane(0) }` | sygnał trwa jeden takt w chwili zapisu |
| RO stała | ID_VERSION, CONFIG, ENGINE_CFG, GEMV_CFG, TABLE_CFG, FFT_INFO | `bus.read(B(stała), adres)` | opis bitstreamu |

**Impulsy w CTRL** są kluczowe dla prostoty: jeden zapis może jednocześnie wyzerować liczniki
i uruchomić polecenie, a nie ma ryzyka, że „zapomniany” bit start uruchomi coś drugi raz.

| bit CTRL | | 
| --- | --- |
| 0 start | pojedyncze działanie z OPERAND_A / B (etap 1) |
| 1 soft_reset | przerywa pracę, opróżnia potok (`ResetArea` w PositVecAxi) |
| 2 clear_counters | CNT_NAR / SAT / INEXACT, CYCLES, CNT_ELEMENTS |
| 3 quire_clear, 4 quire_round | quire: zerowanie, zaokrąglenie do RESULT |
| 5 mem_start, 6 mem_ack | polecenie MEM, potwierdzenie końca (kasuje done i przerwanie) |

## 6.3 Odkrywanie możliwości: CONFIG i rejestry opisu

Jeden kod w Pythonie obsługuje każdy bitstream z historii projektu, bo **pyta**, co ma przed sobą
(`PositVecHw.__init__`):

| rejestr | zawartość | Python |
| --- | --- | --- |
| ID_VERSION 0x00 | 0x5053, wersja (1.0 … 5.6) | `self.version`; nowsze funkcje tylko od właściwej wersji |
| CONFIG 0x04 | n, es; [31:16] **maska operacji** (bit = kod OP); bity 12–15 has_stream / quire / master / gemv | `op_mask`, `array_ops`, `master`, `fma3` |
| ENGINE_CFG 0x5C | k (bity dzielnika na takt), tory div/sqrt/FMA, **L** | `engine_cfg()` |
| QUIRE_CFG 0x54 | szerokość quire, quireFrac | dekodowanie surowego quire |
| GEMV_CFG 0xD0 | maxK, P (kolumny) | `gemv_max_k`, `gemv_cols` |
| TABLE_CFG 0xDC | bity indeksu, odczyty na takt | `has_table()`, `table_rate` |
| FFT_INFO 0xEC | maxS (N ≤ 4^maxS) | `fft_max_s` |

Gdy czegoś nie ma (np. operacji w masce, gemv w posit8, rdzenia FFT), Python liczy to w programie
(`PositSw`) i liczy takie elementy w `stats()["fallback"]`. Dlatego testy na płytce przechodzą
na każdym bitstreamie — i dlatego warto sprawdzać `hw.version` / `pn.board.describe()`, czy działa
to, co myślimy (lekcja z FFT: stary bitstream po nieudanym buildzie dawał poprawne, ale wolne wyniki).

## 6.4 Wzorzec polecenia

Każda operacja ma tę samą strukturę, niezależnie od etapu:

```
1. parametry   (rejestry RW: OP, MEM_*, GEMV_*, FFT_CFG ...)
2. start       (impuls w CTRL)
3. czekanie    (STATUS.done / MEM_STATUS.done, odpytywanie albo przerwanie)
4. błędy       (bity błędów w statusie → wyjątek z opisem)
5. wynik       (RESULT albo dane w pamięci)
```

Zasady, których się trzymamy:
- **Kontrola przed startem w sprzęcie** (zakres, wyrównanie, kształt) — złe polecenie kończy się od razu
  błędem, bez transakcji, zamiast zawiesić silnik albo pisać w złe miejsce.
- **Status nie jest niszczony odczytem**: można go czytać wiele razy; kasuje go dopiero następny start
  albo mem_ack. Quire też nie jest niszczony odczytem (round nie zeruje) — dłuższe tablice niż jeden
  transfer akumulują się dalej.
- **Rejestry trybu muszą wrócić do zera**: GEMV_MODE, FFT_CTRL, TABLE_CTRL zmieniają znaczenie
  następnego polecenia, więc sterownik ustawia je tuż przed poleceniem i zeruje w `finally`.

## 6.5 Kody operacji i tryby — gdy zabrakło miejsca

OP ma 4 bity (kody 0–15), a maska operacji w CONFIG 16 bitów. Kody zapełniały się etapami:

| kod | operacja | etap |
| --- | --- | --- |
| 0–3 | add, sub, mul, div | 1 |
| 4–6 | sum, dot, sumsq (quire) | 4 |
| 7–11 | sqrt, fma, axpy, f2p, p2f | 5 |
| 12 | fma3 | 6 |
| 13–14 | xload, gemv | 7a |
| 15 | table | 7 |

Gdy kodów zabrakło, nowe funkcje dostały **bity trybu** w osobnych rejestrach zamiast nowych kodów:
`TABLE_CTRL.load` (ładowanie tablicy zamiast odczytu), `GEMV_MODE.window` (FIR: wiersze z okna
na sygnale), `FFT_CTRL.fft / twiddle / conj` (polecenie MEM jest partią FFT). Tryb FFT wyłącza
wszystkie inne ścieżki niezależnie od OP.

## 6.6 Wykrywanie silników z Pythona

1. build.tcl nazywa komórki w block designie `posit_vec_p<n>` (i `posit_dma_p<n>`), a overlay daje
   węzłom te same nazwy oraz bufor `udmabuf-posit-p<n>`.
2. `vec_engines()` (`backend_hw.py`) przegląda `/sys/class/uio/*/name` wyrażeniem `posit_vec(_p\d+)?`
   i dla każdego silnika zwraca `VecEngine(name, dma, buf)`.
3. `PositVecHw.open_engine(e)` mapuje rejestry (UIO), opcjonalnie DMA, bufor (u-dma-buf), czyta CONFIG.
4. `pn.board.load("posit32_16_8_mem")` robi xmutil, czeka na urządzenia, otwiera wszystkie silniki
   i rejestruje każdy jako backend swojego formatu: `posit16` liczy silnik p16, `posit32` silnik p32.

## 6.7 Kilka silników naraz

Każdy silnik ma własne okno rejestrów, własny port m_axi (albo DMA) i własny bufor, więc są
**niezależne** — można z nich korzystać jednocześnie (np. z dwóch wątków). Wspólne są tylko port HP0
(smartconnect_data arbitruje) i linia przerwań (xlconcat). Nazwy modułów w Verilogu mają przedrostek
`p<n>_` (`globalPrefix`), żeby silniki różnych formatów nie zderzały się nazwami w jednym projekcie Vivado.

## Do zapamiętania

- Rejestry: RW parametry, RO stan, WO impulsy, RO stałe opisu bitstreamu.
- Wzorzec: parametry → impuls start → czekanie na done → bity błędów → wynik.
- Python odkrywa możliwości z CONFIG i rejestrów opisu; czego nie ma, liczy w programie.
- Kody OP się skończyły — nowe funkcje dostały bity trybu, zerowane po każdym poleceniu.
- Silniki znajdujemy po nazwach węzłów device tree, nie po adresach.
