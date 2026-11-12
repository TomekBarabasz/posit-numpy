# =====================================================================
#  Etap 1: overlay PYNQ z ALU posit za AXI4-Lite na KV260.
#
#    RTL -> IP (package_ip.tcl) -> block design -> bitstream -> .bit + .hwh
#
#  Block design:
#    zynq_ultra_ps_e_0  M_AXI_HPM0_FPD (32 b), pl_clk0 = 100 MHz, pl_resetn0
#    rst_pl0            proc_sys_reset na pl_clk0
#    smartconnect_0     1 SI -> 1 MI (AXI4 z PS -> AXI4-Lite)
#    posit_alu          PositAluAxiLite; nazwa instancji = atrybut overlayu w PYNQ
#
#  Uruchomienie z tego katalogu, po:  sbt "top-alu/runMain posit.top.alu.AluKv260 <n>"
#    vivado -mode batch -nojournal -log build.log -source build.tcl
#    vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -n 8
#    vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -stop bd   (potem GUI: out/proj_p32)
#
#  Opcje (-tclargs):
#    -n <bity>        format (rtl z ../hw/gen/p<n>/)              (32)
#    -part <part>                                                 (xck26-sfvc784-2LV-c)
#    -board           ustaw board part KV260 i preset PS z plikow plytki (wymaga board files)
#    -out <dir>       katalog roboczy                             (./out)
#    -deploy <dir>    gdzie skopiowac .bit + .hwh                 (../../../../sw/positnp/overlays)
#    -jobs <n>        rownolegle zadania synth/impl               (4)
#    -stop ip|bd      zatrzymaj po spakowaniu IP albo po block designie
#
#  Wynik: <deploy>/posit<n>_alu.{bit,hwh,txt}; .txt: wersja Vivado, WNS, zasoby.
#  Timing nie domkniety (WNS < 0) -> blad, nic nie jest kopiowane.
# =====================================================================

set ::posit_build 1
set script_dir [file normalize [file dirname [info script]]]
source [file join $script_dir package_ip.tcl]

array set opt [list \
    n      32 \
    part   xck26-sfvc784-2LV-c \
    board  0 \
    out    [file join $script_dir out] \
    deploy [file normalize [file join $script_dir .. .. .. .. sw positnp overlays]] \
    jobs   4 \
    stop   "" \
]

proc usage {} {
    puts "uzycie: vivado -mode batch -source build.tcl -tclargs \[-n bity\] \[-part part\] \[-board\] \[-out dir\] \[-deploy dir\] \[-jobs n\] \[-stop ip|bd\]"
}

for {set i 0} {$i < [llength $argv]} {incr i} {
    set a [lindex $argv $i]
    switch -- $a {
        -board { set opt(board) 1 }
        -n - -part - -out - -deploy - -jobs - -stop {
            incr i
            if {$i >= [llength $argv]} { usage; error "brak wartosci dla $a" }
            set opt([string range $a 1 end]) [lindex $argv $i]
        }
        -h - -help { usage; return }
        default { usage; error "nieznana opcja: $a" }
    }
}
if {$opt(stop) ni {"" ip bd}} { error "-stop ip|bd, nie $opt(stop)" }

set n        $opt(n)
set name     posit${n}_alu
set bd_name  posit_alu_bd
set out      [file normalize $opt(out)]
set rtl      [file normalize [file join $script_dir .. hw gen p$n PositAluAxiLite.v]]
set ip_dir   [file join $out ip_p$n]
set proj_dir [file join $out proj_p$n]

puts "build: $name, $opt(part), RTL $rtl"

# ---- 1. IP ------------------------------------------------------------------------
set vlnv [posit_ip::package $rtl $ip_dir $opt(part)]
if {$opt(stop) eq "ip"} { puts "build: zatrzymane po IP ($ip_dir)"; return }

# ---- 2. projekt i block design --------------------------------------------------------
create_project -force $name $proj_dir -part $opt(part)
if {$opt(board)} {
    set bp [lindex [lsort -dictionary [get_board_parts -quiet *kv260_som*]] end]
    if {$bp eq ""} { error "-board: brak board files KV260 (Vivado: Tools > Vivado Store > Boards)" }
    set_property board_part $bp [current_project]
    puts "build: board part $bp"
}
set_property ip_repo_paths [list $ip_dir] [current_project]
update_ip_catalog -rebuild

# najnowsza wersja IP Xilinxa dostepna w tej instalacji
proc latest {vlnv_prefix} {
    set defs [lsort -dictionary [get_ipdefs -all "$vlnv_prefix:*"]]
    if {$defs eq ""} { error "brak IP $vlnv_prefix" }
    return [lindex $defs end]
}

create_bd_design $bd_name

set ps [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:zynq_ultra_ps_e] zynq_ultra_ps_e_0]
if {$opt(board)} {
    apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} $ps
}
# Tylko to, czego potrzebuje PL: jeden master HPM0 FPD (32 b), zegar i reset PL.
# Konfiguracja DDR / MIO nie trafia do bitstreamu PL; PYNQ bierze z .hwh zegary i adresy.
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0                 1 \
    CONFIG.PSU__MAXIGP0__DATA_WIDTH            32 \
    CONFIG.PSU__USE__M_AXI_GP1                 0 \
    CONFIG.PSU__USE__M_AXI_GP2                 0 \
    CONFIG.PSU__FPGA_PL0_ENABLE                1 \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ 100 \
] $ps

set rst [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:proc_sys_reset] rst_pl0]
set ic  [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:smartconnect] smartconnect_0]
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI 1] $ic
set alu [create_bd_cell -type ip -vlnv $vlnv posit_alu]

connect_bd_net [get_bd_pins $ps/pl_clk0] \
    [get_bd_pins $ps/maxihpm0_fpd_aclk] [get_bd_pins $rst/slowest_sync_clk] \
    [get_bd_pins $ic/aclk] [get_bd_pins $alu/aclk]
connect_bd_net [get_bd_pins $ps/pl_resetn0]          [get_bd_pins $rst/ext_reset_in]
connect_bd_net [get_bd_pins $rst/interconnect_aresetn] [get_bd_pins $ic/aresetn]
connect_bd_net [get_bd_pins $rst/peripheral_aresetn]   [get_bd_pins $alu/aresetn]
connect_bd_intf_net [get_bd_intf_pins $ps/M_AXI_HPM0_FPD] [get_bd_intf_pins $ic/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $ic/M00_AXI]        [get_bd_intf_pins $alu/s_axi]

assign_bd_address
catch {
    foreach seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces $ps/Data]] {
        puts "build: [get_property NAME $seg] @ [get_property OFFSET $seg], [get_property RANGE $seg] B"
    }
}
validate_bd_design
save_bd_design

if {$opt(stop) eq "bd"} { puts "build: zatrzymane po block designie ($proj_dir)"; return }

# ---- 3. wrapper, ograniczenia, bitstream -------------------------------------------------
set bd_file [get_files $bd_name.bd]
generate_target all $bd_file
add_files -norecurse [make_wrapper -files $bd_file -top]
set_property top ${bd_name}_wrapper [current_fileset]

# kompresja bitstreamu: kilkukrotnie mniejszy plik dla prawie pustego PL
set xdc [file join $proj_dir bitstream.xdc]
set fh [open $xdc w]
puts $fh "set_property BITSTREAM.GENERAL.COMPRESS TRUE \[current_design\]"
close $fh
add_files -fileset constrs_1 -norecurse $xdc

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

# ---- 4. .bit + .hwh -----------------------------------------------------------------------
set bit [file join [get_property DIRECTORY [get_runs impl_1]] ${bd_name}_wrapper.bit]
set hwh [lindex [glob -nocomplain \
    [file join $proj_dir $name.gen sources_1 bd $bd_name hw_handoff $bd_name.hwh] \
    [file join $proj_dir $name.srcs sources_1 bd $bd_name hw_handoff $bd_name.hwh]] 0]
if {![file exists $bit]} { error "brak $bit" }
if {$hwh eq ""}          { error "brak .hwh w $proj_dir" }

file mkdir $opt(deploy)
file copy -force $bit [file join $opt(deploy) $name.bit]
file copy -force $hwh [file join $opt(deploy) $name.hwh]
set fh [open [file join $opt(deploy) $name.txt] w]
puts $fh "overlay   $name"
puts $fh "vivado    [version -short]"
puts $fh "part      $opt(part)"
puts $fh "built     [clock format [clock seconds] -format {%Y-%m-%d %H:%M}]"
puts $fh "wns_ns    $wns"
puts $fh "lut       [util_field $util {CLB LUTs\*?}]"
puts $fh "ff        [util_field $util {CLB Registers}]"
puts $fh "dsp       [util_field $util {DSPs}]"
close $fh
puts "build: gotowe -> [file join $opt(deploy) $name].{bit,hwh,txt}"
