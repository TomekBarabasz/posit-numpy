# 1. Platforma: KV260 i Zynq UltraScale+

## 1.1 Co jest na płytce

Kria KV260 to płytka z modułem K26, a w nim układ **Zynq UltraScale+ MPSoC** (xck26-sfvc784-2LV-c).
Jeden kawałek krzemu zawiera dwa światy:

```
┌───────────────────────────── układ XCK26 ─────────────────────────────┐
│  PS (Processing System) — twardy, zawsze ten sam                      │
│    4 × ARM Cortex-A53 (1,3 GHz), L1/L2 cache                          │
│    kontroler DDR4 (4 GB na module)  ◄── pamięć wspólna dla PS i PL    │
│    interconnect (CCI, przełączniki FPD/LPD), GIC (przerwania)         │
│    peryferia: USB, Ethernet, UART, timery TTC, ...                    │
│        │ porty PS → PL (PS jest masterem)      ▲ porty PL → PS        │
│        │ M_AXI_HPM0_FPD, M_AXI_HPM1_FPD, ...   │ S_AXI_HP0..3_FPD,    │
│        ▼                                       │ S_AXI_HPC0/1, ACP    │
├────────────────────────────────────────────────────────────────────────┤
│  PL (Programmable Logic) — FPGA, zawartość z bitstreamu                │
│    ok. 117 tys. LUT, 234 tys. FF, 1248 DSP, 144 BRAM36, 64 URAM        │
│    zegary pl_clk0..3 z PS (u nas pl_clk0 = 100 MHz), reset pl_resetn0 │
└────────────────────────────────────────────────────────────────────────┘
```

Najważniejsza rzecz do zrozumienia: **PS i PL współdzielą tę samą pamięć DDR** i rozmawiają przez
porty AXI. Nie ma tu „karty PCIe” ani osobnej pamięci akceleratora. Akcelerator w PL może czytać
i pisać tę samą pamięć, w której leżą tablice NumPy procesu Pythona — trzeba tylko znać jej adres
**fizyczny** i pamiętać o pamięci podręcznej procesora (rozdział 7).

## 1.2 Porty między PS i PL, których używamy

| port | kierunek | kto jest masterem | do czego u nas |
| --- | --- | --- | --- |
| **M_AXI_HPM0_FPD** | PS → PL | procesor | rejestry silników (AXI4-Lite), rejestry AXI DMA |
| **S_AXI_HP0_FPD** | PL → PS | PL (DMA albo silnik) | dane: odczyt argumentów z DDR, zapis wyników |
| **pl_ps_irq0** | PL → PS | — | przerwanie „polecenie skończone” (etap 6) |
| **pl_clk0, pl_resetn0** | PS → PL | — | zegar 100 MHz i reset całej logiki |
| **EMIO TTC0** | PS → PL → pin | — | PWM wentylatora (rozdział 8.6) |

- **HPM** = High Performance Master (z punktu widzenia PS). Zapis procesora pod adres z okna HPM0
  (u nas 0xA000_0000 …) zamienia się w transakcję AXI, która trafia do PL.
- **HP** = High Performance (slave po stronie PS). PL wysyła na niego transakcje do DDR.
  Porty HP **nie są spójne z cache procesora** (w przeciwieństwie do HPC i ACP). To świadomy wybór:
  są najszybsze i najprostsze, a spójność zapewniamy programowo (rozdział 7.4).
- FPD = Full Power Domain (część PS z A53 i DDR), w odróżnieniu od LPD (Low Power Domain).

Szerokość obu portów: **128 bitów**. To ważne, bo wyznacza, ile danych płynie na takt:
128 b = 16 B = 4 elementy posit32 = 8 posit16 = 16 posit8 (rozdział 5.6 — tory).

## 1.3 Zegar

Wszystko w PL (silniki, smartconnecty, AXI DMA) chodzi na jednym zegarze **pl_clk0 = 100 MHz**.
Przy 100 MHz jeden element na takt to 100 mln elementów na sekundę — to jest górna granica
„silnik o jednym torze”, do której porównujemy wszystkie pomiary.

Ciekawostka z praktyki (`hw/top-modules/alu/README.md`): zegar PL ustawia sterownik Linuksa
z device tree, dzieląc PLL 1500 MHz. Wpisanie „faktycznej” częstotliwości z Vivado (99 999 001 Hz)
dawało dzielnik 16 → 93,75 MHz; wpisanie 100 000 000 daje dzielnik 15 → 100 MHz.
Sprawdzenie na płytce: `sudo cat /sys/kernel/debug/clk/pl0_ref/clk_rate`.

## 1.4 Oprogramowanie: Ubuntu, xmutil, bez PYNQ

Na płytce działa **Ubuntu 24.04 dla Kria** (jądro `linux-xilinx`). Nie używamy PYNQ — wszystko,
co PYNQ robi „magicznie”, robimy jawnie i świadomie:

| zadanie | PYNQ | u nas |
| --- | --- | --- |
| wgranie bitstreamu | `Overlay("x.bit")` | `xmutil loadapp <nazwa>` (dfx-mgr) — `pn.board.load` |
| opis sprzętu dla jądra | `.hwh` czytany przez PYNQ | **device tree overlay** `.dtbo` generowany w build.tcl |
| dostęp do rejestrów | `MMIO(adres)` przez /dev/mem | **UIO**: `/dev/uioN` z węzła `generic-uio` |
| bufor DMA | `allocate()` (xlnk / CMA) | moduł **u-dma-buf**: `/dev/udmabuf-posit-p<n>` |
| przerwania | asyncio + UIO | (na razie odpytywanie MEM_STATUS; przerwanie jest w sprzęcie i dtsi) |

Dlaczego tak? Mniej warstw, pełna kontrola nad tym, co się dzieje, i działa na zwykłym Ubuntu
bez specjalnego obrazu. Cena: trzeba samemu wygenerować device tree (rozdział 8.3).

## 1.5 Aplikacje PL, xmutil i „flat shell”

Kria ładuje logikę PL jako **aplikację**: katalog w `/lib/firmware/xilinx/<nazwa>/` z plikami

```
<nazwa>.bit.bin   bitstream w formacie dla menedżera FPGA (bootgen -process_bitstream bin)
<nazwa>.dtbo      skompilowany device tree overlay
shell.json        {"shell_type": "XRT_FLAT", "num_slots": "1"}  — jedna aplikacja na całą PL
<nazwa>.json      (nasz dodatek) lista silników: nazwy UIO, bufory, adresy, zasoby z syntezy
```

`xmutil loadapp <nazwa>` (pod spodem demon **dfx-mgr**) wgrywa bitstream przez fpga_manager,
nakłada overlay na drzewo urządzeń, a jądro tworzy z niego urządzenia: `/dev/uioN` dla rejestrów,
`/dev/udmabuf-…` dla buforów. `xmutil unloadapp` cofa to wszystko.

„Flat shell” oznacza: **jedna aplikacja naraz** zajmuje całą PL (bez częściowej rekonfiguracji).
Dlatego kilka formatów posit naraz robimy jednym bitstreamem z kilkoma silnikami (rozdział 8.2),
a nie kilkoma aplikacjami.

## 1.6 Zasoby PL — dlaczego to ma znaczenie

xck26 ma ok. 117 tys. LUT. Przykłady z projektu (synteza OOC, `hw/top-modules/ooc/README.md`):

| blok | LUT |
| --- | ---: |
| silnik posit32 z masterem, 1 tor | 8 405 |
| to samo, 2 tory | 11 842 |
| + gemv na 4 kolumny (8 quire) | 35 119 |
| posit16 ×4 z tablicą funkcji | 9 331 |
| rdzeń FFT posit32 | ok. 27–30 tys. |

Wszystkiego naraz się nie da: trzy silniki z gemv ×4, tablicami i rdzeniem FFT nie zmieściły się
w placerze (10 925 z 9 894 dostępnych CLB). Stąd opcje generatora (`--gemv-cols`, `--fft`) — rozdział 8.2.

## Do zapamiętania

- PS i PL to jeden układ i **jedna pamięć DDR**; akcelerator czyta i pisze te same strony pamięci co Linux.
- Rejestry: procesor → PL przez **HPM0**. Dane: PL → DDR przez **HP0** (128 b, bez spójności z cache).
- Jeden zegar 100 MHz: 1 element / takt = 100 M elementów / s.
- Bez PYNQ: xmutil + device tree overlay + UIO + u-dma-buf — każdy z tych elementów omawiamy osobno.

## Sprawdź sam (na płytce)

```bash
sudo xmutil listapps                         # zainstalowane aplikacje, aktywna ma slot >= 0
ls /sys/class/uio/; cat /sys/class/uio/uio*/name    # urządzenia UIO i ich nazwy z device tree
cat /proc/iomem | grep -i a00                # okno HPM0 z rejestrami silników
grep -i cma /proc/meminfo                    # pamięć CMA, z której u-dma-buf bierze bufory
```
