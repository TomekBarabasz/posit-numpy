# sw: positnp (Python)

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
  backend_sw.py    PositSw: te same array_op na ref.py (wzorzec, bez płytki)   etap 2
  array.py         PositArray v0: + - * /, from_float / to_float, set_backend  etap 2 (protokoły NumPy: 3)
  overlays/        katalogi aplikacji dla xmutil: .bit.bin, .dtsi, shell.json (poza gitem)
tests/
  test_ref.py           ref.py kontra skróty SHA-256 z PositRef.scala (ref_digests.txt)
  test_backend_fake.py  sterownik na atrapie rejestrów
  test_board.py         na KV260, etap 1: UIO kontra ref.py (pomijany, gdy overlay nie jest wgrany)
  test_vec_fake.py      sterownik etapu 2 na atrapach: porcjowanie, protokół cache, błędy DMA
  test_array.py         PositArray, konwersje float <-> posit
  test_board_vec.py     na KV260, etap 2: tablice przez DMA kontra ref.py, przepustowość
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
sudo apt install device-tree-compiler python3-pytest python3-numpy
scripts/install_overlay.sh posit32_alu
sudo python3 -m pytest -v tests/test_board.py          # POSITNP_VECTORS=100000 dla większej liczby par
```
