# 2. Podstawy AXI: Lite, pełny AXI4 i Stream

Cała komunikacja PS ↔ PL w projekcie to trzy odmiany protokołu AMBA AXI. Ten rozdział daje
tyle, ile trzeba, żeby czytać kod `PositVecAxi`, `PositMemMover` i block design.

## 2.1 Uzgadnianie valid / ready — fundament wszystkiego

Każdy kanał AXI to wiązka sygnałów danych plus para:

- **valid** (od nadawcy): „na liniach jest ważna porcja”,
- **ready** (od odbiorcy): „mogę ją przyjąć w tym takcie”.

Porcja przechodzi (**fire**) w takcie, w którym `valid && ready`. Dwie zasady, które łamią się najczęściej:

1. Nadawca, który podniósł `valid`, **nie może go zdjąć ani zmienić danych**, dopóki nie przyjdzie `ready`.
2. Nadawca **nie może czekać na `ready`**, żeby podnieść `valid` (inaczej dwa układy mogą czekać na siebie).

```
takt:      1    2    3    4    5
valid  ___/‾‾‾‾‾‾‾‾‾‾‾‾‾‾\___
data   ===< A >< A >< B >====     A trzymane, bo w takcie 1-2 nie było ready
ready  ________/‾‾‾‾‾‾‾‾‾‾‾‾‾
fire             ^    ^           takt 2: A, takt 3: B
```

W SpinalHDL to `Stream[T]` (`valid`, `ready`, `payload`, `fire`). Model pamięci w symulacji
(`AxiMemModel` w testach) sprawdza zasadę 1 na każdym kanale — przy gemv na posit8 wyłapał nią
„ładunek zmienił się przed ready” (bity `U` z niezainicjowanych rejestrów w symulatorze GHDL, rozdział 5.8).

## 2.2 AXI4-Lite — rejestry

AXI4-Lite to uproszczony AXI do **pojedynczych 32-bitowych odczytów i zapisów**. Pięć kanałów:

```
master (PS, przez smartconnect)                  slave (rejestry silnika)
   AW  adres zapisu      ────────────────────────►
   W   dane zapisu + WSTRB ──────────────────────►
   B   odpowiedź zapisu  ◄────────────────────────  (OKAY / SLVERR)
   AR  adres odczytu     ────────────────────────►
   R   dane odczytu + odpowiedź ◄─────────────────
```

Zapis pod offset 0x10 to: AW(0x10) + W(dane) → B(OKAY). Odczyt: AR(0x14) → R(dane).
Każdy dostęp to osobna transakcja z pełnym obiegiem przez interconnect — **kosztuje
setki nanosekund do pojedynczych mikrosekund**, niezależnie od tego, jak szybki jest silnik.
Dlatego rejestry służą do sterowania, a nie do przenoszenia danych (rozdział 3.5).

W projekcie: `AxiLite4SlaveFactory(io.s_axi)` w SpinalHDL generuje dekoder adresów;
`bus.readAndWrite(reg, offset)`, `bus.read(sig, offset)`, `bus.onWrite(offset) { … }` opisują mapę
(rozdział 6). Szyna ma 12 bitów adresu — 4 KB okna na silnik (najmniejszy segment w Vivado).

Ważny szczegół z `backend_hw.py` (`_MappedWords`): dostęp z Pythona idzie przez
`memoryview(...).cast("I")` — jedno wyrównane 32-bitowe słowo na rejestr. Kopiowanie bajt po bajcie
mogłoby się rozbić na cztery 8-bitowe zapisy, a nasz slave nie patrzy na WSTRB.

## 2.3 AXI4 (pełny) — pamięć, serie

Pełny AXI4 ma te same pięć kanałów, ale dodaje to, czego potrzeba do szybkiego przesyłu:

| pole | znaczenie |
| --- | --- |
| **AxLEN** | długość serii: 1–256 słów (u nas do 16) |
| **AxSIZE** | bajty na słowo (u nas 16 B = 128 b) |
| **AxBURST** | INCR: kolejne słowa pod kolejne adresy |
| **AxID** | numer strumienia transakcji; odpowiedzi o różnych ID mogą przychodzić w dowolnym przeplocie |
| **WLAST / RLAST** | ostatnie słowo serii |
| **WSTRB** | które bajty słowa zapisu są ważne (maskowanie niepełnego ostatniego słowa) |
| **AxCACHE, AxPROT** | atrybuty pamięci (u nas 0011: bufferable, modifiable) |

Jedna transakcja AR z AxLEN = 15 przynosi 16 słów po 16 B = 256 B danych na **jeden** adres.
To jest sedno wydajności: narzut adresu rozkłada się na wiele słów, a kanał R może dawać
jedno słowo na takt.

**Reguła 4 KB**: seria nie może przekroczyć granicy 4 KB (strony). `PositMemMover.burstBeats`
liczy, ile słów zostało do granicy, i skraca serię — test `mem_elementwise` celowo kładzie
bufory tuż przed granicą 4 KB.

**ID i przeplot**: master czytający kilka tablic naraz (a, b, c) daje każdej inny ID.
Interconnect może zwracać słowa różnych ID przemieszane. Odbiorca musi więc mieć dla każdego
ID własne miejsce — w PositMemMover osobne FIFO na strumień (rozdział 5.3).

## 2.4 AXI4-Stream — strumień bez adresów

AXI4-Stream to jeden kanał: `tdata`, `tvalid`, `tready`, opcjonalnie `tlast` (koniec pakietu).
Nie ma adresów — dane po prostu płyną z jednego bloku do drugiego. W etapach 2–5 AXI DMA zamieniało
tablicę w pamięci na strumień (MM2S: memory-map → stream) i z powrotem (S2MM), a silnik widział
tylko strumienie (rozdział 4).

## 2.5 Smartconnect — kto łączy porty

W block designie (`build.tcl`) są dwa bloki **smartconnect** (interconnect Xilinxa):

```
                 smartconnect_ctl                         smartconnect_data
HPM0 (128 b) ──► 1 wejście → po 1 wyjściu na silnik      wyjścia m_axi silników → 1 wyjście ──► HP0
                 (zwęża 128 b → AXI4-Lite 32 b,            (arbitraż między silnikami)
                  dekoduje adresy okien 4 KB)
```

Smartconnect robi konwersję szerokości i protokołu, arbitraż i dekodowanie adresów. Dzięki temu
silnik ma prosty port AXI4-Lite, a procesor widzi każdy silnik pod własnym adresem.

## Do zapamiętania

- Porcja przechodzi przy `valid && ready`; nadawca trzyma dane do `ready`.
- AXI4-Lite = rejestry, jedna liczba na transakcję, drogo na dostęp → tylko sterowanie.
- AXI4 = serie (do 256 słów, u nas 16 × 16 B), ID, WSTRB, granica 4 KB → dane.
- AXI4-Stream = strumień bez adresów; tak silnik dostawał dane od AXI DMA.
- Smartconnect zwęża, dekoduje adresy i arbitruje — dlatego w block designie jest ich dwa.
