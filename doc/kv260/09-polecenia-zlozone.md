# 9. Polecenia złożone: matmul, FIR, tablice funkcji, FFT

Interfejs z etapu 6 (polecenie MEM: adresy, długość, start, done) okazał się na tyle ogólny, że wszystkie
późniejsze funkcje weszły w niego bez nowych portów. Zmieniało się tylko to, **co silnik robi ze strumieniem
elementów** między czytnikiem a pisarzem. Ten rozdział pokazuje każdą z tych funkcji z punktu widzenia
integracji: jakie polecenia, jakie rejestry, jakie dane w pamięci.

```
            czytnik a (b, c)                                    pisarz
 DDR ──► grupy po L elementów ──► [ co robi silnik? ] ──► grupy wyników ──► DDR
                                   ├─ tory ALU (etap 6)          element na element
                                   ├─ quire (redukcje)           N → 1 wynik w RESULT
                                   ├─ gemv: bufor x + quire       M·K → M (× P kolumn)
                                   ├─ okno: bufor kołowy + gemv   (M−1)·P + K → M·P
                                   ├─ tablica: T[a] z BRAM        N → N
                                   └─ rdzeń FFT: banki BRAM       2N·B → 2N·B
```

## 9.1 Dwa polecenia na jedno działanie: najpierw dane stałe, potem strumień

Wszystkie funkcje poniżej mają ten sam wzorzec: **ładowanie** (polecenie MEM, które tylko czyta,
bez zapisu, i wypełnia pamięć w silniku), a potem **obliczenie** (polecenie MEM ze strumieniem
danych i zapisem). Ładowanie robi się raz, obliczeń może być wiele.

| funkcja | ładowanie | pamięć w silniku | obliczenie |
| --- | --- | --- | --- |
| gemv / matmul | OP_XLOAD: kolumny x (K · cols elementów) | P buforów x (BRAM / URAM) | OP_GEMV: A (M · K) → y (M · cols) |
| FIR | OP_XLOAD: odczepy przesunięte o p w kolumnie p | jak wyżej | OP_GEMV + GEMV_MODE.window: sygnał → wyjścia |
| funkcje (exp, sin, ...) | OP_TABLE + TABLE_CTRL.load: 2ⁿ wartości | C kopii tablicy w BRAM | OP_TABLE: a → T[a] |
| FFT | FFT_CTRL.twiddle: W_N^e, 3N/4 wartości | 3 kopie twiddli | FFT_CTRL.fft: partia B transformat |

Python pamięta, co jest załadowane (`_table`, `_fft_tw`), i nie ładuje drugi raz tego samego.

## 9.2 Matmul: gemv na quire (wersje 5.1–5.2)

Rejestry: `GEMV_K` (0xCC) = K, `MEM_WLEN` (0xC8) = M wierszy, `GEMV_COLS` (0xD4) = kolumny naraz.
`MEM_LEN` = M·K. Kontrola kształtu w sprzęcie: `MEM_LEN ≠ M·K` albo K poza 1..maxK → `err_shape`.

Przebieg w silniku: element A[i, j] idzie do quire wiersza i (dwa banki quire na zmianę — zaokrąglanie
wiersza i odbywa się w cieniu wiersza i+1), mnożony przez x_c[j] w każdej z P kolumn naraz. Wyniki
scalane wierszami, zapis M·cols elementów. P = 4 → 4 mnożenia z dodawaniem na takt.

Python (`PositVecHw.matmul`): B dzielone na grupy po P kolumn; Bᵀ grupami w stercie urządzenia;
na grupę jedna para XLOAD + GEMV; A z `to_device()` czytana w miejscu; za mało miejsca → bloki wierszy
i kolumn; K > maxK → jeden dot quire na element. Na płytce: ok. 388 M MAC/s przy P = 4.

## 9.3 FIR: to samo gemv, inne wiersze (wersja 5.4)

y[i] = Σⱼ x[i + j]·h[j]. Wiersz r „macierzy A” to okno sygnału x[r·P .. r·P + K), K = T + P − 1,
a kolumna p ma odczepy przesunięte o p — jeden wiersz daje P kolejnych wyjść. Nowy jest tylko sposób
podawania wierszy: bit `GEMV_MODE.window`. Sygnał idzie z pamięci **raz** (`MEM_LEN = (M−1)·P + K`)
do **bufora kołowego** (2·maxK), a silnik czyta z niego nakładające się wiersze:

```
zapis do bufora czeka, gdy nadpisałby element >= rowStart (bieżący wiersz go jeszcze czyta)
odczyt elementu rowStart + m czeka, aż zostanie zapisany
```

Szczegół, który wyszedł w testach: kolumna p ma w buforze x zera poza zakresem odczepów, a 0 · NaR = NaR,
więc NaR z sygnału „przeciekał” do sąsiednich wyjść. Rozwiązanie: w trybie okna kolumna dostaje element
spoza swojego zakresu jako 0. Python: `np.convolve`, `np.correlate` (tryby jak NumPy).

## 9.4 Tablice funkcji (wersja 5.3)

posit16 ma 65 536 wartości — każdą funkcję jednej zmiennej można stablicować w całości, poprawnie
zaokrągloną (liczy Python, `positnp/funcs.py`). Silnik tylko czyta T[a]:
- `TABLE_CTRL.load = 1`, OP_TABLE, `MEM_LEN = 2ⁿ`: ładowanie (bez zapisu), każda wartość do wszystkich kopii;
- `TABLE_CTRL.load = 0`, OP_TABLE: odczyt; grupa L elementów w porcjach po 2C (dwuportowa BRAM, C kopii).

Ładowanie posit16 to 128 KB, ok. 0,7 ms — Python robi je raz na funkcję. W pamięci urządzenia:
exp posit16 332 M/s, posit8 648 M/s.

## 9.5 FFT (wersje 5.5–5.6): osobny rdzeń w tym samym interfejsie

Bit `FFT_CTRL.fft` zamienia polecenie MEM w **partię B transformat**: `MEM_LEN = 2N·B`, dane w układzie
(B, 2, N) — N części rzeczywistych, potem N urojonych — i tyle samo zapisu. Tryb FFT wyłącza wszystkie
inne ścieżki. Rdzeń `PositFftCore`:
- wczytanie z odwróceniem cyfr w bazie 4 do 4 banków BRAM (bank = suma cyfr mod 4 → cztery wejścia
  motylka zawsze w różnych bankach),
- S etapów po N/4 motylków radix-4 (12 mnożeń + 22 dodawania w potoku, jeden motylek na takt),
- zapis w porządku naturalnym;
- **trzy zestawy banków** (5.6): w każdym slocie jednocześnie wczytanie transformaty t, liczenie t−1
  i zapis t−2 — partia trwa ok. (B + 2)·max(2N/L, S·(N/4 + 15)) taktów;
- `FFT_CTRL.conj` neguje część urojoną przy wczytaniu i zapisie: N·ifft = conj(fft(conj x)) bez kopii.

Dlaczego partia w jednym poleceniu? Narzut polecenia (kilka zapisów rejestrów, sync, czekanie) jest
stały — przy 1024 transformatach 64-punktowych w jednym poleceniu rozkłada się na wszystkie.

## 9.6 Co z tego wynika dla projektowania interfejsu

- **Jeden mechanizm przenoszenia danych** (mover: czytniki, pisarz, kredyty, zakres) obsłużył wszystko.
  Nowa funkcja to nowa „środkowa” część silnika plus kilka rejestrów konfiguracji.
- **Stan w silniku** (bufor x, tablica, twiddle) ładuje się osobnym poleceniem; sterownik śledzi, co jest
  załadowane, i unika ponownego ładowania.
- **Kształt sprawdzany w sprzęcie**: każde polecenie złożone ma `err_shape`, bo złe długości inaczej
  zawiesiłyby silnik (pisarz czekałby na wyniki, które nigdy nie przyjdą).
- **Bity trybu** (window, load, fft, conj) zamiast nowych kodów OP — i zawsze zerowane po poleceniu.

## Do zapamiętania

- Wszystkie funkcje używają tego samego polecenia MEM; różni się środek silnika i kilka rejestrów.
- Wzorzec: polecenie ładujące (bez zapisu) + polecenia obliczające (ze strumieniem i zapisem).
- Długie partie w jednym poleceniu rozkładają stały narzut.
- Każdy tryb ma kontrolę kształtu przed startem — złe polecenie ma się skończyć błędem, a nie zawiesić.
