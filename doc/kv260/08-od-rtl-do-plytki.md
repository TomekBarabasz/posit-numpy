# 8. Od RTL do płytki: budowa, device tree, ładowanie

## 8.1 Cały łańcuch

```
 komputer z Vivado                                                   KV260 (Ubuntu)
 ──────────────────                                                  ──────────────
 1. sbt "top-vec/runMain posit.top.vec.VecKv260 ..."   SpinalHDL → Verilog (hw/top-modules/vec/hw/gen/p<n>/)
 2. vivado -mode batch -source build.tcl -tclargs -engines "32 16 8"
      a. package_ip.tcl: Verilog → IP (po jednym na silnik)
      b. block design: PS, smartconnecty, silniki, reset, przerwania, wentylator
      c. synteza, implementacja, bitstream; WNS < 0 → błąd, nic nie jest kopiowane
      d. bootgen: .bit → .bit.bin;  overlay_files.tcl: .dtsi + .json;  shell.json
      → sw/positnp/overlays/<nazwa>/
                                                        3. scripts/build_udmabuf.sh   (raz na jądro)
                                                        4. scripts/install_overlay.sh <nazwa>
                                                             dtc .dtsi → .dtbo, kopia do /lib/firmware/xilinx/<nazwa>/,
                                                             xmutil unloadapp; xmutil loadapp <nazwa>,
                                                             sprawdzenie /dev/uio* i /dev/udmabuf-*
                                                        5. Python: pn.board.load("<nazwa>") albo PositVecHw.open(32)
```

## 8.2 Generator RTL: co wchodzi do silnika

`VecKv260` (`hw/top-modules/vec/hw/spinal/main/VecKv260.scala`) generuje po jednym `PositVecAxi_p<n>`
na format. Specyfikacja silnika `n:es:k:u:L` i opcje:

| argument | znaczenie |
| --- | --- |
| `32:2:4:4:2` | posit⟨32,2⟩, dzielnik 4 bity / takt, 4 tory div/sqrt/FMA, L = 2 tory elementowe |
| `--dma` | stara ścieżka: AXI4-Stream + AXI DMA (etapy 2–5) |
| `--gemv-k K`, `--gemv-cols P` albo `32=4,16=2,8=0` | gemv: pojemność bufora x, kolumny (na format; 0 = bez gemv) |
| `--table-copies C` | kopie tablicy funkcji (posit8 / 16) |
| `--fft S[:32,16]` | rdzeń FFT do N = 4^S w wybranych formatach |

Moduły Verilog dostają przedrostek `p<n>_`, więc kilka silników mieści się w jednym projekcie Vivado.

Ograniczenie miejsca to realny problem (rozdział 1.6): przy wszystkich blokach naraz placer zgłosił
„unplaced instances require 10 925 CLBs” z 9 894 dostępnych. Build wtedy się przerywa, a na płytce
zostaje **poprzedni** bitstream — testy przechodzą, ale bez nowych funkcji. Zawsze sprawdzaj wersję
(`hw.version`) albo `pn.board.describe()` po instalacji.

## 8.3 Device tree overlay — opis sprzętu dla jądra

Bez PYNQ jądro nie wie nic o tym, co jest w PL. Overlay `.dtsi` (generowany przez
`posit_overlay::write_dtsi` w `overlay_files.tcl`) mówi mu to w jednym pliku:

```dts
&fpga_full {
    firmware-name = "posit32_16_8_mem.bit.bin";   /* co wgrać */
    resets = <&zynqmp_reset 116>;                 /* pl_resetn0 */
};
&amba {
    afi0: afi0 { compatible = "xlnx,afi-fpga";
                 config-afi = <4 0>, <5 0>, <14 0xa00>; };   /* szerokości HP0 i HPM0: 128 b */
    clocking0: clocking0 { compatible = "xlnx,fclk"; clocks = <&zynqmp_clk 71>;
                           assigned-clock-rates = <100000000>; };   /* pl_clk0 */

    posit_vec_p32: posit_vec_p32@a0001000 {        /* rejestry silnika → /dev/uioN (adres z assign_bd_address) */
        compatible = "generic-uio";
        reg = <0x0 0xa0001000 0x0 0x1000>;
        clocks = <&clocking0>;
        interrupt-parent = <&gic>;
        interrupts = <0 89 4>;                     /* pl_ps_irq0[0], poziom wysoki */
    };
    posit_buf_p32: posit_buf_p32 {                 /* bufor → /dev/udmabuf-posit-p32 */
        compatible = "ikwzm,u-dma-buf";
        device-name = "udmabuf-posit-p32";
        size = <0x4000000>;                        /* 64 MB */
    };
    /* ... to samo dla p16, p8 ... */
};
```

Każdy element ma swoją rolę:
- **fpga_full / firmware-name**: fpga_manager wgrywa bitstream, reset PL;
- **afi0**: szerokości portów PS siedzą w rejestrach AFI, nie w bitstreamie. Gdyby nie zgadzały się
  z block designem (128 b), rejestry pod offsetami ≠ 0 czytałyby się źle;
- **clocking0**: włącza zegar PL0 i ustawia 100 MHz (rozdział 1.3 — dzielnik PLL);
- **generic-uio**: dla każdego okna rejestrów urządzenie UIO o nazwie węzła;
- **u-dma-buf**: ciągły bufor z CMA dla każdego silnika.

`overlay_files_test.tcl` sprawdza generowanie `.dtsi` bez Vivado (sam tclsh + dtc).

## 8.4 xmutil i dfx-mgr

```bash
sudo xmutil listapps            # aplikacje w /lib/firmware/xilinx; aktywna ma Active_slot >= 0
sudo xmutil unloadapp           # zdejmuje bieżącą (overlay i bitstream)
sudo xmutil loadapp <nazwa>     # bitstream + overlay → urządzenia UIO i u-dma-buf
```

`pn.board` (`board.py`) robi to samo z Pythona: `apps()`, `active()`, `load(name)` (z `sudo -n`,
czekanie na urządzenia z `<nazwa>.json`), `open()`, `describe()`, `unload()`. Ładowanie wymaga roota:
Python przez `sudo` albo reguła sudoers dla `xmutil` i `modprobe u-dma-buf`, plus reguła udev dająca
dostęp do `/dev/uio*` i `/dev/udmabuf-*`.

## 8.5 u-dma-buf: moduł jądra spoza drzewa

`scripts/build_udmabuf.sh` klonuje ikwzm/udmabuf, buduje moduł dla **bieżącego** jądra
(`linux-headers-$(uname -r)`), instaluje do `/lib/modules/$(uname -r)/extra`, `depmod`, ładowanie przy
starcie. Po aktualizacji jądra trzeba go zbudować ponownie — inaczej `modprobe: FATAL: Module u-dma-buf
not found`. To nam się zdarzyło: automatyczna aktualizacja zainstalowała nowe jądro w tle, twardy restart
przerwał `dpkg`, a płytka wstała na nowym jądrze bez modułu. Stąd zalecenie w README: na płytce roboczej
wyłączyć `unattended-upgrades` i aktualizować ręcznie.

## 8.6 Wentylator — przykład, jak PS i PL dzielą piny

Fabryczna aplikacja Kria steruje wentylatorem przez PWM z timera **TTC0** w PS, wyprowadzony przez
**EMIO** do PL i dalej na pin `fan_en_b` (A12). Bitstream, który tego połączenia nie ma, zostawia pin
niesterowany i wentylator kręci się na maksa. `kv260_fan.tcl`:
- `ps_config`: włącza TTC0 z wyjściem fali na EMIO,
- `connect`: wycina bit 2 z `emio_ttc0_wave_o` (xlslice) na port `fan_en_b`,
- `xdc`: PACKAGE_PIN A12, LVCMOS33, SLEW SLOW, DRIVE 4.

Krzywą temperatura → obroty dalej robi Linux (pwm-fan z bazowego device tree). Lekcja: logika PL
może być „przewodem” między peryferium PS a pinem — i jeśli bitstream go nie poprowadzi, nikt tego nie zrobi.

## 8.7 Weryfikacja przed płytką

Każda zmiana sprzętu przechodzi trzy poziomy, zanim trafi na płytkę:
1. **testplany SpinalHDL** (vertebra): DUT w symulacji z modelem pamięci AXI (`AxiMemModel`) albo
   sterownikiem AXI4-Lite; wynik bit w bit z wzorcem w Scali (`PositRef`, `QuireRef`). Verilator i GHDL.
2. **atrapy w Pythonie** (`fakes.py`): `FakeVecMmio` udaje rejestry i polecenia MEM, `FakeUDmaBuf` bufor;
   cały `PositVecHw` (porcje, podwójny bufor, sterta, gemv, FFT) działa na PC. Tryb `lazy_mem` wykonuje
   polecenie dopiero przy czekaniu — wyłapuje błędy protokołu bufora.
3. **testy na płytce** (`tests/test_board_vec.py`): każdy test na każdym silniku, porównanie bit w bit
   z `PositSw` i pomiary przepustowości z rozbiciem czasu (`last_timing`).

`sw/scripts/diag_gemv.py` to przykład narzędzia „krok po kroku”: wydaje polecenia pojedynczo i po każdym
wypisuje rejestry — przy problemie na płytce od razu widać, które polecenie się nie skończyło.

## Do zapamiętania

- Łańcuch: SpinalHDL → Verilog → IP → block design → bitstream + .dtsi → .dtbo → xmutil → UIO / u-dma-buf → Python.
- Device tree overlay zastępuje PYNQ: mówi jądru o bitstreamie, zegarze, szerokościach portów, rejestrach, buforach i przerwaniach.
- Nieudany build zostawia stary bitstream — zawsze sprawdzaj wersję po instalacji.
- u-dma-buf to moduł spoza drzewa: po każdej zmianie jądra trzeba go zbudować ponownie.
