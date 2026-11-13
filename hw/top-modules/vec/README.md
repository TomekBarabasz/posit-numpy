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

**Bitstream z etapu 2 nie ma quire** — trzeba go zbudować od nowa (te same kroki niżej); `PositVecHw.reduce`
sprawdza `CONFIG.has_quire` i bez niego zgłasza błąd.

## Krok po kroku

1. RTL (z `hw/`):
   ```
   sbt "top-vec/runMain posit.top.vec.VecKv260"          # posit<32,2> -> hw/gen/p32/PositVecAxi.v
   sbt "top-vec/runMain posit.top.vec.VecKv260 16"
   ```
2. Bitstream i aplikacja (z `hw/top-modules/vec/vivado`):
   ```
   vivado -mode batch -nojournal -log build.log -source build.tcl
   ```
   Wynik: `sw/positnp/overlays/posit32_vec/` (`.bit.bin`, `.dtsi`, `shell.json`, `.json`).
3. Na płytce, z `sw/`:
   ```
   scripts/build_udmabuf.sh                    # raz na jądro: moduł u-dma-buf (git, nagłówki jądra)
   scripts/install_overlay.sh posit32_vec      # sprawdza /dev/uio (posit_vec, posit_dma) i /dev/udmabuf-posit
   sudo python3 -m pytest -v -s tests/test_board_vec.py
   ```
   z Pythona:
   ```python
   import numpy as np, positnp as pn
   from positnp.backend_hw import PositVecHw
   hw = PositVecHw.open()
   r = hw.array_op("mul", a_bits, b_bits)                  # surowe bity positów (np. uint32)
   pn.set_backend(hw)                                      # PositArray liczy teraz na płytce
   x = pn.PositArray.from_float(np.linspace(-4, 4, 1000))
   (x * x - x / 3).to_float()
   ```

## build.tcl

| opcja `-tclargs` | domyślnie | |
| --- | --- | --- |
| `-n <bity>` | `32` | 8, 16 albo 32 (`tdata` AXI DMA w pełnych bajtach) |
| `-buf_mb <MB>` | `64` | bufor u-dma-buf w `.dtsi`; połowę z grubsza zajmuje wejście, resztę wynik |
| `-out`, `-deploy`, `-part`, `-jobs`, `-threads`, `-stop ip\|bd` | | jak w `alu` |

AXI DMA (PG021): tryb prosty (bez scatter-gather), adresy 32 b, długość transferu 26 b (do 64 MB − 1),
szyna pamięci 64 b, `tdata` MM2S 2n b, S2MM n b.

## PS, device tree i pamięć podręczna

Jak w etapie 1 (zegar PL0, reset, AFI, węzły UIO), plus:

- **port HP0 FPD** (dane DMA ↔ DDR) na domyślnych 128 b, wpisane też do AFI (`config-afi = <4 0>, <5 0>`);
- **bufor DMA**: węzeł `compatible = "ikwzm,u-dma-buf"`, `device-name = "udmabuf-posit"` →
  `/dev/udmabuf-posit`, adres fizyczny w `/sys/class/u-dma-buf/udmabuf-posit/phys_addr`;
  maska DMA 32 b, więc bufor leży poniżej 4 GB, w zasięgu 32-bitowych adresów DMA;
- **spójność pamięci podręcznej**: HP0 nie jest spójny z cache CPU. `udmabuf.py` mapuje bufor z cache
  i przed każdym transferem robi `sync_for_device` (wejście i wynik), po nim `sync_for_cpu` (wynik).
  Mapowanie bez cache (`O_SYNC`) na arm64 to pamięć Device, w której `memcpy`/numpy padają z SIGBUS.

## Weryfikacja

| poziom | gdzie | |
| --- | --- | --- |
| silnik | `posit-vector/.../PositStreamAluTestplan` | tablice o losowych długościach, `last`, tablica ⊕ skalar, przeplot z rejestrami, przepustowość |
| IP | `posit-kv260/.../PositVecAxiTestplan` | AXI4-Lite + AXI4-Stream jak z AXI DMA, długości 1, 2, 3, 1000, liczniki, miękki reset, nazwy portów |
| quire | `posit-vector/.../PositQuireTestplan` | wszystkie pary posit8 jako wektory dł. 1, losowe posit16/32, znoszenie się, przeniesienia przez cały quire, NaR, kontynuacja, permutacja, 200k × maxpos² — wynik, flagi i surowy quire kontra `QuireRef` |
| quire w IP | `posit-kv260/.../PositVecQuireTestplan` | sum/dot/sumsq przez AXI4-Lite + AXI4-Stream, kontynuacja, NaR, przeplot z tablicami |
| sterownik | `sw/tests/test_vec_fake.py` | `PositVecHw` na atrapach rejestrów, DMA i bufora: porcjowanie, protokół cache, błędy DMA |
| płytka | `sw/tests/test_board_vec.py` | długości 1 … 1 000 001, wszystkie operacje kontra `ref.py`, tablica ⊕ skalar, liczniki, przepustowość |

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
