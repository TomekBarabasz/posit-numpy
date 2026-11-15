# =====================================================================
#  Wentylator KV260: sygnal fan_en_b (pin A12, LVCMOS33) idzie z PS przez PL.
#
#  W fabrycznej aplikacji (k26-starter-kits) wyjscie TTC0 (EMIO, bit 2) jest
#  polaczone z tym pinem, a Linux steruje nim przez pwm-fan (ttc0) z device tree
#  bazowego. Bitstream, ktory tego nie robi, zostawia pin niesterowany i wentylator
#  kreci sie na maksa. Te procedury dokladaja polaczenie do block designu i XDC;
#  sterowanie (krzywa temperatura -> obroty) zostaje po stronie Linuksa jak w fabrycznej.
#
#    source kv260_fan.tcl
#    set_property -dict [kv260_fan::ps_config] $ps        ;# TTC0 + wyjscie fali na EMIO
#    kv260_fan::connect $ps                               ;# emio_ttc0_wave_o[2] -> port fan_en_b
#    kv260_fan::xdc $fh                                   ;# ograniczenia pinu A12
# =====================================================================

namespace eval kv260_fan {}

proc kv260_fan::ps_config {} {
    return [list \
        CONFIG.PSU__TTC0__PERIPHERAL__ENABLE 1 \
        CONFIG.PSU__TTC0__WAVEOUT__ENABLE    1 \
        CONFIG.PSU__TTC0__WAVEOUT__IO        EMIO \
    ]
}

# Wycinek bitu 2 z emio_ttc0_wave_o[2:0] na zewnetrzny port fan_en_b.
proc kv260_fan::connect {ps} {
    set port [create_bd_port -dir O -from 0 -to 0 fan_en_b]
    # xlslice (starsze Vivado) albo inline_hdl ilslice (nowsze); te same parametry
    set defs [lsort -dictionary [concat [get_ipdefs -all -quiet "xilinx.com:ip:xlslice:*"] \
                                        [get_ipdefs -all -quiet "xilinx.com:inline_hdl:ilslice:*"]]]
    if {$defs eq ""} { error "kv260_fan: brak IP xlslice / ilslice" }
    set slice [create_bd_cell -type ip -vlnv [lindex $defs end] fan_slice]
    set_property -dict [list CONFIG.DIN_WIDTH 3 CONFIG.DIN_FROM 2 CONFIG.DIN_TO 2] $slice
    catch { set_property CONFIG.DOUT_WIDTH 1 $slice }
    connect_bd_net [get_bd_pins $ps/emio_ttc0_wave_o] [get_bd_pins $slice/Din]
    connect_bd_net [get_bd_pins $slice/Dout] $port
}

proc kv260_fan::xdc {fh} {
    puts $fh "# wentylator KV260 (fan_en_b, PWM z TTC0 przez EMIO)"
    puts $fh "set_property PACKAGE_PIN A12 \[get_ports {fan_en_b}\]"
    puts $fh "set_property IOSTANDARD LVCMOS33 \[get_ports {fan_en_b}\]"
    puts $fh "set_property SLEW SLOW \[get_ports {fan_en_b}\]"
    puts $fh "set_property DRIVE 4 \[get_ports {fan_en_b}\]"
}
