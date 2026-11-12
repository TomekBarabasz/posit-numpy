# sw: positnp (Python)

Bindingi Pythona do silnika posit na KV260. Jedna klasa `PositArray`, dwa wymienne backendy:
sprzętowy (PYNQ: Overlay, MMIO, DMA) i programowy, bitowo zgodny ze sprzętem — biblioteka działa
też na PC bez płytki, a testy porównują oba.

```
positnp/
  ref.py           model referencyjny z flagami (port PositRef ze Scali)       etap 1
  regs.py          mapa rejestrów ALU (= posit.kv260.PositAluRegs)             etap 1
  backend_hw.py    PositAluHw: MMIO na PYNQ; FakeAluMmio do testów bez płytki  etap 1 (DMA: etap 2)
  overlays/        pary .bit + .hwh, po jednej na format (poza gitem)
  array.py         PositArray: operatory, __array_ufunc__, __array_function__   etap 2-3
  backend_sw.py    operacje na ref.py                                          etap 3
tests/
  test_ref.py           ref.py kontra skróty SHA-256 z PositRef.scala (ref_digests.txt)
  test_backend_fake.py  sterownik na atrapie rejestrów
  test_board.py         na KV260: MMIO kontra ref.py (pomijany bez pynq i overlayu)
```

```
pip install -e sw[test]
python3 -m pytest sw/tests
```

Na płytce (Kria-PYNQ, Python z pynq): `sudo -E python3 -m pytest -v sw/tests/test_board.py`,
format bitstreamu przez `POSITNP_BITFILE`, liczba par przez `POSITNP_VECTORS`.
