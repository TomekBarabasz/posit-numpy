# =====================================================================
#  Etap 0: synteza poza kontekstem (OOC) jednostek posit-core na K26.
#
#  Wejscie: RTL z OocUnits (sbt "top-ooc/runMain posit.top.ooc.OocUnits"),
#  ../hw/gen/<wariant>/<modul>.v, wariant = comb | op | full | div.
#  Wyjscie (-out, domyslnie ./out):
#    ooc_results.csv           wiersz na jednostke i etap (synth, impl)
#    ooc_results.md            ta sama tabela do wklejenia w README
#    <wariant>/<modul>/*.rpt   report_utilization i report_timing_summary
#
#  Uruchomienie z tego katalogu:
#    vivado -mode batch -nojournal -log vivado.log -source ooc_synth.tcl
#    vivado -mode batch -nojournal -log vivado.log -source ooc_synth.tcl -tclargs -impl -only "op/*"
#
#  Opcje (-tclargs):
#    -gen <dir>       katalog z RTL                (../hw/gen)
#    -out <dir>       katalog wynikow              (./out)
#    -part <part>     uklad                        (xck26-sfvc784-2LV-c)
#    -period <ns>     okres zegara                 (10.0 = 100 MHz)
#    -only <glob>     filtr "<wariant>/<modul>"    (*), np. "*/PositMul_32_2" albo "div/*"
#    -impl            po syntezie opt/place/route i drugi wiersz z wynikiem po routingu
#    -threads <n>     watki Vivado (general.maxThreads)          (8)
#
#  Ograniczenia czasowe (kazda jednostka tak samo):
#    - zegar clk o okresie -period na porcie clk albo aclk (IP z AXI);
#      jednostki kombinacyjne (comb) nie maja portu zegara, wtedy zegar
#      jest wirtualny,
#    - set_input_delay / set_output_delay 0 wzgledem clk na wszystkich
#      portach: sciezki port -> rejestr, rejestr -> port i port -> port maja
#      caly okres, jakby jednostke otaczaly rejestry. Pomija to clk-to-q i
#      setup tych rejestrow, wiec Fmax wariantu comb jest lekko optymistyczne.
#  Fmax = 1000 / (okres - WNS). Po samej syntezie opoznienia polaczen sa
#  szacowane; wiazace liczby daje -impl.
# =====================================================================

set script_dir [file normalize [file dirname [info script]]]

array set opt [list \
    gen    [file normalize [file join $script_dir .. hw gen]] \
    out    [file join $script_dir out] \
    part   xck26-sfvc784-2LV-c \
    period 10.0 \
    only   * \
    impl   0 \
    threads 8 \
]

proc usage {} {
    puts "uzycie: vivado -mode batch -source ooc_synth.tcl -tclargs \[-gen dir\] \[-out dir\] \[-part part\] \[-period ns\] \[-only glob\] \[-impl\] \[-threads n\]"
}

for {set i 0} {$i < [llength $argv]} {incr i} {
    set a [lindex $argv $i]
    switch -- $a {
        -impl { set opt(impl) 1 }
        -gen - -out - -part - -period - -only - -threads {
            incr i
            if {$i >= [llength $argv]} { usage; error "brak wartosci dla $a" }
            set opt([string range $a 1 end]) [lindex $argv $i]
        }
        -h - -help { usage; return }
        default { usage; error "nieznana opcja: $a" }
    }
}
if {![string is double -strict $opt(period)] || $opt(period) <= 0} { error "-period: liczba ns > 0, nie $opt(period)" }

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
set opt(gen) [file normalize $opt(gen)]
set opt(out) [file normalize $opt(out)]

# ---- lista jednostek ----------------------------------------------------------

proc find_units {gen only} {
    set units {}
    foreach dir [lsort [glob -nocomplain -type d -directory $gen *]] {
        set variant [file tail $dir]
        foreach f [lsort [glob -nocomplain -type f -directory $dir *.v]] {
            set module [file rootname [file tail $f]]
            if {[string match $only "$variant/$module"]} {
                lappend units [list $variant $module $f]
            }
        }
    }
    return $units
}

set units [find_units $opt(gen) $opt(only)]
if {[llength $units] == 0} {
    error "brak jednostek w $opt(gen) dla -only \"$opt(only)\"; najpierw z hw/: sbt \"top-ooc/runMain posit.top.ooc.OocUnits\""
}

# ---- pomocnicze ---------------------------------------------------------------

# Ograniczenia jednostki. clk_port: port zegara ("clk" jednostek, "aclk" IP z AXI)
# albo "" - wtedy zegar wirtualny.
proc write_xdc {path clk_port period} {
    set p [format %.3f $period]
    set fh [open $path w]
    if {$clk_port ne ""} {
        puts $fh "create_clock -period $p -name clk \[get_ports $clk_port\]"
    } else {
        puts $fh "create_clock -period $p -name clk"
    }
    puts $fh "set_input_delay  -clock clk 0.000 \[get_ports -filter {DIRECTION == IN && NAME != \"$clk_port\"}\]"
    puts $fh "set_output_delay -clock clk 0.000 \[all_outputs\]"
    close $fh
}

# Pierwsza liczba w wierszu tabeli report_utilization, np. "| CLB LUTs* | 347 | ...".
proc util_field {rpt name} {
    set re [format {(?n)^\|\s*%s\s*\|\s*([0-9.]+)} $name]
    if {[regexp $re $rpt -> v]} { return $v }
    return ""
}

# Liczba komorek o danym REF_NAME - zapas, gdy wiersza nie ma w raporcie.
proc count_refs {pattern} {
    return [llength [get_cells -quiet -hierarchical -filter "REF_NAME =~ $pattern"]]
}

proc metrics {period} {
    set rpt [report_utilization -return_string]
    set m [dict create]
    foreach {key row ref} {
        lut   {CLB LUTs\*?}     LUT*
        ff    {CLB Registers}   FD*
        carry {CARRY8}          CARRY8
        dsp   {DSPs}            DSP48E2
        bram  {Block RAM Tile}  RAMB*
    } {
        set v [util_field $rpt $row]
        if {$v eq ""} { set v [count_refs $ref] }
        dict set m $key $v
    }

    set wns ""
    set path [get_timing_paths -quiet -delay_type max -max_paths 1 -nworst 1]
    if {[llength $path]} { set wns [get_property SLACK $path] }
    if {[string is double -strict $wns]} {
        dict set m wns  [format %.3f $wns]
        dict set m fmax [format %.1f [expr {1000.0 / ($period - $wns)}]]
        dict set m met  [expr {$wns >= 0 ? "tak" : "nie"}]
    } else {
        dict set m wns  ""
        dict set m fmax ""
        dict set m met  "brak sciezek"
    }
    return $m
}

# ---- wyniki -------------------------------------------------------------------

file mkdir $opt(out)
set csv_path [file join $opt(out) ooc_results.csv]
set md_path  [file join $opt(out) ooc_results.md]
set cols {variant module n es stage lut ff carry8 dsp bram period_ns wns_ns fmax_mhz met seconds}

set csv [open $csv_path w]
puts $csv [join $cols ,]
flush $csv
set rows {}

proc add_row {values} {
    global csv rows
    puts $csv [join $values ,]
    flush $csv
    lappend rows $values
}

# ---- przebieg -----------------------------------------------------------------

puts "ooc: [llength $units] jednostek, $opt(part), [format %.3f $opt(period)] ns, impl=$opt(impl)"
puts "ooc: RTL $opt(gen), watki [set_threads $opt(threads)]"
puts "ooc: wyniki $opt(out)"

set idx 0
foreach u $units {
    lassign $u variant module src
    incr idx
    puts "ooc: \[$idx/[llength $units]\] $variant/$module"

    set n ""; set es ""
    regexp {_(\d+)_(\d+)$} $module -> n es

    set dir [file join $opt(out) $variant $module]
    file mkdir $dir

    set fh [open $src r]; set text [read $fh]; close $fh
    set clk_port ""
    regexp {input\s+wire\s+(a?clk)\y} $text -> clk_port
    set xdc [file join $dir ooc.xdc]
    write_xdc $xdc $clk_port $opt(period)

    set t0 [clock seconds]
    set stage synth
    if {[catch {
        create_project -in_memory -part $opt(part)
        read_verilog $src
        read_xdc $xdc
        synth_design -top $module -part $opt(part) -mode out_of_context

        report_utilization    -file [file join $dir utilization_synth.rpt]
        report_timing_summary -file [file join $dir timing_synth.rpt]
        set m [metrics $opt(period)]
        add_row [list $variant $module $n $es synth \
            [dict get $m lut] [dict get $m ff] [dict get $m carry] [dict get $m dsp] [dict get $m bram] \
            $opt(period) [dict get $m wns] [dict get $m fmax] [dict get $m met] [expr {[clock seconds] - $t0}]]

        if {$opt(impl)} {
            set stage impl
            set t1 [clock seconds]
            opt_design
            place_design
            route_design
            report_utilization    -file [file join $dir utilization_impl.rpt]
            report_timing_summary -file [file join $dir timing_impl.rpt]
            set m [metrics $opt(period)]
            add_row [list $variant $module $n $es impl \
                [dict get $m lut] [dict get $m ff] [dict get $m carry] [dict get $m dsp] [dict get $m bram] \
                $opt(period) [dict get $m wns] [dict get $m fmax] [dict get $m met] [expr {[clock seconds] - $t1}]]
        }
    } err]} {
        puts "ooc: BLAD $variant/$module ($stage): $err"
        add_row [list $variant $module $n $es $stage "" "" "" "" "" $opt(period) "" "" "BLAD" [expr {[clock seconds] - $t0}]]
    }
    catch { close_project }
}

close $csv

# ---- tabela markdown ----------------------------------------------------------

set md [open $md_path w]
puts $md "Synteza OOC, $opt(part), okres [format %.3f $opt(period)] ns ([format %.0f [expr {1000.0 / $opt(period)}]] MHz)."
puts $md "Vivado [version -short], [clock format [clock seconds] -format {%Y-%m-%d}]. Fmax = 1000 / (okres - WNS).\n"
puts $md "| wariant | modul | etap | LUT | FF | CARRY8 | DSP | BRAM | WNS ns | Fmax MHz | timing |"
puts $md "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
foreach r $rows {
    lassign $r variant module n es stage lut ff carry dsp bram period wns fmax met
    puts $md "| $variant | $module | $stage | $lut | $ff | $carry | $dsp | $bram | $wns | $fmax | $met |"
}
close $md

puts "ooc: gotowe, [llength $rows] wierszy -> $csv_path, $md_path"
