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
