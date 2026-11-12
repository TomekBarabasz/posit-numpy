# sw: positnp (Python)

Bindingi Pythona do silnika posit na KV260. Jedna klasa `PositArray`, dwa wymienne backendy:
sprzętowy (PYNQ: Overlay, MMIO, DMA) i programowy, bitowo zgodny ze sprzętem — biblioteka działa
też na PC bez płytki, a testy porównują oba.

```
positnp/
  __init__.py
  array.py         PositArray: operatory, __array_ufunc__, __array_function__   (etap 2-3)
  ref.py           model referencyjny (port PositRef ze Scali)                 (etap 1)
  backend_sw.py    operacje na ref.py                                          (etap 3)
  backend_hw.py    Overlay, MMIO, DMA, bufory z pynq.allocate                  (etap 1-2)
  overlays/        pary .bit + .hwh, po jednej na format
tests/             pytest: backend_hw kontra backend_sw
```

```
pip install -e sw[test]
pytest sw/tests
```
