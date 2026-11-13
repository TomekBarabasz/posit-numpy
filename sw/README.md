# sw: positnp (Python)

```python
import numpy as np, positnp as pn
x = pn.array(np.linspace(-2, 2, 5))          # posit<32,2>; pn.array(x, pn.P16) dla posit<16,2>
y = x * x - x / 3 + 1                         # każdy operator = jedno array_op na backendzie
np.sort(y), np.max(y), np.argmax(y), -y, abs(y), y < 0, y[1:3]
y.to_float()                                  # float64, NaR -> nan
with pn.using(PositVecHw.open()):             # KV260 (domyślnie PositSw na vecops)
    z = x * x
pn.stats()                                    # NaR / nasycenia / niedokładne / elementy
np.sum(x), np.dot(x, y), np.mean(x), np.linalg.norm(x)   # quire: dokładnie, jedno zaokrąglenie
pn.quire(x, y)                                # dokładna wartość quire jako Fraction
```

- Własna klasa, nie podklasa `ndarray`: nic nie przechodzi po cichu do arytmetyki na liczbach całkowitych;
  nieobsługiwane funkcje NumPy rzucają `TypeError` (`np.sum`, `np.dot`: etap 4, `np.sin`: etap 7).
- Poziom 0 (neg, abs, porównania, sort, min, max, argmin, argmax) na bitach jako liczbach ze znakiem.
- NaR według standardu: równy sobie, mniejszy od każdej liczby (sortuje się na początek, jest minimum).
- Rozgłaszanie jak w NumPy; tablica ⊕ skalar idzie trybem skalarnym backendu.
- Redukcje (etap 4) idą przez quire backendu (`reduce`: FPGA albo `vecops.quire`): `sum`, `dot`, suma kwadratów
  są dokładne i zaokrąglone raz; `mean` (= quire / N) i `norm` (= √quire) zaokrągla raz Python z dokładnego quire.

Bindingi Pythona do silnika posit na KV260. Jedna klasa `PositArray`, dwa wymienne backendy:
sprzętowy i programowy, bitowo zgodny ze sprzętem — biblioteka działa też na PC bez płytki,
a testy porównują oba. Bez PYNQ: na płytce Ubuntu z `xmutil`, rejestry przez `/dev/uioN`.

```
positnp/
  ref.py           model referencyjny z flagami (port PositRef ze Scali)       etap 1
  regs.py          mapa rejestrów ALU (= posit.kv260.PositAluRegs)             etap 1
  backend_hw.py    PositAluHw (etap 1) i PositVecHw (etap 2: tablice przez DMA)
                   na UioMmio (/dev/uioN); FakeAluMmio do testów bez płytki     etap 1-2
  dma.py           AXI DMA w trybie prostym (PG021) przez UIO                  etap 2
  udmabuf.py       bufor ciągły z modułu u-dma-buf, synchronizacja cache        etap 2
  fakes.py         atrapy silnika etapu 2 i DMA do testów bez płytki           etap 2
  vecops.py        posit w NumPy: dekoder, enkoder z flagami, add/sub/mul/div,
                   konwersje float <-> posit, poziom 0; bit w bit z ref.py      etap 3
  backend_sw.py    PositSw: te same array_op na vecops (bez płytki, wzorzec)   etap 2-3
  array.py         PositArray z protokołami NumPy, pn.array, using, stats       etap 3
  overlays/        katalogi aplikacji dla xmutil: .bit.bin, .dtsi, shell.json (poza gitem)
tests/
  test_ref.py           ref.py kontra skróty SHA-256 z PositRef.scala (ref_digests.txt)
  test_backend_fake.py  sterownik na atrapie rejestrów
  test_board.py         na KV260, etap 1: UIO kontra ref.py (pomijany, gdy overlay nie jest wgrany)
  test_vec_fake.py      sterownik etapu 2 na atrapach: porcjowanie, protokół cache, błędy DMA
  test_array.py         PositArray, konwersje float <-> posit
  test_hypothesis.py    hypothesis: ścieżka sprzętowa (atrapy) == PositSw, konwersje == ref.py
  test_board_vec.py     na KV260, etap 2-3: tablice przez DMA kontra PositSw, PositArray na sprzęcie,
                        przepustowość z rozbiciem na fazy
  test_vecops.py        vecops kontra skróty ze Scali, round-tripy, konwersje kontra ref.py
  test_quire.py         modele quire (ref.py, vecops) kontra Fraction, znoszenie się, kolejność
  test_examples.py      examples/formats.py, examples/quire_dot.py
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
