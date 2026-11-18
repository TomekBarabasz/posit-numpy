# =====================================================================
#  Wszystkie bitstreamy do pakietow po kolei - Windows i Linux, z tego katalogu:
#
#    vivado -mode batch -nojournal -nolog -source release_builds.tcl
#    vivado -mode batch -nojournal -nolog -source release_builds.tcl -tclargs posit16_8_ml posit32_16_linalg
#
#  Wczesniej (raz, z hw/, np. w kontenerze SpinalHDL): sbt "top-vec/runMain posit.top.vec.VecReleases"
#  -> ../hw/gen/release/<nazwa>/ (RTL + release.txt). Buduje kazdy katalog stamtad (albo tylko podane
#  nazwy) build.tcl w osobnym procesie Vivado, jeden po drugim; blad jednego nie zatrzymuje reszty.
#
#  Opcje (-tclargs):
#    -out <dir>       projekty Vivado: <dir>/<nazwa>/proj_<nazwa>, log <dir>/<nazwa>/build.log   (out)
#                     Windows: przy bledach dlugosci sciezek krotka, np. -out C:/vb (bez spacji)
#    -jobs <n>        -threads <n>     przekazywane do build.tcl                      (domyslne build.tcl)
#    -dry             tylko wypisz komendy
#    <nazwa> ...      tylko te konfiguracje
#
#  Wynik: ../../../../sw/positnp/overlays/<nazwa>/ (jak build.tcl), podsumowanie na koncu i w
#  <out>/release_summary.txt.
# =====================================================================

set here [file normalize [file dirname [info script]]]
cd $here

array set opt {out out jobs "" threads "" dry 0}
set names {}
for {set i 0} {$i < [llength $argv]} {incr i} {
    set a [lindex $argv $i]
    switch -- $a {
        -out - -jobs - -threads { incr i; set opt([string range $a 1 end]) [lindex $argv $i] }
        -dry { set opt(dry) 1 }
        default {
            if {[string index $a 0] eq "-"} { error "nieznana opcja $a (-out, -jobs, -threads, -dry, nazwy)" }
            lappend names $a
        }
    }
}

# konfiguracje z VecReleases: ../hw/gen/release/<nazwa>/release.txt ("klucz wartosc" w liniach)
set rel_root [file join .. hw gen release]
proc read_release {path} {
    set d [dict create]
    set fh [open $path r]
    foreach line [split [read $fh] \n] {
        if {[regexp {^(\S+)\s+(.*)$} [string trim $line] -> k v]} { dict set d $k $v }
    }
    close $fh
    return $d
}
set found {}
foreach f [lsort [glob -nocomplain -directory $rel_root */release.txt]] {
    set d [read_release $f]
    dict set d rtl [file dirname $f]
    lappend found $d
}
if {![llength $found]} {
    error "brak konfiguracji w [file normalize $rel_root] - najpierw z hw/: sbt \"top-vec/runMain posit.top.vec.VecReleases\""
}
set todo {}
if {[llength $names]} {
    foreach n $names {
        set hit [lsearch -inline -exact -index 1 [lmap d $found {list x [dict get $d name] $d}] $n]
        if {$hit eq ""} { error "$n: brak RTL w $rel_root (VecReleases $n)" }
        lappend todo [lindex $hit 2]
    }
} else {
    set todo $found
}

# Vivado do procesow potomnych: ten sam, ktory wykonuje ten skrypt (vivado.bat na Windows)
proc vivado_exe {} {
    if {[info exists ::env(XILINX_VIVADO)]} {
        foreach f {vivado.bat vivado} {
            set p [file join $::env(XILINX_VIVADO) bin $f]
            if {[file exists $p]} { return $p }
        }
    }
    set p [auto_execok vivado]
    if {$p eq ""} { error "nie znaleziono vivado (XILINX_VIVADO ani PATH)" }
    return $p
}
set vivado [vivado_exe]

# pierwszy konkretny blad z build.log: linia przed "while executing" (error z build.tcl) albo "ERROR:"
proc log_error {log} {
    if {![file exists $log]} { return "" }
    set fh [open $log r]
    set lines [split [read $fh] \n]
    close $fh
    set i [lsearch -exact $lines "    while executing"]
    if {$i > 0} { return [string trim [lindex $lines $i-1]] }
    set e [lsearch -inline -regexp $lines {^ERROR:}]
    return [string trim $e]
}

set deploy_root [file normalize [file join $here .. .. .. .. sw positnp overlays]]
set results {}
set t_all [clock seconds]
puts "release_builds: [llength $todo] konfiguracji: [join [lmap d $todo {dict get $d name}] {, }]"
puts "vivado: $vivado\n"

foreach d $todo {
    set name [dict get $d name]
    set dir  [file join $opt(out) $name]
    # tylko sciezki wzgledne i argumenty bez spacji, przecinkow, srednikow i "=": vivado.bat przechodzi
    # przez cmd.exe, ktory po nich dzieli argumenty (32,16,8 -> "32" "16" "8"); silniki jako 32_16_8
    set cmd [list -mode batch -nojournal -log $dir/build.log -source build.tcl -tclargs \
                 -engines [string map {, _} [dict get $d engines]] -name $name -buf_mb [dict get $d buf_mb] \
                 -rtl [dict get $d rtl] -out $dir]
    foreach o {jobs threads} { if {$opt($o) ne ""} { lappend cmd -$o $opt($o) } }
    puts "== $name ([dict get $d extra]): [dict get $d args]"
    puts "   vivado [join $cmd]"
    if {$opt(dry)} { continue }

    file mkdir $dir
    set bit [file join $deploy_root $name $name.bit.bin]
    set t0 [clock seconds]
    set rc [catch { exec {*}$vivado {*}$cmd >@stdout 2>@stderr } err]
    set min [expr {([clock seconds] - $t0) / 60}]
    if {!$rc && [file exists $bit] && [file mtime $bit] >= $t0} {
        set r "ok, $min min -> sw/positnp/overlays/$name"
    } elseif {!$rc} {
        set r "brak nowego $name.bit.bin ($min min; $dir/build.log)"
    } else {
        set why [log_error $dir/build.log]
        if {$why eq ""} { set why [lindex [split $err \n] 0] }
        set r "blad po $min min: $why ($dir/build.log)"
    }
    lappend results [list $name $r]
    puts "== $name: $r\n"
}

if {!$opt(dry)} {
    set lines [list "release_builds [clock format $t_all -format {%Y-%m-%d %H:%M}], razem [expr {([clock seconds] - $t_all) / 60}] min"]
    foreach r $results { lappend lines [format "  %-20s %s" {*}$r] }
    puts "\n[join $lines \n]"
    file mkdir $opt(out)
    set fh [open [file join $opt(out) release_summary.txt] w]
    puts $fh [join $lines \n]
    close $fh
}
