# 3. Etap 1: ALU za rejestrami AXI4-Lite

Pierwsza architektura jest najprostsza możliwa: jednostka arytmetyczna posit (ALU) w PL, a procesor
podaje jej argumenty i odbiera wynik przez rejestry. Po co zaczynać od czegoś tak wolnego? Bo to
najkrótsza droga do sprawdzenia całego łańcucha: generator RTL → Vivado → bitstream → device tree →
UIO → Python. Każdy późniejszy etap używa tych samych elementów.

## 3.1 Schemat

```
 Python (proces na A53)
   │ mmap(/dev/uioN)  — okno 4 KB rejestrów
   ▼
 ┌──────── PS ─────────┐
 │ M_AXI_HPM0_FPD 128b │
 └─────────┬───────────┘
           ▼
   smartconnect (128 b → AXI4-Lite 32 b)
           ▼
 ┌──────────────── PL: posit_alu (PositAluAxiLite) ───────────────┐
 │  s_axi (AXI4-Lite) → AxiLite4SlaveFactory → rejestry            │
 │     OP, OPERAND_A, OPERAND_B ──► PositAlu ──► RESULT, STATUS    │
 │     CTRL: start / soft reset / clear counters (impulsy)         │
 │     liczniki: CNT_NAR, CNT_SAT, CNT_INEXACT, CYCLES             │
 └─────────────────────────────────────────────────────────────────┘
```

Pliki: `hw/posit-kv260/hw/spinal/main/PositAluAxiLite.scala` (sprzęt), `hw/top-modules/alu/` (generator
i `vivado/build.tcl`), `sw/positnp/backend_hw.py` (`PositAluHw`), `sw/positnp/regs.py` (mapa).

## 3.2 Mapa rejestrów

Rejestry są 32-bitowe, offsety w bajtach (`PositAluRegs`):

| offset | nazwa | dostęp | zawartość |
| --- | --- | --- | --- |
| 0x00 | ID_VERSION | RO | [31:16] 0x5053 („PS”), [15:8].[7:0] wersja (etap 1: 1.0) |
| 0x04 | CONFIG | RO | [7:0] n, [11:8] es, [31:16] maska zbudowanych operacji |
| 0x08 | CTRL | WO | bit 0 start, bit 1 miękki reset, bit 2 zerowanie liczników — **impulsy** |
| 0x0C | STATUS | RO | bit 0 busy, 1 done, 2 error, [10:8] flagi ostatniego wyniku (NaR, sat, inexact) |
| 0x10 | OP | RW | kod operacji (add 0, sub 1, mul 2, div 3) |
| 0x14 / 0x18 | OPERAND_A / B | RW | argumenty (bity positu) |
| 0x1C | RESULT | RO | ostatni wynik |
| 0x40–0x4C | CNT_* , CYCLES | RO | liczniki zdarzeń i taktów |

Trzy wzorce, które wracają we wszystkich etapach (rozwinięte w rozdziale 6):

1. **Rejestr identyfikacji** (ID_VERSION, CONFIG): Python najpierw pyta, *co* jest w bitstreamie,
   i dopiero potem z tym rozmawia. Zły ID → wyjątek zamiast śmieci.
2. **CTRL jako impulsy**: zapis 1 do bitu start nie jest „stanem”, tylko jednorazowym sygnałem
   w takcie zapisu (`bus.onWrite(CTRL) { start := ctrlData(0) }`). Nie trzeba go potem zerować.
3. **STATUS jako maszyna stanów widoczna z zewnątrz**: busy → done; done kasuje następny start.

## 3.3 Sekwencja jednego działania

`PositAluHw.op("add", a, b)` (`backend_hw.py`):

```python
self.write(regs.OP, regs.OPS[name])         # 1. AXI4-Lite zapis
self.write(regs.OPERAND_A, a & mask)        # 2.
self.write(regs.OPERAND_B, b & mask)        # 3.
self.write(regs.CTRL, 1 << regs.CTRL_START) # 4. impuls start
s = self._wait_done()                       # 5..k: odczyty STATUS, aż bit done
result = self.read(regs.RESULT)             # k+1. odczyt wyniku
```

Co dzieje się w sprzęcie po zapisie CTRL.start: rejestr polecenia (`cmd`) łapie OP i argumenty,
`busy := True`, polecenie idzie do ALU (Stream), po kilku taktach wraca odpowiedź z wynikiem
i flagami, `done := True`, `busy := False`. Działanie trwa kilka taktów (10 ns każdy) — **znikomo**
wobec czasu samych dostępów do rejestrów.

## 3.4 Skąd Python ma adres rejestrów — UIO

1. Overlay `.dtsi` ma węzeł `posit_alu@a0000000 { compatible = "generic-uio"; reg = <…>; }`.
2. Jądro Kria startuje z `uio_pdrv_genirq.of_id=generic-uio`, więc dla takiego węzła tworzy
   `/dev/uioN` i katalog `/sys/class/uio/uioN/` z plikiem `name` (= nazwa węzła) i `maps/map0/{addr,size}`.
3. `find_uio("posit_alu")` przegląda `/sys/class/uio/*/name` i znajduje właściwe N.
4. `UioMmio` robi `mmap` 4 KB z `/dev/uioN` — od tej chwili `memoryview[offset >> 2] = wartość`
   to zapis do rejestru.

Nie potrzebujemy `/dev/mem` ani roota do samych rejestrów (wystarczą prawa do `/dev/uio*`).
`DevMemMmio` istnieje tylko awaryjnie: czytanie z okna PL, w którym nie ma logiki, potrafi zawiesić
magistralę i cały system — dlatego nic nie przełącza się na `/dev/mem` samo.

## 3.5 Dlaczego to jest wolne (i dlaczego to dobrze wiedzieć)

Jedno działanie = 4 zapisy + co najmniej 2 odczyty przez HPM0 i smartconnect. Każdy dostęp to
pełna transakcja AXI4-Lite przez interconnect PS, rzędu mikrosekundy po stronie procesora,
a silnik liczy przez 10–100 ns. Przepustowość jest więc rzędu setek tysięcy działań na sekundę
(szacunek z liczby dostępów — zmierz `alu.time_op()` poniżej), czyli **setki razy mniej** niż 100 M/s,
które silnik mógłby dać.

Wniosek, który ukształtował resztę projektu: **dane nie mogą płynąć przez rejestry.**
Rejestry służą do opisania polecenia („dodaj tablicę spod adresu X do tablicy spod Y, wynik do Z,
długość N”), a same dane muszą płynąć przez szeroki port do pamięci, w seriach. To jest etap 2
(AXI DMA) i etap 6 (własny master).

## 3.6 Co z etapu 1 zostało na zawsze

- ID_VERSION / CONFIG i zasada „najpierw sprawdź, co jest w bitstreamie” — `PositVecHw.__init__`
  czyta wersję, maskę operacji, ENGINE_CFG, GEMV_CFG, TABLE_CFG, FFT_INFO.
- Ścieżka rejestrowa: w `PositVecAxi` tor 0 silnika nadal obsługuje pojedyncze działania przez
  OP / OPERAND_A / OPERAND_B / CTRL.start (`hw.op(...)` działa na każdym bitstreamie).
- Miękki reset (CTRL bit 1) i liczniki zdarzeń.
- Mapa rejestrów w dwóch miejscach (Scala i Python) i test zgodności między nimi
  (`vec_regs_python_match` w `PositVecAxiTestplan` czyta `regs.py` i porównuje każdą stałą).

## Do zapamiętania

- Rejestry AXI4-Lite to interfejs **sterowania**: kilka dostępów na polecenie.
- UIO: węzeł `generic-uio` w device tree → `/dev/uioN` → `mmap` → rejestry jako tablica słów 32 b.
- Jedno działanie przez rejestry kosztuje mikrosekundy; silnik potrzebuje nanosekund.
  Stąd cała dalsza architektura: dane przez pamięć i serie, rejestry tylko do poleceń.

## Sprawdź sam

```python
from positnp.backend_hw import PositAluHw       # na bitstreamie posit32_alu
alu = PositAluHw.open()
print(hex(alu.read(0x00)), alu.config, alu.ops)
print(alu.time_op("add") * 1e6, "µs na działanie (z dostępami do rejestrów)")
```
