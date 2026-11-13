# Sprawdzenie overlay_files.tcl bez Vivado: tclsh overlay_files_test.tcl
# Pisze .dtsi i .json dla 1 i 3 silnikow do katalogu tymczasowego, kompiluje .dtsi przez dtc
# (jak install_overlay.sh na plytce) i parsuje .json przez python3.
source [file join [file dirname [info script]] overlay_files.tcl]

set dir [file join [expr {[info exists env(TMPDIR)] ? $env(TMPDIR) : "/tmp"}] overlay_files_test_[pid]]
file mkdir $dir
set fails 0

foreach ns {{32} {32 16 8}} {
    set name [posit_overlay::app_name $ns]
    set engines {}
    set addr 0xa0000000
    foreach n $ns {
        set e [posit_overlay::engine $n]
        dict set e vec_base [expr {$addr}];           dict set e vec_size 4096
        dict set e dma_base [expr {$addr + 0x10000}]; dict set e dma_size 65536
        dict set e buf_size [expr {64 << 20}]
        lappend engines $e
        set addr [expr {$addr + 0x20000}]
    }
    set dtsi [file join $dir $name.dtsi]
    set json [file join $dir $name.json]
    posit_overlay::write_dtsi $dtsi $name $engines 100000000 "(test)"
    posit_overlay::write_json $json $name $engines 100000000 [expr {(1 << 26) - 1}] \
        [list vivado [posit_overlay::json_str 2026.1] wns_ns 1.244 lut [posit_overlay::json_str 6550]]
    if {[catch { exec dtc -@ -q -O dtb -o [file join $dir $name.dtbo] $dtsi } msg]} {
        puts "FAIL dtc $name: $msg"; incr fails
    } else { puts "ok   dtc  $name" }
    set py "import json,sys; d=json.load(open(sys.argv\[1\])); print(d\['overlay'\], \[e\['ip'\] for e in d\['engines'\]\], \[e\['udmabuf'\] for e in d\['engines'\]\])"
    if {[catch { exec python3 -c $py $json } msg]} {
        puts "FAIL json $name: $msg"; incr fails
    } else { puts "ok   json $msg" }
}
if {$fails} { puts "$fails bledow (pliki: $dir)"; exit 1 }
file delete -force $dir
puts "overlay_files: ok"
