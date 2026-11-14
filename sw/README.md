# sw: positnp (Python)

```python
import numpy as np, positnp as pn
x = pn.array(np.linspace(-2, 2, 5))          # posit<32,2>; pn.array(x, pn.P16) dla posit<16,2>
y = x * x - x / 3 + 1                         # każdy operator = jedno array_op na backendzie
np.sort(y), np.max(y), np.argmax(y), -y, abs(y), y < 0, y[1:3]
y.to_float()                                  # float64, NaR -> nan
with pn.using(PositVecHw.open(32)):           # KV260, silnik posit32 (domyślnie PositSw na vecops)
    z = x * x
pn.stats()                                    # NaR / nasycenia / niedokładne / elementy
np.sum(x), np.dot(x, y), np.mean(x), np.linalg.norm(x)   # quire: dokładnie, jedno zaokrąglenie
pn.quire(x, y)                                # dokładna wartość quire jako Fraction
np.sqrt(x), pn.fma(x, y, 1.0), pn.axpy(0.5, x, y)   # etap 5: jedno zaokrąglenie
pn.array(np.float32(...)), x.to_float32()     # float32 <-> posit32 (F2P / P2F w PL na KV260)
```

## Płytka z Pythona: `pn.board`

```python
import positnp as pn
pn.board.apps()                         # zainstalowane aplikacje (/lib/firmware/xilinx), z listą silników z .json
pn.board.active()                       # wgrana aplikacja (xmutil listapps) albo None
hw = pn.board.load("posit32_16_8_mem")  # unloadapp + loadapp, czeka na /dev/uio*, otwiera silniki: {32: .., 16: .., 8: ..}
print(pn.board.describe())              # silniki: operacje, quire, tory div/sqrt/FMA, k, bufor, wersja
x16 = pn.array(data, pn.P16)            # każdy format liczy swój silnik, formaty bez silnika: PositSw
pn.board.engine(32).time_array("div")   # konkretny silnik
pn.board.open()                         # podłączenie do już wgranej aplikacji (bez przeładowania)
pn.board.unload()                       # zamyka silniki (backendy wracają do PositSw), xmutil unloadapp
```

- Silniki z etapu 6 (`*_mem`, własny master AXI4) czytają i zapisują bufor sami: bez AXI DMA i bez pakowania
  `{b, a}`; `pn.fma(x, y, z)` na trzech tablicach liczy sprzęt (FMA3). Starsze aplikacje (`*_vec`, AXI DMA) działają dalej.
- Jedna aplikacja naraz (płaski shell KV260); kilka formatów naraz = jeden bitstream z kilkoma silnikami
  (`build.tcl -engines "32 16 8"`). `load` tej samej, już wgranej aplikacji tylko otwiera silniki (`force=True` przeładowuje).
- Backendy: `pn.set_backend(hw)` ustawia backend formatu `hw.config` (inne formaty bez zmian), `pn.set_backend([...])`
  kilka naraz, `pn.set_backend(None)` wszystko w programie; `pn.active_backends()`; `pn.using(...)` przywraca poprzedni zestaw.
- `xmutil` wymaga roota: Python przez `sudo` albo reguła sudoers dla `xmutil` (`pn.board` woła `sudo -n`), np.
  `/etc/sudoers.d/positnp`: `<user> ALL=(root) NOPASSWD: /usr/bin/xmutil, /usr/sbin/modprobe u-dma-buf`,
  plus reguła udev dająca dostęp do `/dev/uio*` i `/dev/udmabuf-*`.
- Aplikacje instaluje `scripts/install_overlay.sh <nazwa>` (kopiuje też `<nazwa>.json`, z którego `pn.board` zna silniki).

- Własna klasa, nie podklasa `ndarray`: nic nie przechodzi po cichu do arytmetyki na liczbach całkowitych;
  nieobsługiwane funkcje NumPy rzucają `TypeError` (`np.matmul`, `np.sin`: etap 7).
- Poziom 0 (neg, abs, porównania, sort, min, max, argmin, argmax) na bitach jako liczbach ze znakiem.
- NaR według standardu: równy sobie, mniejszy od każdej liczby (sortuje się na początek, jest minimum).
- Rozgłaszanie jak w NumPy; tablica ⊕ skalar idzie trybem skalarnym backendu.
- Redukcje (etap 4) idą przez quire backendu (`reduce`: FPGA albo `vecops.quire`): `sum`, `dot`, suma kwadratów
  są dokładne i zaokrąglone raz; `mean` (= quire / N) i `norm` (= √quire) zaokrągla raz Python z dokładnego quire.
- Etap 5: `np.sqrt`, `pn.fma(a, b, c)` (a · b + c, rozgłaszanie), `pn.axpy(α, x, y)`; na KV260 stała `c` idzie
  przez OP_FMA, skalarne `b` (lub `a`) z tablicą `c` przez OP_AXPY — oba w jednym przejściu strumienia;
  trzy tablice i operacje, których bitstream nie ma, liczy `PositSw` (`pn.stats()["fallback"]`).
- `pn.array(tablica float32)` i `x.to_float32()` / `x.astype(np.float32)` dla posit32 idą przez F2P / P2F backendu
  (bez rzutowania przez float64); dla innych formatów przez `vecops`.

Bindingi Pythona do silnika posit na KV260. Jedna klasa `PositArray`, dwa wymienne backendy:
sprzętowy i programowy, bitowo zgodny ze sprzętem — biblioteka działa też na PC bez płytki,
a testy porównują oba. Bez PYNQ: na płytce Ubuntu z `xmutil`, rejestry przez `/dev/uioN`.

```
positnp/
  ref.py           model referencyjny z flagami (port PositRef ze Scali)       etap 1
  regs.py          mapa rejestrów ALU (= posit.kv260.PositAluRegs)             etap 1
  backend_hw.py    PositAluHw (etap 1) i PositVecHw (etap 2: tablice przez DMA; vec_engines(),
                   open(n), open_all(), close() dla bitstreamów z kilkoma silnikami)
                   na UioMmio (/dev/uioN); FakeAluMmio do testów bez płytki     etap 1-2
  dma.py           AXI DMA w trybie prostym (PG021) przez UIO                  etap 2
  udmabuf.py       bufor ciągły z modułu u-dma-buf, synchronizacja cache        etap 2
  fakes.py         atrapy silnika etapu 2 i DMA do testów bez płytki           etap 2
  vecops.py        posit w NumPy: dekoder, enkoder z flagami, add/sub/mul/div,
                   konwersje float <-> posit, poziom 0; bit w bit z ref.py      etap 3
                   quire, sqrt, fma, float32 <-> posit32                        etap 4-5
  backend_sw.py    PositSw: te same array_op na vecops (bez płytki, wzorzec)   etap 2-3
  array.py         PositArray z protokołami NumPy, pn.array, using, stats       etap 3
  board.py         pn.board: aplikacje PL z Pythona (xmutil), silniki jako backendy
  overlays/        katalogi aplikacji dla xmutil: .bit.bin, .dtsi, shell.json (poza gitem)
tests/
  test_ref.py           ref.py kontra skróty SHA-256 z PositRef.scala (ref_digests.txt)
  test_backend_fake.py  sterownik na atrapie rejestrów
  test_board.py         na KV260, etap 1: UIO kontra ref.py (pomijany, gdy overlay nie jest wgrany)
  test_vec_fake.py      sterownik etapu 2 na atrapach: porcjowanie, protokół cache, błędy DMA
  test_array.py         PositArray, konwersje float <-> posit
  test_hypothesis.py    hypothesis: ścieżka sprzętowa (atrapy) == PositSw, konwersje == ref.py
  test_board_vec.py     na KV260, etap 2-5: tablice przez DMA kontra PositSw, PositArray na sprzęcie,
                        przepustowość z rozbiciem na fazy, quire, sqrt / FMA / konwersje
  test_vecops.py        vecops kontra skróty ze Scali, round-tripy, konwersje kontra ref.py
  test_quire.py         modele quire (ref.py, vecops) kontra Fraction, znoszenie się, kolejność
  test_examples.py      examples/formats.py, examples/quire_dot.py
  test_board_api.py     pn.board na PC: katalog firmware, xmutil, /sys/class/uio i u-dma-buf jako atrapy
examples/
  formats.py            float32 / posit32 / posit16 / float64: Horner i suma drzewem (--hw: posit32 na KV260)
  quire_dot.py          źle uwarunkowany iloczyn skalarny: float32, float64, posit32 bez i z quire
notebooks/
  01_formats.ipynb      to samo z wykresami
  02_quire_dot.ipynb    quire: błąd w funkcji uwarunkowania
scripts/
  install_overlay.sh    na płytce: dtc, /lib/firmware/xilinx/<nazwa>/, xmutil loadapp, sprawdzenie urządzeń
  build_udmabuf.sh      na płytce, raz na jądro: moduł u-dma-buf (bufory DMA)
```

```
pip install -e sw[test]
python3 -m pytest sw/tests
```

Na płytce (Ubuntu 24.04, bez instalacji pakietu; `python3 -m` z `sw/` widzi `positnp`):
```
sudo apt install device-tree-compiler python3-pytest python3-numpy python3-hypothesis
scripts/install_overlay.sh posit32_alu
sudo python3 -m pytest -v tests/test_board.py          # POSITNP_VECTORS=100000 dla większej liczby par
```
