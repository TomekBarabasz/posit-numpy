# =====================================================================
#  Bitstreamy do pakietow - to samo co release_builds.sh, bez basha (Windows; dziala tez na Linuksie).
#
#    vivado -mode batch -nojournal -nolog -source release_builds.tcl -tclargs posit32_16_8_mem
#    vivado -mode batch -nojournal -nolog -source release_builds.tcl -tclargs run           (wszystkie)
#    ... -tclargs list                  konfiguracje i ich stan (release_configs.txt)
#    ... -tclargs print [nazwa ...]     komendy sbt + vivado, bez uruchamiania
#    ... -tclargs run   [nazwa ...]     dla kazdej po kolei: sbt (RTL), potem vivado (bitstream)
#    ... -tclargs gen   <nazwa>         tylko RTL
#    ... -tclargs build <nazwa>         tylko Vivado (RTL z ostatniego "gen" tej nazwy!)
#  Same nazwy bez komendy = run. Opcje (przed albo po komendzie):
#    -nogen               bez sbt: RTL juz jest w hw/top-modules/vec/hw/gen/p<n>/ (np. z WSL albo kontenera)
#    -sbt "<komenda>"     zamiast sbt z PATH          -vivado "<komenda>"  zamiast vivado z PATH / XILINX_VIVADO
#    -vivado_args "..."   dodatkowe -tclargs build.tcl, np. "-jobs 4 -threads 8"
#
#  Kazda konfiguracja w osobnym procesie Vivado (wlasny log: out/<nazwa>/build.log, swieza pamiec).
#  RTL laduje we wspolnym hw/top-modules/vec/hw/gen/p<n>/, wiec gen i build tej samej nazwy ida razem.
#  Wynik: sw/positnp/overlays/<nazwa>/ (WNS < 0: nic). Dziala tez w zwyklym tclsh (list / print / run).
# =====================================================================

set here [file normalize [file dirname [info script]]]
set hw   [file normalize [file join $here .. .. ..]]

proc configs {} {
    set out {}
    set fh [open [file join $::here release_configs.txt] r]
    fconfigure $fh -encoding utf-8
    foreach line [split [read $fh] "\n"] {
        set line [string trim $line]
        if {$line eq "" || [string index $line 0] eq "#"} { continue }
        lassign [split $line |] name extra vargs engines buf state
        lappend out [dict create name $name extra $extra vargs $vargs engines $engines buf $buf state $state]
    }
    close $fh
    return $out
}

proc find_cfg {name} {
    foreach c [configs] { if {[dict get $c name] eq $name} { return $c } }
    error "nieznana konfiguracja: $name (release_builds.tcl -tclargs list)"
}

proc tool {name override {soft 0}} {
    if {$override ne ""} { return $override }
    set p [auto_execok $name]
    if {$p eq "" && $name eq "vivado" && [info exists ::env(XILINX_VIVADO)]} {
        foreach f [list vivado.bat vivado] {
            set q [file join $::env(XILINX_VIVADO) bin $f]
            if {[file exists $q]} { return [list $q] }
        }
    }
    if {$p eq "" && $soft} { return [list $name] }           ;# print: tylko pokazac komende
    if {$p eq ""} { error "nie ma $name w PATH (opcja -$name \"<komenda>\")" }
    return $p
}

proc sbt_cmd {c {soft 0}} {
    return [concat [tool sbt $::opt(sbt) $soft] [list "top-vec/runMain posit.top.vec.VecKv260 [dict get $c vargs]"]]
}

proc vivado_cmd {c {soft 0}} {
    set n [dict get $c name]
    return [concat [tool vivado $::opt(vivado) $soft] \
        [list -mode batch -nojournal -log [file join out $n build.log] -source build.tcl -tclargs \
              -engines [dict get $c engines] -name $n -buf_mb [dict get $c buf] -out [file join out $n] \
              -rtl [dict get $c vargs]] \
        $::opt(vivado_args)]
}

proc show {dir cmd} {
    set words {}
    foreach w $cmd { lappend words [expr {[string match "* *" $w] ? "\"$w\"" : $w}] }
    return "(w $dir) [join $words { }]"
}

proc run_in {dir cmd} {
    set old [pwd]
    cd $dir
    set rc [catch { exec {*}$cmd >@ stdout 2>@ stderr } msg]
    cd $old
    if {$rc} { puts stderr $msg }
    return [expr {$rc == 0}]
}

proc gen {name} {
    set c [find_cfg $name]
    puts ">> gen $name"
    set t0 [clock seconds]
    after 1100
    if {![run_in $::hw [sbt_cmd $c]]} { return 0 }
    foreach n [dict get $c engines] {                  ;# RTL kazdego silnika musi byc swiezo zapisany
        set v [file join $::here .. hw gen p$n PositVecAxi_p$n.v]
        if {![file exists $v] || [file mtime $v] < $t0} {
            puts stderr "gen $name: sbt nie zapisal hw/gen/p$n/PositVecAxi_p$n.v - Vivado zbudowaloby stary RTL"
            return 0
        }
    }
    return 1
}

proc build {name} {
    set c [find_cfg $name]
    puts ">> build $name"
    file mkdir [file join $::here out $name]
    return [run_in $::here [vivado_cmd $c]]
}

proc main {av} {
    global opt here hw
    # ---- argumenty ---------------------------------------------------------------------------
    array set ::opt {nogen 0 sbt "" vivado "" vivado_args ""}
    set cmd ""
    set names {}
    for {set i 0} {$i < [llength $av]} {incr i} {
        set a [lindex $av $i]
        switch -- $a {
            -nogen { set opt(nogen) 1 }
            -sbt - -vivado - -vivado_args {
                incr i
                set opt([string range $a 1 end]) [lindex $av $i]
            }
            list - print - run - gen - build {
                if {$cmd ne ""} { error "dwie komendy: $cmd i $a" }
                set cmd $a
            }
            default { lappend names $a }
        }
    }
    if {$cmd eq ""} { set cmd [expr {[llength $names] ? "run" : "list"}] }
    if {![llength $names] && $cmd in {print run}} {
        foreach c [configs] { lappend names [dict get $c name] }
    }
    foreach n $names { find_cfg $n }                       ;# nieznana nazwa: blad przed startem

    set ok 1
    switch -- $cmd {
        list {
            puts [format "%-20s %-16s %-9s %-7s %s" nazwa extra silniki buf_MB stan]
            foreach c [configs] {
                puts [format "%-20s %-16s %-9s %-7s %s" [dict get $c name] [dict get $c extra] \
                      [dict get $c engines] [dict get $c buf] [dict get $c state]]
            }
        }
        print {
            foreach n $names {
                set c [find_cfg $n]
                puts "# $n"
                if {!$opt(nogen)} { puts [show $hw [sbt_cmd $c 1]] }
                puts [show $here [vivado_cmd $c 1]]
                puts ""
            }
        }
        gen   { if {[llength $names] != 1} { error "gen <nazwa>" }; set ok [gen [lindex $names 0]] }
        build { if {[llength $names] != 1} { error "build <nazwa>" }; set ok [build [lindex $names 0]] }
        run {
            set results {}
            foreach n $names {
                set t0 [clock seconds]
                if {!$opt(nogen) && ![gen $n]} {
                    set r "blad sbt"
                } elseif {![build $n]} {
                    set r "blad vivado ([file join out $n build.log])"
                } else {
                    set r "ok, [expr {([clock seconds] - $t0) / 60}] min -> sw/positnp/overlays/$n"
                }
                if {![string match ok* $r]} { set ok 0 }
                lappend results $n $r
            }
            puts "\nrelease_builds [clock format [clock seconds] -format {%Y-%m-%d %H:%M}]"
            foreach {n r} $results { puts [format "  %-20s %s" $n $r] }
        }
    }
    if {!$ok} { exit 1 }
}

if {[catch {main [expr {[info exists argv] ? $argv : {}}]} msg]} {
    puts stderr "release_builds: $msg"
    exit 1
}
