# =====================================================================
#  Pliki aplikacji dla xmutil: .dtsi i .json dla jednego albo kilku silnikow
#  PositVecAxi (kazdy z wlasnym AXI DMA i buforem u-dma-buf). Uzywa build.tcl;
#  bez Vivado (czysty Tcl), wiec da sie to sprawdzic samym tclsh + dtc:
#    tclsh overlay_files_test.tcl
#
#  Silnik = dict: n, vec, dma, buf (nazwy wezlow / urzadzen), vec_base, vec_size,
#  dma_base, dma_size, buf_size. Nazwy: posit_vec_p<n>, posit_dma_p<n>, udmabuf-posit-p<n>
#  (positnp.backend_hw.vec_engines() szuka wlasnie ich).
# =====================================================================

namespace eval posit_overlay {}

proc posit_overlay::engine {n} {
    return [dict create n $n vec posit_vec_p$n dma posit_dma_p$n buf udmabuf-posit-p$n]
}

# Nazwa aplikacji: posit32_vec dla jednego silnika, posit32_16_8_vec dla kilku.
proc posit_overlay::app_name {ns} {
    return "posit[join $ns _]_vec"
}

proc posit_overlay::cells64 {v} {
    return [format "0x%x 0x%x" [expr {($v >> 32) & 0xffffffff}] [expr {$v & 0xffffffff}]]
}

proc posit_overlay::write_dtsi {path name engines pl0_hz {stamp ""}} {
    set fh [open $path w]
    set ns {}
    foreach e $engines { lappend ns posit<[dict get $e n],2> }
    puts $fh "/*
 * $name: PositVecAxi + AXI DMA na KV260 ([join $ns {, }]), wygenerowane przez
 * hw/top-modules/vec/vivado/build.tcl $stamp
 * Na plytce: dtc -@ -O dtb -o $name.dtbo $name.dtsi  (robi to sw/scripts/install_overlay.sh)
 * Bufory DMA: modul u-dma-buf (sw/scripts/build_udmabuf.sh) -> /dev/udmabuf-posit-p<n>.
 */
/dts-v1/;
/plugin/;

&fpga_full {
\t#address-cells = <2>;
\t#size-cells = <2>;
\tfirmware-name = \"$name.bit.bin\";
\tresets = <&zynqmp_reset 116>;\t/* pl_resetn0 */
};

&amba {
\t#address-cells = <2>;
\t#size-cells = <2>;

\t/* szerokosci portow PS jak w block designie:
\t *   <4 0>, <5 0>: S_AXI_HP0_FPD (AFIFM2) odczyt i zapis 128 b
\t *   <14 0xa00>:   M_AXI_HPM0/HPM1_FPD 128 b */
\tafi0: afi0 {
\t\tcompatible = \"xlnx,afi-fpga\";
\t\tconfig-afi = <4 0>, <5 0>, <14 0xa00>;
\t};

\tclocking0: clocking0 {
\t\tcompatible = \"xlnx,fclk\";
\t\t#clock-cells = <0>;
\t\tclocks = <&zynqmp_clk 71>;\t/* PL0_REF */
\t\tclock-output-names = \"fabric_clk\";
\t\tassigned-clocks = <&zynqmp_clk 71>;
\t\tassigned-clock-rates = <$pl0_hz>;
\t};"
    foreach e $engines {
        dict with e {
            puts $fh "
\t/* silnik posit<$n,2>: rejestry, AXI DMA, ciagly bufor (adresy 32 b) */
\t$vec: $vec@[format %llx $vec_base] {
\t\tcompatible = \"generic-uio\";
\t\treg = <[cells64 $vec_base] [cells64 $vec_size]>;
\t\tclocks = <&clocking0>;
\t};

\t$dma: $dma@[format %llx $dma_base] {
\t\tcompatible = \"generic-uio\";
\t\treg = <[cells64 $dma_base] [cells64 $dma_size]>;
\t\tclocks = <&clocking0>;
\t};

\tposit_buf_p$n: posit_buf_p$n {
\t\tcompatible = \"ikwzm,u-dma-buf\";
\t\tdevice-name = \"$buf\";
\t\tsize = <[format 0x%x $buf_size]>;
\t};"
        }
    }
    puts $fh "};"
    close $fh
}

proc posit_overlay::json_str {s} { return "\"[string map {\\ \\\\ \" \\\"} $s]\"" }

# extra: lista {klucz wartosc_json ...} dopisywana na koncu (wersja Vivado, WNS, zasoby...)
proc posit_overlay::write_json {path name engines pl0_hz dma_max_bytes {extra {}}} {
    set items {}
    foreach e $engines {
        dict with e {
            lappend items "        \{\"n\": $n, \"ip\": [json_str $vec], \"base\": [expr {wide($vec_base)}], \"size\": $vec_size,\
\"dma\": [json_str $dma], \"dma_base\": [expr {wide($dma_base)}], \"dma_size\": $dma_size,\
\"udmabuf\": [json_str $buf], \"buf_size\": $buf_size\}"
        }
    }
    set fh [open $path w]
    puts $fh "\{
    \"overlay\": [json_str $name],
    \"engines\": \[
[join $items ",\n"]
    \],
    \"dma_max_bytes\": $dma_max_bytes,
    \"pl_clk0_hz\": $pl0_hz[expr {[llength $extra] ? "," : ""}]"
    set rows {}
    foreach {k v} $extra { lappend rows "    [json_str $k]: $v" }
    if {[llength $rows]} { puts $fh [join $rows ",\n"] }
    puts $fh "\}"
    close $fh
}
