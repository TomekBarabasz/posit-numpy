# =====================================================================
#  Pakowanie PositAluAxiLite jako IP Vivado (etap 1).
#  Uzywane przez build.tcl; samodzielnie:
#    vivado -mode batch -nojournal -log package_ip.log -source package_ip.tcl -tclargs <rtl.v> <ip_dir> [part]
#
#  IP: newhope.pl:posit:PositAluAxiLite:1.0
#  Interfejsy wnioskuje ipx::package_project z nazw portow (test
#  axi_port_names w PositAluAxiLiteTestplan pilnuje tych nazw):
#    s_axi_*  -> s_axi    (AXI4-Lite slave, mapa pamieci 4 KB)
#    aclk     -> zegar,   ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn
#    aresetn  -> reset,   POLARITY ACTIVE_LOW
#  Skrypt ustawia te parametry jawnie i sprawdza, ze interfejsy sa.
# =====================================================================

namespace eval posit_ip {
    variable vendor  newhope.pl
    variable library posit
    variable top     PositAluAxiLite
    variable version 1.0
}

proc posit_ip::vlnv {} {
    variable vendor; variable library; variable top; variable version
    return "$vendor:$library:$top:$version"
}

proc posit_ip::bus_param {busif name value} {
    set p [ipx::get_bus_parameters $name -of_objects $busif]
    if {$p eq ""} { set p [ipx::add_bus_parameter $name $busif] }
    set_property value $value $p
}

# Pakuje <rtl> do <ip_dir> (katalog czyszczony). Zwraca VLNV.
proc posit_ip::package {rtl ip_dir part} {
    variable vendor; variable library; variable top; variable version
    if {![file exists $rtl]} {
        error "brak $rtl; najpierw z hw/: sbt \"top-alu/runMain posit.top.alu.AluKv260 <n>\""
    }
    set rtl    [file normalize $rtl]
    set ip_dir [file normalize $ip_dir]
    file delete -force $ip_dir
    file mkdir $ip_dir

    # projekt tymczasowy tylko do pakowania
    set tmp [file join $ip_dir _pkg]
    create_project -force pkg $tmp -part $part
    add_files -norecurse $rtl
    set_property top $top [current_fileset]
    update_compile_order -fileset sources_1

    ipx::package_project -root_dir $ip_dir -vendor $vendor -library $library -taxonomy /UserIP \
        -import_files -set_current true
    set core [ipx::current_core]
    set_property version      $version $core
    set_property display_name "Posit ALU (AXI4-Lite)" $core
    set_property description  "posit-numpy stage 1: add/sub/mul/div on posits behind AXI4-Lite registers" $core

    foreach name {s_axi aclk aresetn} {
        if {[ipx::get_bus_interfaces $name -of_objects $core] eq ""} {
            error "IP packager nie rozpoznal interfejsu $name - sprawdz nazwy portow w $rtl"
        }
    }
    set clk [ipx::get_bus_interfaces aclk -of_objects $core]
    posit_ip::bus_param $clk ASSOCIATED_BUSIF s_axi
    posit_ip::bus_param $clk ASSOCIATED_RESET aresetn
    posit_ip::bus_param [ipx::get_bus_interfaces aresetn -of_objects $core] POLARITY ACTIVE_LOW

    if {[ipx::get_memory_maps -of_objects $core] eq ""} {
        error "IP bez mapy pamieci s_axi - PYNQ nie zobaczy rejestrow"
    }

    set_property core_revision [scan [clock format [clock seconds] -format %y%m%d%H] %d] $core
    ipx::create_xgui_files $core
    ipx::update_checksums $core
    ipx::check_integrity $core
    ipx::save_core $core
    close_project
    file delete -force $tmp
    puts "posit_ip: [posit_ip::vlnv] -> $ip_dir"
    return [posit_ip::vlnv]
}

# Uruchomienie samodzielne (nie przy source z build.tcl)
if {[info exists argv] && [file tail [info script]] eq "package_ip.tcl" && ![info exists ::posit_build]} {
    if {[llength $argv] < 2} {
        puts "uzycie: -tclargs <rtl.v> <ip_dir> \[part\]"
    } else {
        set part [expr {[llength $argv] > 2 ? [lindex $argv 2] : "xck26-sfvc784-2LV-c"}]
        posit_ip::package [lindex $argv 0] [lindex $argv 1] $part
    }
}
