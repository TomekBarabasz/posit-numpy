# =====================================================================
#  Aplikacja PL dla xmutil z jednym albo kilkoma silnikami na tablicach
#  (PositVecAxi, po jednym na format posit) i AXI DMA na KV260. Bez PYNQ:
#  Ubuntu + xmutil, rejestry przez /dev/uioN, bufory przez u-dma-buf.
#
#    RTL -> IP (../../common/vivado/package_ip.tcl, jedno na silnik) -> block design -> bitstream
#        -> bootgen: .bit.bin, + .dtsi (region FPGA, zegar PL0, AFI, na silnik: UIO x2 + u-dma-buf), shell.json
#
#  Block design (pl_clk0 = 100 MHz dla wszystkiego), dla kazdego silnika n:
#    zynq_ultra_ps_e_0  M_AXI_HPM0_FPD (128 b) -> sterowanie, S_AXI_HP0_FPD (128 b) <- dane z/do DDR
#    rst_pl0            proc_sys_reset
#    smartconnect_ctl   HPM0 -> posit_vec_p<n> (AXI4-Lite) i posit_dma_p<n>/S_AXI_LITE
#    posit_dma_p<n>     AXI DMA, tryb prosty (bez SG), dlugosc transferu 26 b (64 MB - 1)
#                       MM2S: DDR -> posit_vec_p<n>/s_axis ({b, a}, 2n b), S2MM: m_axis (n b) -> DDR
#    smartconnect_data  M_AXI_MM2S + M_AXI_S2MM wszystkich DMA -> HP0
#    posit_vec_p<n>     PositVecAxi_p<n>; nazwy komorek = nazwy wezlow UIO w .dtsi
#  Silniki sa niezalezne (wlasne DMA i bufory): Python moze uzywac kilku naraz.
#
#  Uruchomienie z tego katalogu, po:  sbt "top-vec/runMain posit.top.vec.VecKv260 <n> [<n> ...]"
#    vivado -mode batch -nojournal -log build.log -source build.tcl                       posit32_mem
#    vivado -mode batch -nojournal -log build.log -source build.tcl -tclargs -engines "32 16 8"
#                                                                                         posit32_16_8_mem
#    ... -tclargs -io dma -engines "32 16 8"  (RTL z VecKv260 --dma)                       posit32_16_8_vec
#  Opcje (-tclargs):
#    -engines "<n> ..."  silniki: 8, 16, 32, kazdy co najwyzej raz (rtl z ../hw/gen/p<n>/)   ("32")
#    -io master|dma      etap 6: wlasny master AXI4 silnika (m_axi -> HP0, przerwanie -> pl_ps_irq0)
#                        albo AXI4-Stream + AXI DMA (etapy 2-5; RTL z VecKv260 --dma)       (master)
#    -n <bity>           to samo co -engines <bity> (jeden silnik)
#    -name <nazwa>       nazwa aplikacji                  (posit<n>[_<n>...]_mem, z -io dma: _vec)
#    -part <part>                                                       (xck26-sfvc784-2LV-c)
#    -out <dir>          katalog roboczy                                (./out)
#    -deploy <dir>       gdzie zapisac katalog aplikacji                (../../../../sw/positnp/overlays)
#    -buf_mb <MB>        bufor DMA (u-dma-buf) na silnik; suma musi sie zmiescic w CMA   (64)
#    -jobs <n>           rownolegle przebiegi                           (8)
#    -threads <n>        watki Vivado na przebieg (general.maxThreads)  (8)
#    -stop ip|bd         zatrzymaj po spakowaniu IP albo po block designie
#
#  Wynik: <deploy>/<nazwa>/ (.bit.bin, .dtsi, shell.json, .json z lista silnikow); na plytce
#  sw/scripts/install_overlay.sh <nazwa>. WNS < 0 -> blad, nic nie jest kopiowane.
# =====================================================================

set ::posit_build 1
set script_dir [file normalize [file dirname [info script]]]
source [file join $script_dir .. .. common vivado package_ip.tcl]
source [file join $script_dir overlay_files.tcl]

array set opt [list \
    engines 32 \
    io      master \
    name    "" \
    part    xck26-sfvc784-2LV-c \
    out     [file join $script_dir out] \
    deploy  [file normalize [file join $script_dir .. .. .. .. sw positnp overlays]] \
    buf_mb  64 \
    jobs    8 \
    threads 8 \
    stop    "" \
]

proc usage {} {
    puts "uzycie: vivado -mode batch -source build.tcl -tclargs \[-engines \"32 16 8\"\] \[-name nazwa\] \[-part part\] \[-out dir\] \[-deploy dir\] \[-buf_mb MB\] \[-jobs n\] \[-threads n\] \[-stop ip|bd\]"
}

for {set i 0} {$i < [llength $argv]} {incr i} {
    set a [lindex $argv $i]
    switch -- $a {
        -n - -engines - -io - -name - -part - -out - -deploy - -buf_mb - -jobs - -threads - -stop {
            incr i
            if {$i >= [llength $argv]} { usage; error "brak wartosci dla $a" }
            set opt([expr {$a eq "-n" ? "engines" : [string range $a 1 end]}]) [lindex $argv $i]
        }
        -h - -help { usage; return }
        default { usage; error "nieznana opcja: $a" }
    }
}
if {$opt(stop) ni {"" ip bd}} { error "-stop ip|bd, nie $opt(stop)" }
set ns [string map {, " "} $opt(engines)]
if {![llength $ns]} { error "-engines: co najmniej jeden silnik" }
foreach n $ns {
    if {$n ni {8 16 32}} { error "-engines: 8|16|32 (tdata AXI DMA w pelnych bajtach), nie $n" }
}
if {[llength [lsort -unique $ns]] != [llength $ns]} { error "-engines $ns: kazdy format co najwyzej raz" }
if {$opt(io) ni {master dma}} { error "-io master|dma, nie $opt(io)" }
set master [expr {$opt(io) eq "master"}]
if {$master && [llength $ns] > 8} { error "-io master: najwyzej 8 silnikow (pl_ps_irq0 ma 8 linii)" }

# Watki Vivado na jeden proces; przy odmowie (limit wersji) 8.
proc set_threads {n} {
    if {[catch { set_param general.maxThreads $n } err]} {
        puts "uwaga: general.maxThreads $n odrzucone ($err), ustawiam 8"
        set_param general.maxThreads [set n 8]
    }
    return $n
}
set opt(threads) [set_threads $opt(threads)]

set name     [expr {$opt(name) ne "" ? $opt(name) : [posit_overlay::app_name $ns $opt(io)]}]
set bd_name  posit_vec_bd
set out      [file normalize $opt(out)]
set proj_dir [file join $out proj_$name]
set buf_size [expr {$opt(buf_mb) * 1048576}]

puts "build: $name, silniki $ns, $opt(part), watki $opt(threads), przebiegi $opt(jobs), bufor $opt(buf_mb) MB na silnik"

# ---- 1. IP: jedno na silnik (PositVecAxi_p<n>, moduly z przedrostkiem p<n>_) ---------------
set vlnv    [dict create]
set ip_dirs {}
foreach n $ns {
    set rtl    [file normalize [file join $script_dir .. hw gen p$n PositVecAxi_p$n.v]]
    set ip_dir [file join $out ip_p$n]
    set busifs [expr {$master ? {s_axi m_axi} : {s_axi s_axis m_axis}}]
    dict set vlnv $n [posit_ip::package $rtl $ip_dir $opt(part) PositVecAxi_p$n $busifs]
    lappend ip_dirs $ip_dir
}
if {$opt(stop) eq "ip"} { puts "build: zatrzymane po IP ($ip_dirs)"; return }

# ---- 2. projekt i block design --------------------------------------------------------
create_project -force $name $proj_dir -part $opt(part)
set_property ip_repo_paths $ip_dirs [current_project]
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
    CONFIG.PSU__USE__IRQ0                      $master \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ 100 \
] $ps

set k    [llength $ns]
set rst  [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:proc_sys_reset] rst_pl0]
set ctl  [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:smartconnect] smartconnect_ctl]
set per  [expr {$master ? 1 : 2}]                 ;# porty smartconnectow na silnik
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI [expr {$per * $k}]] $ctl
set data [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:smartconnect] smartconnect_data]
set_property -dict [list CONFIG.NUM_SI [expr {$per * $k}] CONFIG.NUM_MI 1] $data

connect_bd_net [get_bd_pins $ps/pl_clk0] \
    [get_bd_pins $ps/maxihpm0_fpd_aclk] [get_bd_pins $ps/saxihp0_fpd_aclk] \
    [get_bd_pins $rst/slowest_sync_clk] [get_bd_pins $ctl/aclk] [get_bd_pins $data/aclk]
connect_bd_net [get_bd_pins $ps/pl_resetn0]            [get_bd_pins $rst/ext_reset_in]
connect_bd_net [get_bd_pins $rst/interconnect_aresetn] [get_bd_pins $ctl/aresetn] [get_bd_pins $data/aresetn]
connect_bd_intf_net [get_bd_intf_pins $ps/M_AXI_HPM0_FPD]  [get_bd_intf_pins $ctl/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $data/M00_AXI]       [get_bd_intf_pins $ps/S_AXI_HP0_FPD]

set engines {}
set irqs {}
set port 0
foreach n $ns {
    set e   [posit_overlay::engine $n]
    set vec [create_bd_cell -type ip -vlnv [dict get $vlnv $n] [dict get $e vec]]
    connect_bd_net [get_bd_pins $ps/pl_clk0] [get_bd_pins $vec/aclk]
    connect_bd_net [get_bd_pins $rst/peripheral_aresetn] [get_bd_pins $vec/aresetn]
    if {$master} {
        # etap 6: silnik sam czyta i zapisuje DDR przez HP0; przerwanie do PS
        dict unset e dma
        dict set e irq [expr {89 + $port}]          ;# pl_ps_irq0[port] = GIC SPI 89 + port
        connect_bd_intf_net [get_bd_intf_pins $ctl/[format M%02d_AXI $port]]  [get_bd_intf_pins $vec/s_axi]
        connect_bd_intf_net [get_bd_intf_pins $vec/m_axi] [get_bd_intf_pins $data/[format S%02d_AXI $port]]
        lappend irqs [get_bd_pins $vec/irq]
    } else {
        set dma [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:axi_dma] [dict get $e dma]]
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
            [get_bd_pins $dma/s_axi_lite_aclk] [get_bd_pins $dma/m_axi_mm2s_aclk] [get_bd_pins $dma/m_axi_s2mm_aclk]
        connect_bd_net [get_bd_pins $rst/peripheral_aresetn] [get_bd_pins $dma/axi_resetn]
        set m0 [format M%02d_AXI [expr {2 * $port}]]
        set m1 [format M%02d_AXI [expr {2 * $port + 1}]]
        set s0 [format S%02d_AXI [expr {2 * $port}]]
        set s1 [format S%02d_AXI [expr {2 * $port + 1}]]
        connect_bd_intf_net [get_bd_intf_pins $ctl/$m0]          [get_bd_intf_pins $vec/s_axi]
        connect_bd_intf_net [get_bd_intf_pins $ctl/$m1]          [get_bd_intf_pins $dma/S_AXI_LITE]
        connect_bd_intf_net [get_bd_intf_pins $dma/M_AXIS_MM2S]  [get_bd_intf_pins $vec/s_axis]
        connect_bd_intf_net [get_bd_intf_pins $vec/m_axis]       [get_bd_intf_pins $dma/S_AXIS_S2MM]
        connect_bd_intf_net [get_bd_intf_pins $dma/M_AXI_MM2S]   [get_bd_intf_pins $data/$s0]
        connect_bd_intf_net [get_bd_intf_pins $dma/M_AXI_S2MM]   [get_bd_intf_pins $data/$s1]
    }
    lappend engines $e
    incr port
}
if {$master} {
    if {[llength $irqs] == 1} {
        connect_bd_net [lindex $irqs 0] [get_bd_pins $ps/pl_ps_irq0]
    } else {
        set cat [create_bd_cell -type ip -vlnv [latest xilinx.com:ip:xlconcat] irq_concat]
        set_property CONFIG.NUM_PORTS [llength $irqs] $cat
        for {set i 0} {$i < [llength $irqs]} {incr i} { connect_bd_net [lindex $irqs $i] [get_bd_pins $cat/In$i] }
        connect_bd_net [get_bd_pins $cat/dout] [get_bd_pins $ps/pl_ps_irq0]
    }
}

assign_bd_address

proc to_bytes {range} {
    if {[regexp {^(\d+)([KMG]?)$} $range -> v u]} {
        return [expr {$v * ($u eq "K" ? 1024 : $u eq "M" ? 1048576 : $u eq "G" ? 1073741824 : 1)}]
    }
    return [expr {$range}]
}
# Okno rejestrow <cell> w przestrzeni PS: {adres rozmiar}. Segment SEG_<cell>_<nazwa>;
# nazwy komorek nie sa swoimi przedrostkami (posit_vec_p8 / _p16 / _p32), wiec wzorzec jest jednoznaczny.
proc reg_window {ps cell} {
    set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces $ps/Data] -filter "NAME =~ *${cell}_*"]
    if {[llength $seg] != 1} { error "segment adresowy $cell: [llength $seg] zamiast 1" }
    return [list [expr {[get_property OFFSET $seg]}] [to_bytes [get_property RANGE $seg]]]
}
set pl0_hz 100000000
catch { set pl0_hz [expr {round([get_property CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $ps] * 1e6)}] }
set placed {}
foreach e $engines {
    lassign [reg_window $ps [dict get $e vec]] vec_base vec_size
    dict set e vec_base $vec_base; dict set e vec_size $vec_size
    set what "[dict get $e vec] @ [format 0x%llx $vec_base] ($vec_size B)"
    if {[dict exists $e dma]} {
        lassign [reg_window $ps [dict get $e dma]] dma_base dma_size
        dict set e dma_base $dma_base; dict set e dma_size $dma_size
        append what ", [dict get $e dma] @ [format 0x%llx $dma_base] ($dma_size B)"
    } else {
        append what ", master AXI4, przerwanie SPI [dict get $e irq]"
    }
    dict set e buf_size $buf_size
    lappend placed $e
    puts "build: $what"
}
set engines $placed
puts "build: pl_clk0 $pl0_hz Hz"

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
report_timing_summary -file [file join $out timing_$name.rpt]
report_utilization    -file [file join $out utilization_$name.rpt]
set util [report_utilization -return_string]
proc util_field {rpt row} {
    if {[regexp [format {(?n)^\|\s*%s\s*\|\s*([0-9.]+)} $row] $rpt -> v]} { return $v }
    return "?"
}
puts "build: WNS $wns ns"
if {$wns eq "" || $wns < 0} { error "timing nie domkniety (WNS = $wns ns); raport: $out/timing_$name.rpt" }

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

set stamp "([clock format [clock seconds] -format {%Y-%m-%d %H:%M}], Vivado [version -short])"
posit_overlay::write_dtsi [file join $app $name.dtsi] $name $engines $pl0_hz $stamp

set fh [open [file join $app shell.json] w]
puts $fh "\{\n    \"shell_type\" : \"XRT_FLAT\",\n    \"num_slots\": \"1\"\n\}"
close $fh

set q posit_overlay::json_str
posit_overlay::write_json [file join $app $name.json] $name $engines $pl0_hz [expr {(1 << 26) - 1}] [list \
    vivado [$q [version -short]] \
    part   [$q $opt(part)] \
    built  [$q [clock format [clock seconds] -format {%Y-%m-%d %H:%M}]] \
    wns_ns $wns \
    lut    [$q [util_field $util {CLB LUTs\*?}]] \
    ff     [$q [util_field $util {CLB Registers}]] \
    dsp    [$q [util_field $util {DSPs}]] \
    bram   [$q [util_field $util {Block RAM Tile}]] \
]
puts "build: gotowe -> $app ([join [lsort [glob -tails -directory $app *]] {, }])"
puts "build: na plytce: sw/scripts/install_overlay.sh $name"
