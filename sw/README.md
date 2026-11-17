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
x ** 3, np.var(x), np.std(x), np.cumsum(x), np.prod(x)   # dokładnie, jedno zaokrąglenie (w PS)
np.clip(x, -1, 1), np.floor(x), np.round(x)   # na bitach / dokładnie
x.astype(pn.P16), x + pn.array(y, pn.P16)     # inne formaty; P8 / P16 / P32 razem -> najszerszy
np.linalg.solve(A, b), np.linalg.inv(A), np.linalg.det(A)   # LU z quire + poprawianie z residuum w quire
X * Y, X / Y, abs(X)                          # PositComplex: składowe dokładnie, zaokrąglone raz
pn.save("x.npz", x), pn.load("x.npz")         # z formatem (np.save odmawia: .npy go nie zapisze)
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

- Pamięć urządzenia (silniki `*_mem`): `x.to_device()` / `pn.to_device(x)` przenosi tablicę do bufora silnika;
  działania na takich tablicach idą bez kopii i zostawiają wyniki w pamięci urządzenia (`y.on_device`,
  `y.to_host()`); opis w `hw/top-modules/vec/README.md`.
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
  nieobsługiwane funkcje NumPy rzucają `TypeError` (`np.sin`, `np.exp`: etap 7).
- Poziom 0 (neg, abs, porównania, sort, min, max, argmin, argmax) na bitach jako liczbach ze znakiem.
- NaR według standardu: równy sobie, mniejszy od każdej liczby (sortuje się na początek, jest minimum).
- Rozgłaszanie jak w NumPy; tablica ⊕ skalar idzie trybem skalarnym backendu.
- Redukcje (etap 4) idą przez quire backendu (`reduce`: FPGA albo `vecops.quire`): `sum`, `dot`, suma kwadratów
  są dokładne i zaokrąglone raz; `mean` (= quire / N) i `norm` (= √quire) zaokrągla raz Python z dokładnego quire.
- Etap 5: `np.sqrt`, `pn.fma(a, b, c)` (a · b + c, rozgłaszanie), `pn.axpy(α, x, y)`; na KV260 stała `c` idzie
  przez OP_FMA, skalarne `b` (lub `a`) z tablicą `c` przez OP_AXPY — oba w jednym przejściu strumienia;
  trzy tablice i operacje, których bitstream nie ma, liczy `PositSw` (`pn.stats()["fallback"]`).
- Etap 7a: `A @ B`, `np.matmul`, `np.dot` macierzy, `pn.matmul` (też macierz · wektor i wsadowe `(..., M, K) @ (K, N)`):
  każdy element to dokładny iloczyn skalarny w quire, zaokrąglony raz. Na silniku z gemv (PositVecAxi 5.1,
  `VecKv260 --gemv-k`, domyślnie K ≤ 4096) kolumny B idą kolejno do bufora x silnika (OP_XLOAD), a A przechodzi
  przez dwa quire na zmianę (OP_GEMV, 1 mnożenie z dodawaniem na takt); A, Bᵀ i C siedzą w pamięci urządzenia
  (A już na urządzeniu: `A.to_device()` — czytana w miejscu). Silnik 5.2 liczy P kolumn B na jedno przejście A
  (`hw.gemv_cols`, domyślnie 4: 4 MAC na takt). K > 4096, brak gemv albo pełna pamięć urządzenia:
  jeden dot quire na element (też dokładnie). `hw.time_matmul(m, k, n)` mierzy MAC/s.
- Etap 7, FFT: `pn.fft.fft`, `pn.fft.ifft`, `pn.fft.rfft` (i `np.fft.fft / ifft / rfft` na `PositArray`) zwracają
  `PositComplex` (`.real`, `.imag`, `.to_complex()`; `re, im = pn.fft.fft(x)`); wejście: PositArray, para (re, im),
  PositComplex albo tablica NumPy (też zespolona). Bloki DFT do 64 punktów dokładnie w quire (matmul), twiddle przez FMA3;
  `exact=True` — jedno DFT (N ≤ 2048), jedno zaokrąglenie na wyjście. Silnik 5.5 z rdzeniem FFT (`--fft`): `method="auto"`
  liczy N = 4^S na rdzeniu radix-4 (`hw.fft_core`, model `pn.fft.fft_core_model`, `method="core"` w programie).
  `X.to_device()` (PositComplex) kładzie dane w buforze urządzenia (…, 2, N): fft / ifft czytają go w miejscu i zostawiają
  wynik na urządzeniu (`X.on_device`, `X.to_host()`).
  Opis i dokładność: `hw/top-modules/vec/README.md`.
- Etap 7, splot: `np.convolve`, `np.correlate` (tryby full / same / valid), `pn.convolve`, `pn.correlate`; każde wyjście
  dokładnie w quire, zaokrąglone raz; NaR tylko tam, gdzie jest składnikiem sumy (jak `nan` w NumPy). Silnik 5.4:
  FIR przez gemv z oknem (`hw.correlate`, P wyjść na wiersz); bez okna albo dla odczepów dłuższych niż maxK − P + 1: PositSw.
- Etap 7, funkcje: `np.exp`, `np.exp2`, `np.expm1`, `np.log`, `np.log2`, `np.log10`, `np.log1p`, `np.sin`, `np.cos`, `np.tan`,
  `np.arcsin`, `np.arccos`, `np.arctan`, `np.sinh`, `np.cosh`, `np.tanh`, `np.arcsinh`, `np.arccosh`, `np.arctanh`, `np.cbrt`,
  `pn.sigmoid(x)`, `pn.func(name, x)` (`positnp/funcs.py`). posit8 / posit16: pełne tablice, poprawnie zaokrąglone
  (sprawdzone ±4 ulp float64, `funcs.uncertified`); posit32: float64 libm zaokrąglone raz (poprawne poza wynikami
  w odległości 1 ulp float64 od remisu). Reguły positów: NaR, bieguny → NaR, nadmiar → maxpos, niedomiar → minpos.
  Silniki 5.3 posit8 / posit16 czytają tablicę same (OP_TABLE, `hw.func`, `hw.time_func`).
- Uzupełnienia API (wszystko liczone dokładnie i zaokrąglane raz; bez nowego sprzętu):
  - formaty: `x.astype(pn.P16)` (poszerzenie przy tym samym es bez zaokrąglenia, inaczej jedno zaokrąglenie), działania
    na P8 / P16 / P32 naraz idą w najszerszym formacie (posit⟨8,es⟩ ⊂ posit⟨16,es⟩ ⊂ posit⟨32,es⟩, jak float32 + float64
    w NumPy); różne es → `TypeError`;
  - `x ** n`: 2 = mul, −1 = div, 0.5 = sqrt na silniku; inne całkowite n dokładnie w PS (liczby całkowite Pythona),
    jedno zaokrąglenie; `0 ** 0` = NaR (standard posit: pow(0, 0)); `np.power`, `np.reciprocal`;
  - `np.var`, `np.std` (`axis`, `ddof`): z dwóch sum w quire, var = (N·Σx² − (Σx)²) / (N·(N − ddof)) dokładnie, bez
    znoszenia się, które psuje var we floatach; std = pierwiastek z dokładnej wariancji, zaokrąglony raz;
  - `np.clip` (na bitach; NaR zostaje NaR), `np.floor`, `np.ceil`, `np.trunc`, `np.rint`, `np.round` (tylko
    `decimals=0`): dokładnie, bo zaokrąglony do liczby całkowitej posit też jest positem;
  - `np.cumsum` (każdy prefiks dokładnie, zaokrąglony raz), `np.prod` (dokładny iloczyn, zaokrąglony raz, z nasyceniem):
    w PS, bo silnik ich nie ma;
  - `np.linalg.solve`, `inv`, `det` (`positnp/linalg.py`): LU z wyborem elementu głównego, każdy element z jednej
    dokładnej sumy w quire (`vecops.fused_dot`), podstawienia tak samo, `refine=1` krok poprawiania z residuum liczonym
    w quire; w PS, na wszystkich backendach te same bity;
  - `PositComplex`: `+ - * /`, `abs`, `conj`; składowe iloczynu (ac − bd, ad + bc) i ilorazu oraz `abs` (hypot)
    dokładnie, zaokrąglone raz;
  - `pn.save(plik, x)` / `pn.load(plik)`: .npz z bitami oraz n i es (także `PositComplex`); `np.save` na posicie
    zgłasza błąd z podpowiedzią.
- Szersze API NumPy (`positnp/npapi.py`, `PositComplex` w `positnp/fft.py`):
  - redukcje (`sum`, `mean`, `max`, `min`, `prod`, `var`, `std`, `argmax`, `any`, `all`, `cumsum`): `axis` jako liczba,
    ujemna albo krotka, `keepdims`, `out=` (zapis do podanej `PositArray`), `np.add.reduce` / `accumulate`,
    `np.maximum.reduce`; nieobsługiwana opcja (`where=`, `initial=`, ...) zgłasza `TypeError`, nic nie jest cicho
    pomijane; `sum` po osi dla wielu wierszy idzie jednym `matmul` z kolumną jedynek (na silniku z gemv: jedno
    polecenie, te same bity co suma w quire wiersz po wierszu);
  - funkcje kształtu na bitach: `flip`, `roll`, `rot90`, `tile`, `repeat`, `squeeze`, `expand_dims`, `swapaxes`,
    `moveaxis`, `broadcast_to`, `diag`, `diagonal`, `triu`, `tril`, `take`, `delete`, `insert`, `append`, `pad` (tryby
    constant / edge / reflect / symmetric / wrap), `split`, `hstack`, `vstack`, `column_stack`, `zeros_like`,
    `ones_like`, `full_like`; `pn.ones`, `pn.full`, `pn.eye`, `pn.identity`, `pn.arange`, `pn.linspace`;
  - wybór po wartościach: `unique`, `searchsorted`, `partition`, `nonzero`, `count_nonzero`, `argwhere`;
  - dokładnie, jedno zaokrąglenie: `np.hypot`, `np.fmod`, `np.remainder` (`%`), `np.floor_divide`, `np.median`,
    `np.percentile`, `np.quantile` (metody linear / lower / higher / nearest / midpoint, z dokładnych ułamków),
    `np.average(weights=)`, `np.trace`, `np.diff`; `np.nansum`, `nanmean`, `nanmax`, `nanmin`, `nanvar`, `nanstd`
    (NaR jako brak wartości); na bitach: `np.sign`, `signbit`, `copysign`, `isfinite`, `isinf`, `fmax`, `fmin`,
    `logical_*`; `np.arctan2` przez float64 (jak funkcje posit32), `arctan2(0, 0)` = NaR (standard);
  - `np.isclose` / `np.allclose` na dokładnych wartościach; `int(x)`, `bool(x)`, `x.tolist()`, `x.item()`, `x.nbytes`;
    `np.asarray(x)` zgłasza błąd z podpowiedzią (`x.to_float()` albo `x.bits`) zamiast tablicy obiektów;
  - `np.linalg.norm` z `ord` (None / 2, 1, inf, −inf, 0 dla wektorów; 'fro', 1, −1, inf, −inf dla macierzy) i `axis`,
    `keepdims`; `ord=2` / 'nuc' dla macierzy wymaga SVD - `TypeError`;
  - FFT: `norm=` ('backward', 'forward', 'ortho'; 1 / N dokładnym dzieleniem przez liczbę całkowitą, 1 / sqrt N
    dokładnie, jedno zaokrąglenie), `np.fft.irfft`, `fft2`, `ifft2`, `fftn`, `ifftn`, `fftshift`, `ifftshift`;
    `np.fft.ifft(X)` na `PositComplex` (wcześniej działało tylko `pn.fft.ifft`);
  - `PositComplex` jak tablica: `z[i]`, wycinki, przypisanie, `len`, `reshape`, `sum`, `mean`, `z ** n` (dokładna
    potęga, każda część zaokrąglona raz), `A @ B` zespolone (Re = [Ar, Ai] @ [Br; −Bi] - jeden matmul o długości
    2K, na gemv), `np.vdot`, `np.dot`, `np.angle`, `np.concatenate`, funkcje kształtu; `pn.array(liczby zespolone)`
    i `x + 1j` dają `PositComplex`.
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
  funcs.py         funkcje elementarne: tablice posit8 / posit16, posit32 z float64  etap 7
  fft.py           FFT: bloki DFT przez matmul (quire), twiddle przez FMA3, PositComplex  etap 7
  array.py         PositArray z protokołami NumPy, pn.array, using, stats       etap 3
  board.py         pn.board: aplikacje PL z Pythona (xmutil), silniki jako backendy
  devmem.py        pamięć urządzenia: sterta w buforze u-dma-buf, tablice bez kopii
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
  test_examples.py      examples/: formats.py, quire_dot.py, cała seria NN_*.py (też na atrapach silników)
  test_board_api.py     pn.board na PC: katalog firmware, xmutil, /sys/class/uio i u-dma-buf jako atrapy
examples/               przewodnik po bibliotece (po angielsku): examples/README.md
  00-03                 instalacja, formaty, pn.board i wykrywanie możliwości silników, backendy i liczniki
  10-13                 arytmetyka, NumPy, quire (sum / dot / mean / norm), fma / sqrt / exp / log ...
  20-23                 gemv / gemm, FIR (np.convolve / correlate), FFT, pamięć urządzenia
  30-33                 gdzie posity wygrywają z floatami: precyzja wg rzędu wielkości, zakres w 16 bitach,
                        rozwiązywanie układów z resztą w quire, powtarzalne sumy
  template.py           szablon własnego eksperymentu (posit / float obok siebie)
  _common.py            --hw [APP] / --sim, błędy względne, tabele, bfloat16 / float8 (round_minifloat)
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
