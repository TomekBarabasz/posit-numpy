# =====================================================================
#  Pakowanie IP z posit-kv260 dla Vivado; wspolne dla top-modules/*/vivado/build.tcl.
#  Samodzielnie:
#    vivado -mode batch -nojournal -log package_ip.log -source package_ip.tcl -tclargs <rtl.v> <ip_dir> [part] [top] [busif...]
#
#  IP: newhope.pl:posit:<top>:1.0, np. PositAluAxiLite (etap 1), PositVecAxi (etap 2).
#  Interfejsy wnioskuje ipx::package_project z nazw portow (testy
#  *_port_names w testplanach posit-kv260 pilnuja tych nazw):
#    s_axi_*   -> s_axi    (AXI4-Lite slave, mapa pamieci 4 KB)
#    s_axis_*  -> s_axis   (AXI4-Stream slave),  m_axis_* -> m_axis (master)
#    aclk      -> zegar,   ASSOCIATED_BUSIF <wszystkie busif>, ASSOCIATED_RESET aresetn
#    aresetn   -> reset,   POLARITY ACTIVE_LOW
#  Skrypt ustawia te parametry jawnie i sprawdza, ze interfejsy sa.
# =====================================================================

namespace eval posit_ip {
    variable vendor  newhope.pl
    variable library posit
    variable version 1.0
}

proc posit_ip::vlnv {top} {
    variable vendor; variable library; variable version
    return "$vendor:$library:$top:$version"
}

proc posit_ip::bus_param {busif name value} {
    set p [ipx::get_bus_parameters $name -of_objects $busif]
    if {$p eq ""} { set p [ipx::add_bus_parameter $name $busif] }
    set_property value $value $p
}

# Pakuje <rtl> (top-level <top>, interfejsy magistral <busifs>) do <ip_dir>
# (katalog czyszczony). Zwraca VLNV.
proc posit_ip::package {rtl ip_dir part {top PositAluAxiLite} {busifs {s_axi}}} {
    variable vendor; variable library; variable version
    if {![file exists $rtl]} {
        error "brak $rtl; najpierw wygeneruj RTL z hw/ (sbt \"top-<id>/runMain ...\", README top-levelu)"
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
    set_property display_name "posit-numpy $top" $core
    set_property description  "posit-numpy: posit arithmetic engine ($top, interfaces: $busifs)" $core

    foreach name [concat $busifs {aclk aresetn}] {
        if {[ipx::get_bus_interfaces $name -of_objects $core] eq ""} {
            error "IP packager nie rozpoznal interfejsu $name - sprawdz nazwy portow w $rtl"
        }
    }
    set clk [ipx::get_bus_interfaces aclk -of_objects $core]
    posit_ip::bus_param $clk ASSOCIATED_BUSIF [join $busifs :]
    posit_ip::bus_param $clk ASSOCIATED_RESET aresetn
    posit_ip::bus_param [ipx::get_bus_interfaces aresetn -of_objects $core] POLARITY ACTIVE_LOW

    if {[ipx::get_memory_maps -of_objects $core] eq ""} {
        error "IP bez mapy pamieci s_axi - block design nie przydzieli adresu"
    }

    set_property core_revision [scan [clock format [clock seconds] -format %y%m%d%H] %d] $core
    ipx::create_xgui_files $core
    ipx::update_checksums $core
    ipx::check_integrity $core
    ipx::save_core $core
    close_project
    file delete -force $tmp
    puts "posit_ip: [posit_ip::vlnv $top] -> $ip_dir"
    return [posit_ip::vlnv $top]
}

# Uruchomienie samodzielne (nie przy source z build.tcl)
if {[info exists argv] && [file tail [info script]] eq "package_ip.tcl" && ![info exists ::posit_build]} {
    if {[llength $argv] < 2} {
        puts "uzycie: -tclargs <rtl.v> <ip_dir> \[part\] \[top\] \[busif...\]"
    } else {
        set part   [expr {[llength $argv] > 2 ? [lindex $argv 2] : "xck26-sfvc784-2LV-c"}]
        set top    [expr {[llength $argv] > 3 ? [lindex $argv 3] : "PositAluAxiLite"}]
        set busifs [expr {[llength $argv] > 4 ? [lrange $argv 4 end] : {s_axi}}]
        posit_ip::package [lindex $argv 0] [lindex $argv 1] $part $top $busifs
    }
}
