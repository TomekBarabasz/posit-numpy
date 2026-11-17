# 4. Etapy 2–5: tablice przez AXI DMA i AXI4-Stream

## 4.1 Pomysł

Silnik dostaje dane jako **strumień** elementów (AXI4-Stream), jeden element na takt, a gotowy blok
Xilinxa **AXI DMA** zamienia tablicę w pamięci DDR na strumień i z powrotem. Procesor tylko:
przygotowuje dane w buforze, zleca DMA „przenieś N bajtów spod adresu X” i czeka na koniec.

```
           DDR (bufor u-dma-buf: wejście {b,a} i wynik)
             ▲                         │
             │ HP0 (128 b)             │
   ┌─────────┴─────────────────────────▼────────┐
   │ smartconnect_data                          │
   └───▲─────────────────────────────▲──────────┘
       │ M_AXI_S2MM (64 b)           │ M_AXI_MM2S (64 b)
 ┌─────┴──────────── posit_dma_p<n> (AXI DMA, tryb prosty) ─────────┐
 │  S2MM: strumień → pamięć           MM2S: pamięć → strumień       │
 └─────▲───────────────────────────────────┬────────────────────────┘
       │ S_AXIS_S2MM (n b: wynik)          │ M_AXIS_MM2S (2n b: {b, a})
 ┌─────┴───────────── posit_vec_p<n> (PositVecAxi) ────▼───────────┐
 │  m_axis ◄── PositStreamAlu ◄── s_axis                           │
 │  s_axi (AXI4-Lite): OP, STREAM_CFG, OPERAND_B/C, liczniki, quire│
 └─────────────────────────────────────────────────────────────────┘
   sterowanie: HPM0 → smartconnect_ctl → s_axi silnika i S_AXI_LITE DMA
```

Pliki: `PositVecAxi.scala` (gałąź `withMaster = false`), `sw/positnp/dma.py` (sterownik DMA),
`backend_hw.py` (`PositVecHw._stream`), block design: `build.tcl -io dma`.

## 4.2 Format strumienia

- `s_axis.tdata` = **{b, a}**: 2n bitów, a w młodszej połowie. Jedno słowo strumienia = jedna para argumentów.
- `m_axis.tdata` = wynik, n bitów. `tlast` na ostatnim elemencie.
- Tablica ⊕ skalar: `STREAM_CFG.scalar_b = 1`, b bierze się z rejestru OPERAND_B.
- Operacja całej tablicy: rejestr OP (ten sam kod co w etapie 1, plus sqrt, FMA, AXPY, konwersje).
- Redukcje (etap 4: sum, dot, sumsq): strumień idzie do **quire** zamiast do ALU, bez wyników
  na m_axis. CTRL.quire_clear zeruje, CTRL.quire_round zaokrągla do RESULT (rozdział 6.4).

Dlaczego {b, a} w jednym słowie? Bo AXI DMA w trybie prostym ma **jeden** kanał MM2S —
jeden strumień wejściowy. Dwie tablice trzeba więc w pamięci przepleść: a0 b0 a1 b1 …
To przeplatanie robi procesor i jest najdroższą częścią całej operacji (4.5).

## 4.3 AXI DMA w trybie prostym (PG021)

Sterownik `dma.py` używa trybu „direct register” (bez scatter-gather): jeden transfer na kanał naraz.

| rejestr | MM2S | S2MM | |
| --- | --- | --- | --- |
| DMACR | 0x00 | 0x30 | bit 0 RS (run), bit 2 reset |
| DMASR | 0x04 | 0x34 | halted, idle, błędy, **IOC** (koniec transferu) |
| adres | SA 0x18 | DA 0x48 | adres fizyczny w DDR (32 b) |
| LENGTH | 0x28 | 0x58 | bajty; **zapis długości uruchamia transfer** (max 2²⁶ − 1) |

Kolejność w `_stream` dla jednej porcji (rozdział 7 opisuje bufor i cache):

```
1. pakowanie: bufor[2i] = a[i], bufor[2i+1] = b[i]      (procesor, zapisy z krokiem)
2. sync_for_device(wejście)                           (cache → DDR)
3. S2MM: DA = adres wyniku, LENGTH = n·N/8             (uzbrojenie odbioru PRZED nadawaniem)
4. MM2S: SA = adres wejścia, LENGTH = 2n·N/8           (start nadawania)
5. czekaj na IOC w MM2S i S2MM; sprawdź liczbę odebranych bajtów
6. sync_for_cpu(wynik)                                (unieważnij cache wyniku)
7. kopia wyniku do tablicy NumPy
```

Dlaczego S2MM przed MM2S? Silnik zacznie produkować wyniki, gdy tylko dostanie dane. Gdyby S2MM
nie był gotowy, m_axis by stał (backpressure), a to cofa się do s_axis — nic się nie zgubi,
ale szybciej jest, gdy odbiór czeka od początku.

## 4.4 Szerokości i dlaczego DMA ma 64 b

MM2S czyta 2n bitów na element (64 b dla posit32), więc kanał pamięciowy DMA ustawiliśmy na 64 b
(`c_m_axi_mm2s_data_width 64`), a smartconnect poszerza do 128 b HP0. Silnik bierze 1 element na takt:
przy posit32 to 8 B wejścia i 4 B wyjścia na takt — HP0 (16 B / takt w każdą stronę) ma zapas.

## 4.5 Gdzie szedł czas (pomiar na płytce, add posit32, 4 194 304 elementy)

| faza | czas |
| --- | ---: |
| pakowanie {b, a} (procesor) | 82,9 ms |
| sync przed | 5,5 ms |
| **DMA + silnik** | **42,1 ms** (= 1 element / takt przy 100 MHz) |
| sync po | 1,9 ms |
| kopia wyniku | 31,5 ms |
| razem | 164 ms → 25,4 M elementów / s |

Silnik i DMA zrobiły swoje w dokładnie przewidzianym czasie. **70 % czasu to procesor**:
przeplatanie dwóch tablic w jedną (zapisy co 8 B — A53 robi to ok. 400 MB/s) i kopia wyniku.
Tego nie da się poprawić po stronie PL, dopóki strumień wejściowy jest jeden. Stąd etap 6:
silnik, który sam czyta a i b z dwóch osobnych tablic.

## 4.6 Etapy 3–5 na tej samej architekturze

- **Etap 3**: `PositArray` z protokołami NumPy, `vecops` (posit w NumPy jako wzorzec bit w bit),
  szybsze pakowanie (dwie kopie z krokiem zamiast pętli), pomiar faz (`last_timing`).
- **Etap 4**: quire — dokładne `np.sum`, `np.dot`, `np.mean`, `np.linalg.norm`. Na strumieniu
  idzie tylko MM2S; wynik przez CTRL.quire_round → RESULT, surowy quire w rejestrach QUIRE[k] (0x80…).
- **Etap 5**: sqrt, FMA (a·b + OPERAND_C), AXPY (a·OPERAND_B + b — y = αx + y jednym przejściem),
  konwersje float32 ↔ posit32. CONFIG.opMask mówi, które są zbudowane; brakujące liczy PositSw.

Ta wersja jest w repozytorium oznaczona tagiem i nadal da się ją zbudować (`VecKv260 --dma`,
`build.tcl -io dma`, aplikacje `*_vec`).

## Do zapamiętania

- AXI DMA zamienia pamięć ↔ strumień; silnik widzi tylko AXI4-Stream.
- Jeden kanał MM2S = jeden strumień wejściowy = tablice trzeba przepleść na procesorze.
- Długość w rejestrze LENGTH uruchamia transfer; odbiór (S2MM) uzbraja się przed nadawaniem.
- Pomiar pokazał, że wąskim gardłem nie był sprzęt, tylko praca procesora wokół niego.
