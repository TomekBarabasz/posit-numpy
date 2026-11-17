# Integracja z KV260 — jak to działa

Ten katalog opisuje, jak biblioteka posit-numpy rozmawia z logiką programowalną (PL) na płytce Kria KV260:
od pojedynczego rejestru zapisanego z Pythona, przez DMA, po własny master AXI4 silnika, bufory bez kopii
i polecenia złożone (matmul, FIR, FFT). Dokumenty są pisane do nauki: każdy zaczyna od pojęć, potem pokazuje,
jak zrobiliśmy to w projekcie (z odwołaniami do plików), a kończy listą rzeczy do zapamiętania.

## Kolejność czytania

| # | dokument | o czym |
| --- | --- | --- |
| 1 | [01-platforma.md](01-platforma.md) | KV260 i Zynq UltraScale+: PS, PL, porty AXI między nimi, Linux, overlaye |
| 2 | [02-axi-podstawy.md](02-axi-podstawy.md) | protokoły AXI4-Lite, AXI4 i AXI4-Stream: kanały, uzgadnianie valid / ready, serie |
| 3 | [03-etap1-axi-lite.md](03-etap1-axi-lite.md) | pierwsza architektura: ALU za rejestrami AXI4-Lite, jedno działanie na raz |
| 4 | [04-etap2-dma-strumien.md](04-etap2-dma-strumien.md) | tablice przez AXI DMA i AXI4-Stream (etapy 2–5) |
| 5 | [05-etap6-wlasny-master.md](05-etap6-wlasny-master.md) | architektura po etapie 6: silnik z własnym masterem AXI4 (PositMemMover) |
| 6 | [06-rejestry-i-polecenia.md](06-rejestry-i-polecenia.md) | jak PS wydaje polecenia PL: mapa rejestrów, impulsy, status, przerwania, wykrywanie silników |
| 7 | [07-pamiec-i-bufory.md](07-pamiec-i-bufory.md) | adresy fizyczne, CMA, u-dma-buf, cache, podwójny bufor, pamięć urządzenia bez kopii |
| 8 | [08-od-rtl-do-plytki.md](08-od-rtl-do-plytki.md) | budowa: SpinalHDL → Vivado → bitstream + device tree → xmutil → Python |
| 9 | [09-polecenia-zlozone.md](09-polecenia-zlozone.md) | jak matmul, FIR, tablice funkcji i FFT mieszczą się w tym samym interfejsie poleceń |
| 10 | [10-wydajnosc.md](10-wydajnosc.md) | gdzie idzie czas: historia pomiarów i lekcje z każdego usprawnienia |

Dokumenty 1–2 to tło (warto przeczytać nawet znając FPGA — pojęcia z nich wracają wszędzie).
3–5 to trzy kolejne architektury, w kolejności, w jakiej powstawały. 6–7 to przekroje przez wszystkie
etapy: rejestry i pamięć. 8 to praktyka (jak to zbudować i uruchomić), 9–10 to rozszerzenia i liczby.

## Mapa projektu (pliki, do których odwołują się dokumenty)

```
hw/posit-kv260/hw/spinal/main/
  PositAluAxiLite.scala     etap 1: ALU + rejestry AXI4-Lite (PositAluRegs)
  PositVecAxi.scala         etapy 2-7: silnik na tablicach, rejestry (PositVecRegs), strumień albo master
  PositMemMover.scala       etap 6: własny master AXI4 (odczyt a, b, c i zapis wyniku)
  PositFftCore.scala        etap 7: rdzeń FFT radix-4
hw/top-modules/vec/
  hw/spinal/main/VecKv260.scala   generator RTL silników (opcje: L, gemv, tablice, FFT)
  vivado/build.tcl                block design, bitstream, pliki dla xmutil
  vivado/overlay_files.tcl        generator .dtsi (device tree overlay) i .json
hw/top-modules/common/vivado/kv260_fan.tcl   wentylator (PWM z PS przez PL)
sw/positnp/
  regs.py          mapa rejestrów (ta sama co w Scali; zgodność sprawdza test)
  backend_hw.py    PositAluHw (etap 1), PositVecHw (etapy 2-7): UIO, polecenia, bufory
  dma.py           sterownik AXI DMA (etapy 2-5)
  udmabuf.py       ciągły bufor z modułu u-dma-buf, synchronizacja cache
  devmem.py        sterta w buforze: tablice w pamięci urządzenia (bez kopii)
  board.py         pn.board: xmutil, ładowanie aplikacji, wykrywanie silników
  fakes.py         atrapy rejestrów i pamięci do testów bez płytki
sw/scripts/
  install_overlay.sh, build_udmabuf.sh, diag_gemv.py
```

## Słowniczek

| pojęcie | znaczenie |
| --- | --- |
| **PS** (Processing System) | twarda część układu: 4 rdzenie ARM Cortex-A53, kontroler DDR, interconnect, peryferia |
| **PL** (Programmable Logic) | FPGA w tym samym układzie; tu działają nasze silniki |
| **AXI** | rodzina protokołów magistral ARM AMBA: AXI4 (pamięć, serie), AXI4-Lite (rejestry), AXI4-Stream (strumień) |
| **master / slave** | kto zaczyna transakcję (master) i kto odpowiada (slave) |
| **HPM0 FPD** | port PS → PL (PS jest masterem): tędy procesor pisze i czyta rejestry silników |
| **HP0 FPD** | port PL → PS (PL jest masterem): tędy DMA albo silnik czyta i pisze DDR |
| **MMIO** | rejestry urządzenia widoczne jako adresy pamięci; zapis = transakcja na magistrali |
| **UIO** | sterownik Linuksa „user space I/O”: daje `/dev/uioN`, przez który proces mapuje rejestry |
| **device tree overlay** | opis sprzętu doładowywany razem z bitstreamem: adresy rejestrów, przerwania, bufory |
| **xmutil / dfx-mgr** | narzędzie Kria do ładowania „aplikacji PL” (bitstream + overlay) |
| **u-dma-buf** | moduł jądra dający fizycznie ciągły bufor w pamięci (z CMA) do DMA |
| **cache coherency** | spójność pamięci podręcznej procesora z tym, co widzi DMA; na HP0 jej nie ma — trzeba synchronizować |
| **seria (burst)** | jedna transakcja AXI4 przenosząca wiele słów pod kolejne adresy |
| **tor (lane)** | kopia jednostki obliczeniowej; L torów = L elementów na takt |
