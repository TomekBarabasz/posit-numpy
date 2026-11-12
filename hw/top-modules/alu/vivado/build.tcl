# =====================================================================
#  Etap 1: aplikacja PL dla xmutil (dfx-mgr) z ALU posit za AXI4-Lite na KV260.
#  Bez PYNQ: na plytce Ubuntu + xmutil, rejestry przez /dev/uioN.
#
#    RTL -> IP (package_ip.tcl) -> block design -> bitstream
#        -> bootgen: .bit.bin, + .dtsi (region FPGA, zegar PL0, wezel UIO), shell.json
#
#  Block design:
#    zynq_ultra_ps_e_0  M_AXI_HPM0_FPD (128 b, domyslna szerokosc - patrz nizej), pl_clk0 = 100 MHz, pl_resetn0
#    rst_pl0            proc_sys_reset na pl_clk0
#    smartconnect_0     1 SI -> 1 MI (AXI4 128 b z PS -> AXI4-Lite 32 b)
#    posit_alu          PositAluAxiLite; nazwa wezla UIO w .dtsi (/sys/class/uio/uio*/name)
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
#    -deploy <dir>    gdzie zapisac katalog aplikacji             (../../../../sw/positnp/overlays)
#    -jobs <n>        rownolegle przebiegi (synteza IP z block designu) (8)
#    -threads <n>     watki Vivado na przebieg (general.maxThreads)    (8)
#    -stop ip|bd      zatrzymaj po spakowaniu IP albo po block designie
#
#  Wynik: katalog aplikacji <deploy>/posit<n>_alu/, kopiowany na plytce do
#  /lib/firmware/xilinx/ przez sw/scripts/install_overlay.sh:
#    posit<n>_alu.bit.bin   bitstream dla fpga_manager (bootgen -process_bitstream bin)
#    posit<n>_alu.dtsi      overlay device tree; dtc -> .dtbo na plytce
#    shell.json             typ aplikacji dla dfx-mgr (XRT_FLAT, caly PL)
#    posit<n>_alu.json      adres i rozmiar IP, zegar, wersja Vivado, WNS, zasoby
#  W out/ zostaja tez .bit i .hwh (do podgladu, plytka ich nie potrzebuje).
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
    jobs   8 \
    threads 8 \
    stop   "" \
]

proc usage {} {
    puts "uzycie: vivado -mode batch -source build.tcl -tclargs \[-n bity\] \[-part part\] \[-board\] \[-out dir\] \[-deploy dir\] \[-jobs n\] \[-threads n\] \[-stop ip|bd\]"
}

for {set i 0} {$i < [llength $argv]} {incr i} {
    set a [lindex $argv $i]
    switch -- $a {
        -board { set opt(board) 1 }
        -n - -part - -out - -deploy - -jobs - -threads - -stop {
            incr i
            if {$i >= [llength $argv]} { usage; error "brak wartosci dla $a" }
            set opt([string range $a 1 end]) [lindex $argv $i]
        }
        -h - -help { usage; return }
        default { usage; error "nieznana opcja: $a" }
    }
}
if {$opt(stop) ni {"" ip bd}} { error "-stop ip|bd, nie $opt(stop)" }

# Watki Vivado na jeden proces (place_design, route_design, synteza). Domyslnie
# Vivado bierze 2 na Windows i 8 na Linuksie; gorna granica zalezy od wersji,
# wiec przy odmowie schodzimy do 8.
proc set_threads {n} {
    if {[catch { set_param general.maxThreads $n } err]} {
        puts "uwaga: general.maxThreads $n odrzucone ($err), ustawiam 8"
        set_param general.maxThreads [set n 8]
    }
    return $n
}
set opt(threads) [set_threads $opt(threads)]

set n        $opt(n)
set name     posit${n}_alu
set bd_name  posit_alu_bd
set out      [file normalize $opt(out)]
set rtl      [file normalize [file join $script_dir .. hw gen p$n PositAluAxiLite.v]]
set ip_dir   [file join $out ip_p$n]
set proj_dir [file join $out proj_p$n]

puts "build: $name, $opt(part), RTL $rtl, watki $opt(threads), przebiegi $opt(jobs)"

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
# Tylko to, czego potrzebuje PL: jeden master HPM0 FPD, zegar i reset PL.
# Konfiguracja DDR / MIO nie trafia do bitstreamu PL.
# HPM0 FPD zostaje na 128 b: szerokosc portu siedzi w rejestrach PS (AFI), nie
# w bitstreamie, a firmware Kria ustawia 128 b. Ten sam stan wpisuje .dtsi
# (afi0, config-afi <14 0xa00>); smartconnect zweza 128 b do AXI4-Lite 32 b.
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0                 1 \
    CONFIG.PSU__MAXIGP0__DATA_WIDTH            128 \
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

# Adres i rozmiar okna ALU w przestrzeni PS (do .dtsi i .json).
proc to_bytes {range} {
    if {[regexp {^(\d+)([KMG]?)$} $range -> v u]} {
        return [expr {$v * ($u eq "K" ? 1024 : $u eq "M" ? 1048576 : $u eq "G" ? 1073741824 : 1)}]
    }
    return [expr {$range}]
}
set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces $ps/Data] -filter {NAME =~ *posit_alu*}]
if {[llength $seg] != 1} { error "segment adresowy posit_alu: [llength $seg] zamiast 1 ([get_bd_addr_segs -of_objects [get_bd_addr_spaces $ps/Data]])" }
set alu_base [expr {[get_property OFFSET $seg]}]
set alu_size [to_bytes [get_property RANGE $seg]]
# Zadana czestotliwosc pl_clk0, nie "faktyczna" z Vivado (ACT_FREQMHZ, np. 99.999001 MHz):
# ta jest policzona dla PLL z konfiguracji PS w block designie, a na plytce PL0 taktuje
# PLL z firmware Kria (1500 MHz). Sterownik zegara wybiera dzielnik nie przekraczajacy
# zadanej wartosci: 99 999 001 Hz -> 1500/16 = 93.75 MHz, 100 000 000 Hz -> 1500/15 = 100 MHz.
set pl0_hz 100000000
catch { set pl0_hz [expr {round([get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $ps] * 1e6)}] }
puts "build: posit_alu @ [format 0x%llx $alu_base], $alu_size B; pl_clk0 $pl0_hz Hz"
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

# launch_runs startuje osobne procesy Vivado, ktore nie dziedzicza set_param
# z tej sesji: watki ustawia w nich skrypt tcl.pre pierwszego kroku przebiegu.
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
set hwh [lindex [glob -nocomplain \
    [file join $proj_dir $name.gen sources_1 bd $bd_name hw_handoff $bd_name.hwh] \
    [file join $proj_dir $name.srcs sources_1 bd $bd_name hw_handoff $bd_name.hwh]] 0]
if {![file exists $bit]} { error "brak $bit" }
file copy -force $bit [file join $out $name.bit]
if {$hwh ne ""} { file copy -force $hwh [file join $out $name.hwh] }

set app [file join $opt(deploy) $name]
file delete -force $app
file mkdir $app

# .bit -> .bit.bin: bootgen z instalacji Vivado (bin/bootgen, na Windows bootgen.bat)
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

# overlay device tree: region FPGA z bitstreamem, zegar PL0, szerokosc HPM0, wezel UIO
proc cells64 {v} { return [format "0x%x 0x%x" [expr {($v >> 32) & 0xffffffff}] [expr {$v & 0xffffffff}]] }
set fh [open [file join $app $name.dtsi] w]
puts $fh "/*
 * $name: PositAluAxiLite na KV260, wygenerowane przez hw/top-modules/alu/vivado/build.tcl
 * ([clock format [clock seconds] -format {%Y-%m-%d %H:%M}], Vivado [version -short]).
 * Na plytce: dtc -@ -O dtb -o $name.dtbo $name.dtsi  (robi to sw/scripts/install_overlay.sh)
 */
/dts-v1/;
/plugin/;

&fpga_full {
	#address-cells = <2>;
	#size-cells = <2>;
	firmware-name = \"$name.bit.bin\";
	resets = <&zynqmp_reset 116>;	/* pl_resetn0 */
};

&amba {
	#address-cells = <2>;
	#size-cells = <2>;

	/* HPM0/HPM1 FPD: 128 b (bity 9:8 i 11:10 rejestru fs = 2) - jak PSU__MAXIGP0__DATA_WIDTH w block designie */
	afi0: afi0 {
		compatible = \"xlnx,afi-fpga\";
		config-afi = <14 0xa00>;
	};

	clocking0: clocking0 {
		compatible = \"xlnx,fclk\";
		#clock-cells = <0>;
		clocks = <&zynqmp_clk 71>;	/* PL0_REF */
		clock-output-names = \"fabric_clk\";
		assigned-clocks = <&zynqmp_clk 71>;
		assigned-clock-rates = <$pl0_hz>;
	};

	posit_alu: posit_alu@[format %llx $alu_base] {
		compatible = \"generic-uio\";
		reg = <[cells64 $alu_base] [cells64 $alu_size]>;
		clocks = <&clocking0>;
	};
};"
close $fh

set fh [open [file join $app shell.json] w]
puts $fh "\{\n    \"shell_type\" : \"XRT_FLAT\",\n    \"num_slots\": \"1\"\n\}"
close $fh

set fh [open [file join $app $name.json] w]
puts $fh "\{
    \"overlay\": \"$name\",
    \"ip\": \"posit_alu\",
    \"base\": [expr {wide($alu_base)}],
    \"size\": $alu_size,
    \"pl_clk0_hz\": $pl0_hz,
    \"n\": $n,
    \"vivado\": \"[version -short]\",
    \"part\": \"$opt(part)\",
    \"built\": \"[clock format [clock seconds] -format {%Y-%m-%d %H:%M}]\",
    \"wns_ns\": $wns,
    \"lut\": \"[util_field $util {CLB LUTs\*?}]\",
    \"ff\": \"[util_field $util {CLB Registers}]\",
    \"dsp\": \"[util_field $util {DSPs}]\"
\}"
close $fh
puts "build: gotowe -> $app ([join [lsort [glob -tails -directory $app *]] {, }])"
puts "build: na plytce: sw/scripts/install_overlay.sh $name"
