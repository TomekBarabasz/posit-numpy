# 5. Etap 6: silnik z własnym masterem AXI4

## 5.1 Pomysł

Zamiast AXI DMA, które umie tylko „jeden strumień z jednego miejsca”, silnik dostaje **własny master
AXI4** (`PositMemMover`). Procesor podaje w rejestrach **adresy tablic** i długość, a silnik sam:
czyta a, b (i c) z trzech osobnych tablic w DDR, liczy i zapisuje wynik pod adres docelowy.
Koniec sygnalizuje bitem w MEM_STATUS i przerwaniem.

```
                         DDR
   tablica a   tablica b   tablica c   wynik
       ▲           ▲           ▲          │            (osobne, ciągłe tablice, bez przeplatania)
       └───────────┴─────┬─────┴──────────┘
                         │ HP0 128 b (serie INCR do 16 słów)
              smartconnect_data (arbitraż między silnikami)
                         │ m_axi (AXI4, 128 b, 2 bity ID)
 ┌───────────────────────┴──────── posit_vec_p<n> (PositVecAxi, withMaster) ─────────────┐
 │  PositMemMover                                                                          │
 │    3 czytniki (ID 0, 1, 2): AR ─► R ─► FIFO 64 słów ─► grupy L elementów ─► src(0..2)  │
 │    pisarz: res ─► słowa 128 b (WSTRB w ostatnim) ─► AW / W / B                          │
 │         │ src(a,b,c)                                   ▲ res                            │
 │         ▼                                              │                                │
 │   L kopii PositStreamAlu (tory) ──── albo ────► quire / gemv / tablica / FFT (rozdz. 9)  │
 │  s_axi: MEM_LEN, MEM_SRC_A/B/C, MEM_DST, MEM_LO/HI, MEM_CFG, CTRL.mem_start, MEM_STATUS │
 │  irq ──────────────────────────────────────────────────────────► pl_ps_irq0 (GIC SPI 89+k)│
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

Pliki: `PositMemMover.scala`, `PositVecAxi.scala` (obszar `mem`), `backend_hw.py` (`_mem_start`,
`_mem_wait`, `_mem_pipeline`, `_mem_device`), block design `build.tcl -io master` (domyślne).

## 5.2 Polecenie MEM — rejestry

| offset | rejestr | znaczenie |
| --- | --- | --- |
| 0x60 | MEM_LEN | liczba elementów |
| 0x64 / 0x68 / 0x6C | MEM_SRC_A / B / C | adresy fizyczne tablic (32 b, wyrównane do 16 B) |
| 0x70 | MEM_DST | adres wyniku (może być = SRC_A: działanie w miejscu) |
| 0x74 / 0x78 | MEM_LO / MEM_HI | dozwolony zakres adresów [LO, HI) — po resecie pusty |
| 0x7C | MEM_CFG | bit 0 read_b, 1 read_c, 2 write, 3 irq_en |
| 0xC0 | MEM_STATUS | bit 0 busy, 1 done, 2 err_range, 3 err_align, 4 err_resp, 5 err_shape |
| 0xC4 | MEM_CYCLES | takty ostatniego polecenia (pomiar wydajności sprzętu bez procesora) |
| 0x08 | CTRL | bit 5 mem_start, bit 6 mem_ack (kasuje done i przerwanie) |

Sekwencja (`_mem_start` + `_mem_wait`):

```python
write(OP, kod)                      # co liczyć (add, fma3, ...)
write(MEM_LEN, n)
write(MEM_SRC_A, phys + off_a); write(MEM_SRC_B, phys + off_b)
write(MEM_DST, phys + off_y)
write(MEM_CFG, read_b | write)
write(CTRL, 1 << CTRL_MEM_START)    # impuls: silnik przejmuje polecenie
while not read(MEM_STATUS) & DONE:  # odpytywanie (albo przerwanie, gdy irq_en)
    ...
errors = MEM_STATUS bity 2..5        # wyjątek z opisem
```

Kontrole przed startem, w sprzęcie (zanim pójdzie jakakolwiek transakcja):
- **zakres**: każdy używany adres i jego koniec (adres + słowa·16) w [MEM_LO, MEM_HI), bez przekroczenia 2³²;
- **wyrównanie** do słowa danych (16 B);
- **kształt** (od etapu 7: gemv, FFT): niepasujące długości.

Błąd → MEM_STATUS.done + bit błędu, **zero transakcji AXI**. To ważne: silnik z własnym masterem
może pisać w dowolne miejsce DDR — także w pamięć jądra. MEM_LO / MEM_HI ogranicza go do bufora,
który `PositVecHw.__init__` wpisuje przy otwarciu (`MEM_LO = phys`, `MEM_HI = phys + size`).

## 5.3 Czytniki: serie, ID i kredyty

Każdy z trzech strumieni (a, b, c) ma czytnik z własnym ID AXI i własnym FIFO na 64 słowa po 128 b.

**Wydawanie serii (AR).** Jeden rejestr AR obsługiwany karuzelą po czytnikach (`rr`). Czytnik może
wysłać serię tylko wtedy, gdy ma **kredyt**: miejsce w FIFO na wszystkie słowa, które zamówił,
a które jeszcze nie przyszły:

```
room = 64 − zajęte_w_FIFO − w_drodze      # w_drodze: zamówione na AR, jeszcze nie odebrane z R
canIssue = aktywny && zostało_słów > 0 && room ≥ długość_serii
```

Dzięki temu **kanał R nigdy nie musi czekać** (`io.axi.r.ready := True`): każde słowo, które przyjdzie,
ma zarezerwowane miejsce. To usuwa klasyczne zakleszczenie — gdyby słowa b przyszły, a FIFO b było
pełne, R by stanął, a słowa a stojące za nimi nigdy by nie dotarły, choć silnik na nie czeka.

**Długość serii** (`burstBeats`): min(16 słów, ile zostało, ile do granicy 4 KB).

**Rozdzielanie R po ID**: słowo z RID = i trafia do FIFO czytnika i. Przeplot ID nie ma znaczenia.

**Ze słów na elementy**: słowo 128 b zawiera 4 / 8 / 16 elementów posit32 / 16 / 8; czytnik wydaje
je od najmłodszych bitów, w grupach po L (tory). Ostatnia grupa może być niepełna (len mod L).

## 5.4 Pisarz: grupy → słowa → serie

- Wyniki przychodzą grupami po L elementów; pisarz składa je w słowo 128 b (`acc`), licząc też
  **WSTRB**: w ostatnim, niepełnym słowie ważne są tylko bajty prawdziwych elementów. Bajty za
  końcem tablicy wyniku nie zostają nadpisane (testy kładą tam wartownika 0x5a5a5a5a i sprawdzają).
- **AW wydaje z wyprzedzeniem**: serie są zamawiane od razu po starcie polecenia (adres i długość są
  znane z góry), długości trafiają do kolejki `lenQ`, z której kanał W bierze WLAST.
- **B**: licznik serii bez odpowiedzi; polecenie kończy się dopiero, gdy wszystkie B wróciły —
  dopiero wtedy wynik jest na pewno w pamięci.

## 5.5 Koniec polecenia i przerwanie

`mover.io.done` = wszystkie czytniki skończyły i nie mają nic w drodze, pisarz wysłał i potwierdził
wszystko. Wtedy MEM_STATUS.done = 1, a jeśli MEM_CFG.irq_en — linia `irq` idzie w górę
(poziomowo), do `pl_ps_irq0` (przy kilku silnikach przez `xlconcat`: silnik k = GIC SPI 89 + k).
CTRL.mem_ack kasuje done i przerwanie. Device tree ma właściwość `interrupts = <0 89+k 4>`,
więc UIO może je oddawać do przestrzeni użytkownika; sterownik w Pythonie na razie odpytuje MEM_STATUS
(przy krótkich poleceniach odpytywanie jest szybsze niż obsługa przerwania).

Odpowiedź inna niż OKAY na R lub B → `err_resp`. Polecenie dalej się kończy (dane lecą do końca),
żeby nic nie zostało w połowie; wynik trzeba odrzucić.

## 5.6 Tory: L elementów na takt

Port HP0 przenosi 16 B na takt. Silnik o jednym torze bierze 1 element na takt — przy posit32 to
8 B argumentów i 4 B wyniku, czyli wykorzystuje tylko część portu. Rozwiązanie: **L kopii
`PositStreamAlu`**, a czytniki i pisarz pracują grupami po L elementów (L·n bitów na takt).

```
src(a) grupa L ──► kolejka toru 0 ──► PositStreamAlu 0 ──┐
                └► kolejka toru 1 ──► PositStreamAlu 1 ──┼──► StreamJoin → grupa wyników → pisarz
                   ...                                   │
                └► kolejka toru L-1 ► PositStreamAlu L-1 ┘
```

- Grupa idzie do torów dopiero, gdy wszystkie mają miejsce w kolejkach; wyniki łączy StreamJoin.
  Każdy tor zachowuje kolejność, więc grupy wychodzą w kolejności.
- Sensowne L: działanie dwuargumentowe potrzebuje 2·L·n/8 B na takt ≤ 16 B → posit32 ×2, posit16 ×4,
  posit8 ×8 (`VecKv260 32:2:4:4:2 16:2:4:2:4 8:2:4:1:8`, ENGINE_CFG[23:16] = L).
- Redukcje (quire) nadal po 1 elemencie na takt: grupa jest rozkładana na elementy (`qIdx`).

Wynik na płytce, add: posit32 ×2 121 M/s, posit16 ×4 229 M/s, posit8 ×8 400 M/s (z kopiami);
bez kopii 147 / 297 / 538 M/s (rozdział 7).

## 5.7 Co zyskaliśmy względem AXI DMA

| | AXI DMA (etap 5) | własny master (etap 6) |
| --- | --- | --- |
| wejścia | jedna przeplatana tablica {b, a} | do 3 osobnych tablic |
| przygotowanie na procesorze | przeplatanie (82,9 ms na 4 M elementów) | zwykłe kopie (14,6 ms) albo nic (pamięć urządzenia) |
| FMA na trzech tablicach | nie (w programie) | tak (FMA3) |
| polecenie | 2 × 4 rejestry DMA + rejestry silnika | rejestry silnika |
| add posit32 (4 M elem.) | 25,4 M/s | 59,5 → 92 (podwójny bufor) → 121 (L = 2) → 147 (bez kopii) M/s |
| logika | AXI DMA (kilka tys. LUT) | mover (+1 850 LUT względem wersji strumieniowej) |

## 5.8 Jak to przetestowaliśmy bez płytki

`PositVecMemTestplan` z modelem pamięci `AxiMemModel`: losowe ready i przerwy na każdym kanale,
opóźnienia serii, przeplot ID, sprawdzanie protokołu (valid trzymane do ready, INCR, wyrównanie,
≤ 16 słów, brak przekroczenia 4 KB, WLAST, zakres). Każda operacja bit w bit z `PositRef`,
bufory przecinające 4 KB, działanie w miejscu, odrzucenia, SLVERR, przerwanie. Dwa symulatory:
Verilator (szybki) i GHDL (wyłapuje niezainicjowane `U` — tak znaleźliśmy brakujące `init` w
rejestrach pakowania wyników gemv, gdzie nieokreślone bajty szły na W pod maską WSTRB).

## Do zapamiętania

- Rejestry opisują polecenie (adresy, długość, co czytać i pisać); dane płyną seriami po HP0.
- Kredyty (miejsce w FIFO na wszystko zamówione) gwarantują, że R nie stoi → brak zakleszczeń przy przeplocie ID.
- MEM_LO / MEM_HI to bezpiecznik: master w PL mógłby pisać po pamięci jądra.
- WSTRB w ostatnim słowie: wynik nie nadpisuje bajtów za tablicą.
- Tory: L elementów na takt dopasowuje silnik do szerokości portu 128 b.
