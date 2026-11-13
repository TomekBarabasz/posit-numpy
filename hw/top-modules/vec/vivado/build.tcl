# =====================================================================
#  Etap 2: aplikacja PL dla xmutil z silnikiem na tablicach (PositVecAxi)
#  i AXI DMA na KV260. Bez PYNQ: Ubuntu + xmutil, rejestry przez /dev/uioN,
#  bufory przez u-dma-buf (/dev/udmabuf-posit).
#
#    RTL -> IP (../../common/vivado/package_ip.tcl) -> block design -> bitstream
#        -> bootgen: .bit.bin, + .dtsi (region FPGA, zegar PL0, AFI, UIO x2, u-dma-buf), shell.json
#
#  Block design (pl_clk0 = 100 MHz dla wszystkiego):
#    zynq_ultra_ps_e_0  M_AXI_HPM0_FPD (128 b) -> sterowanie
#                       S_AXI_HP0_FPD  (128 b) <- dane z/do DDR
#    rst_pl0            proc_sys_reset
#    smartconnect_ctl   HPM0 -> posit_vec (AXI4-Lite) i posit_dma/S_AXI_LITE
#    posit_dma          AXI DMA, tryb prosty (bez SG), dlugosc transferu 26 b (64 MB - 1)
#                       MM2S: DDR -> posit_vec/s_axis ({b, a}, 2n b), S2MM: posit_vec/m_axis (n b) -> DDR
#    smartconnect_data  posit_dma/M_AXI_MM2S + M_AXI_S2MM -> HP0
#    posit_vec          PositVecAxi; nazwy posit_vec i posit_dma = nazwy wezlow UIO w .dtsi
#
#  Uruchomienie z tego katalogu, po:  sbt "top-vec/runMain posit.top.vec.VecKv260 <n>"
#    vivado -mode batch -nojournal -log build.log -source build.tcl
#    vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -n 16 -buf_mb 128
#
#  Opcje (-tclargs):
#    -n <bity>        format: 8, 16, 32 (rtl z ../hw/gen/p<n>/)         (32)
#    -part <part>                                                       (xck26-sfvc784-2LV-c)
#    -out <dir>       katalog roboczy                                   (./out)
#    -deploy <dir>    gdzie zapisac katalog aplikacji                   (../../../../sw/positnp/overlays)
#    -buf_mb <MB>     rozmiar bufora DMA (u-dma-buf) w .dtsi             (64)
#    -jobs <n>        rownolegle przebiegi                              (8)
#    -threads <n>     watki Vivado na przebieg (general.maxThreads)     (8)
#    -stop ip|bd      zatrzymaj po spakowaniu IP albo po block designie
#
#  Wynik: <deploy>/posit<n>_vec/ (.bit.bin, .dtsi, shell.json, .json); na plytce
#  sw/scripts/install_overlay.sh posit<n>_vec. WNS < 0 -> blad, nic nie jest kopiowane.
# =====================================================================

set ::posit_build 1
set script_dir [file normalize [file dirname [info script]]]
source [file join $script_dir .. .. common vivado package_ip.tcl]

array set opt [list \
    n       32 \
    part    xck26-sfvc784-2LV-c \
    out     [file join $script_dir out] \
    deploy  [file normalize [file join $script_dir .. .. .. .. sw positnp overlays]] \
    buf_mb  64 \
    jobs    8 \
    threads 8 \
    stop    "" \
]

proc usage {} {
    puts "uzycie: vivado -mode batch -source build.tcl -tclargs \[-n bity\] \[-part part\] \[-out dir\] \[-deploy dir\] \[-buf_mb MB\] \[-jobs n\] \[-threads n\] \[-stop ip|bd\]"
}

for {set i 0} {$i < [llength $argv]} {incr i} {
    set a [lindex $argv $i]
    switch -- $a {
        -n - -part - -out - -deploy - -buf_mb - -jobs - -threads - -stop {
            incr i
            if {$i >= [llength $argv]} { usage; error "brak wartosci dla $a" }
            set opt([string range $a 1 end]) [lindex $argv $i]
        }
        -h - -help { usage; return }
        default { usage; error "nieznana opcja: $a" }
    }
}
if {$opt(stop) ni {"" ip bd}} { error "-stop ip|bd, nie $opt(stop)" }
if {$opt(n) ni {8 16 32}} { error "-n 8|16|32 (tdata AXI DMA w pelnych bajtach), nie $opt(n)" }

# Watki Vivado na jeden proces; przy odmowie (limit wersji) 8.
proc set_threads {n} {
    if {[catch { set_param general.maxThreads $n } err]} {
        puts "uwaga: general.maxThreads $n odrzucone ($err), ustawiam 8"
        set_param general.maxThreads [set n 8]
    }
    return $n
}
set opt(threads) [set_threads $opt(threads)]

set n        $opt(n)
set name     posit${n}_vec
set bd_name  posit_vec_bd
set out      [file normalize $opt(out)]
set rtl      [file normalize [file join $script_dir .. hw gen p$n PositVecAxi.v]]
set ip_dir   [file join $out ip_p$n]
set proj_dir [file join $out proj_p$n]
set buf_size [expr {$opt(buf_mb) * 1048576}]

puts "build: $name, $opt(part), RTL $rtl, watki $opt(threads), przebiegi $opt(jobs), bufor $opt(buf_mb) MB"

# ---- 1. IP ------------------------------------------------------------------------
set vlnv [posit_ip::package $rtl $ip_dir $opt(part) PositVecAxi {s_axi s_axis m_axis}]
if {$opt(stop) eq "ip"} { puts "build: zatrzymane po IP ($ip_dir)"; return }

# ---- 2. projekt i block design --------------------------------------------------------
create_project -force $name $proj_dir -part $opt(part)
set_property ip_repo_paths [list $ip_dir] [current_project]
update_ip_catalog -rebuild

proc latest {vlnv_prefix} {
    set defs [lsort -dictionary [get_ipdefs -all "$vlnv_prefix:*"]]
    if {$defs eq ""} { error "brak IP $vlnv_prefix" }
    return [lindex $defs end]
}

create_bd_design $bd_name

set ps [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:zynq_ultra_ps_e] zynq_ultra_ps_e_0]
# Szerokosci portow PS siedza w rejestrach AFI, nie w bitstreamie: obie zostaja na
# domyslnych 128 b, a .dtsi (afi0) wpisuje ten sam stan. Smartconnecty zwezaja/poszerzaja.
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0                 1 \
    CONFIG.PSU__MAXIGP0__DATA_WIDTH            128 \
    CONFIG.PSU__USE__M_AXI_GP1                 0 \
    CONFIG.PSU__USE__M_AXI_GP2                 0 \
    CONFIG.PSU__USE__S_AXI_GP2                 1 \
    CONFIG.PSU__SAXIGP2__DATA_WIDTH            128 \
    CONFIG.PSU__FPGA_PL0_ENABLE                1 \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ 100 \
] $ps

set rst  [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:proc_sys_reset] rst_pl0]
set ctl  [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:smartconnect] smartconnect_ctl]
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI 2] $ctl
set data [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:smartconnect] smartconnect_data]
set_property -dict [list CONFIG.NUM_SI 2 CONFIG.NUM_MI 1] $data
set vec  [create_bd_cell -type ip -vlnv $vlnv posit_vec]
set dma  [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:axi_dma] posit_dma]
set_property -dict [list \
    CONFIG.c_include_sg              0 \
    CONFIG.c_sg_length_width         26 \
    CONFIG.c_addr_width              32 \
    CONFIG.c_m_axi_mm2s_data_width   64 \
    CONFIG.c_m_axis_mm2s_tdata_width [expr {2 * $n}] \
    CONFIG.c_m_axi_s2mm_data_width   64 \
    CONFIG.c_s_axis_s2mm_tdata_width $n \
    CONFIG.c_mm2s_burst_size         16 \
    CONFIG.c_s2mm_burst_size         16 \
] $dma

connect_bd_net [get_bd_pins $ps/pl_clk0] \
    [get_bd_pins $ps/maxihpm0_fpd_aclk] [get_bd_pins $ps/saxihp0_fpd_aclk] \
    [get_bd_pins $rst/slowest_sync_clk] [get_bd_pins $ctl/aclk] [get_bd_pins $data/aclk] \
    [get_bd_pins $vec/aclk] \
    [get_bd_pins $dma/s_axi_lite_aclk] [get_bd_pins $dma/m_axi_mm2s_aclk] [get_bd_pins $dma/m_axi_s2mm_aclk]
connect_bd_net [get_bd_pins $ps/pl_resetn0]            [get_bd_pins $rst/ext_reset_in]
connect_bd_net [get_bd_pins $rst/interconnect_aresetn] [get_bd_pins $ctl/aresetn] [get_bd_pins $data/aresetn]
connect_bd_net [get_bd_pins $rst/peripheral_aresetn]   [get_bd_pins $vec/aresetn] [get_bd_pins $dma/axi_resetn]

connect_bd_intf_net [get_bd_intf_pins $ps/M_AXI_HPM0_FPD]  [get_bd_intf_pins $ctl/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $ctl/M00_AXI]        [get_bd_intf_pins $vec/s_axi]
connect_bd_intf_net [get_bd_intf_pins $ctl/M01_AXI]        [get_bd_intf_pins $dma/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins $dma/M_AXIS_MM2S]    [get_bd_intf_pins $vec/s_axis]
connect_bd_intf_net [get_bd_intf_pins $vec/m_axis]         [get_bd_intf_pins $dma/S_AXIS_S2MM]
connect_bd_intf_net [get_bd_intf_pins $dma/M_AXI_MM2S]     [get_bd_intf_pins $data/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $dma/M_AXI_S2MM]     [get_bd_intf_pins $data/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins $data/M00_AXI]       [get_bd_intf_pins $ps/S_AXI_HP0_FPD]

assign_bd_address

proc to_bytes {range} {
    if {[regexp {^(\d+)([KMG]?)$} $range -> v u]} {
        return [expr {$v * ($u eq "K" ? 1024 : $u eq "M" ? 1048576 : $u eq "G" ? 1073741824 : 1)}]
    }
    return [expr {$range}]
}
# Okno rejestrow <cell> w przestrzeni PS: {adres rozmiar}
proc reg_window {ps cell} {
    set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces $ps/Data] -filter "NAME =~ *${cell}*"]
    if {[llength $seg] != 1} { error "segment adresowy $cell: [llength $seg] zamiast 1" }
    return [list [expr {[get_property OFFSET $seg]}] [to_bytes [get_property RANGE $seg]]]
}
lassign [reg_window $ps posit_vec] vec_base vec_size
lassign [reg_window $ps posit_dma] dma_base dma_size
set pl0_hz 100000000
catch { set pl0_hz [expr {round([get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $ps] * 1e6)}] }
puts "build: posit_vec @ [format 0x%llx $vec_base] ($vec_size B), posit_dma @ [format 0x%llx $dma_base] ($dma_size B), pl_clk0 $pl0_hz Hz"

validate_bd_design
save_bd_design

if {$opt(stop) eq "bd"} { puts "build: zatrzymane po block designie ($proj_dir)"; return }

# ---- 3. wrapper, ograniczenia, bitstream -------------------------------------------------
set bd_file [get_files $bd_name.bd]
generate_target all $bd_file
add_files -norecurse [make_wrapper -files $bd_file -top]
set_property top ${bd_name}_wrapper [current_fileset]

set xdc [file join $proj_dir bitstream.xdc]
set fh [open $xdc w]
puts $fh "set_property BITSTREAM.GENERAL.COMPRESS TRUE \[current_design\]"
close $fh
add_files -fileset constrs_1 -norecurse $xdc

# launch_runs nie dziedziczy set_param z sesji: watki w tcl.pre przebiegow
set threads_tcl [file join $proj_dir threads.tcl]
set fh [open $threads_tcl w]
puts $fh "set_param general.maxThreads $opt(threads)"
close $fh
set_property STEPS.SYNTH_DESIGN.TCL.PRE $threads_tcl [get_runs synth_1]
set_property STEPS.OPT_DESIGN.TCL.PRE   $threads_tcl [get_runs impl_1]

launch_runs impl_1 -to_step write_bitstream -jobs $opt(jobs)
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    error "impl_1 nie skonczony: [get_property STATUS [get_runs impl_1]] (log: $proj_dir/$name.runs/impl_1/runme.log)"
}

open_run impl_1
set wns [get_property SLACK [get_timing_paths -delay_type max -max_paths 1 -nworst 1]]
report_timing_summary -file [file join $out timing_p$n.rpt]
report_utilization    -file [file join $out utilization_p$n.rpt]
set util [report_utilization -return_string]
proc util_field {rpt row} {
    if {[regexp [format {(?n)^\|\s*%s\s*\|\s*([0-9.]+)} $row] $rpt -> v]} { return $v }
    return "?"
}
puts "build: WNS $wns ns"
if {$wns eq "" || $wns < 0} { error "timing nie domkniety (WNS = $wns ns); raport: $out/timing_p$n.rpt" }

# ---- 4. aplikacja dla xmutil -----------------------------------------------------------
set bit [file join [get_property DIRECTORY [get_runs impl_1]] ${bd_name}_wrapper.bit]
if {![file exists $bit]} { error "brak $bit" }
file copy -force $bit [file join $out $name.bit]
set hwh [lindex [glob -nocomplain \
    [file join $proj_dir $name.gen sources_1 bd $bd_name hw_handoff $bd_name.hwh] \
    [file join $proj_dir $name.srcs sources_1 bd $bd_name hw_handoff $bd_name.hwh]] 0]
if {$hwh ne ""} { file copy -force $hwh [file join $out $name.hwh] }

set app [file join $opt(deploy) $name]
file delete -force $app
file mkdir $app

set bootgen [auto_execok bootgen]
if {$bootgen eq ""} { error "brak bootgen w PATH (jest w <Vivado>/bin); .bit: [file join $out $name.bit]" }
file copy -force $bit [file join $app $name.bit]
set fh [open [file join $app $name.bif] w]
puts $fh "all:\n\{\n    \[destination_device = pl\] $name.bit\n\}"
close $fh
set here [pwd]
cd $app
set rc [catch { exec -ignorestderr {*}$bootgen -arch zynqmp -image $name.bif -process_bitstream bin -w } msg]
cd $here
file delete [file join $app $name.bit] [file join $app $name.bif]
if {$rc || ![file exists [file join $app $name.bit.bin]]} { error "bootgen: $msg" }

proc cells64 {v} { return [format "0x%x 0x%x" [expr {($v >> 32) & 0xffffffff}] [expr {$v & 0xffffffff}]] }
set fh [open [file join $app $name.dtsi] w]
puts $fh "/*
 * $name: PositVecAxi + AXI DMA na KV260, wygenerowane przez hw/top-modules/vec/vivado/build.tcl
 * ([clock format [clock seconds] -format {%Y-%m-%d %H:%M}], Vivado [version -short]).
 * Na plytce: dtc -@ -O dtb -o $name.dtbo $name.dtsi  (robi to sw/scripts/install_overlay.sh)
 * Bufor DMA: modul u-dma-buf (sw/scripts/build_udmabuf.sh) -> /dev/udmabuf-posit.
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
\t};

\tposit_vec: posit_vec@[format %llx $vec_base] {
\t\tcompatible = \"generic-uio\";
\t\treg = <[cells64 $vec_base] [cells64 $vec_size]>;
\t\tclocks = <&clocking0>;
\t};

\tposit_dma: posit_dma@[format %llx $dma_base] {
\t\tcompatible = \"generic-uio\";
\t\treg = <[cells64 $dma_base] [cells64 $dma_size]>;
\t\tclocks = <&clocking0>;
\t};

\t/* ciagly bufor pod AXI DMA (adresy 32 b): /dev/udmabuf-posit, adres w /sys/class/u-dma-buf/udmabuf-posit/phys_addr */
\tposit_buf: posit_buf {
\t\tcompatible = \"ikwzm,u-dma-buf\";
\t\tdevice-name = \"udmabuf-posit\";
\t\tsize = <[format 0x%x $buf_size]>;
\t};
};"
close $fh

set fh [open [file join $app shell.json] w]
puts $fh "\{\n    \"shell_type\" : \"XRT_FLAT\",\n    \"num_slots\": \"1\"\n\}"
close $fh

set fh [open [file join $app $name.json] w]
puts $fh "\{
    \"overlay\": \"$name\",
    \"ip\": \"posit_vec\",
    \"base\": [expr {wide($vec_base)}],
    \"size\": $vec_size,
    \"dma\": \"posit_dma\",
    \"dma_base\": [expr {wide($dma_base)}],
    \"dma_size\": $dma_size,
    \"udmabuf\": \"udmabuf-posit\",
    \"buf_size\": $buf_size,
    \"dma_max_bytes\": [expr {(1 << 26) - 1}],
    \"pl_clk0_hz\": $pl0_hz,
    \"n\": $n,
    \"vivado\": \"[version -short]\",
    \"part\": \"$opt(part)\",
    \"built\": \"[clock format [clock seconds] -format {%Y-%m-%d %H:%M}]\",
    \"wns_ns\": $wns,
    \"lut\": \"[util_field $util {CLB LUTs\*?}]\",
    \"ff\": \"[util_field $util {CLB Registers}]\",
    \"dsp\": \"[util_field $util {DSPs}]\",
    \"bram\": \"[util_field $util {Block RAM Tile}]\"
\}"
close $fh
puts "build: gotowe -> $app ([join [lsort [glob -tails -directory $app *]] {, }])"
puts "build: na plytce: sw/scripts/install_overlay.sh $name"
