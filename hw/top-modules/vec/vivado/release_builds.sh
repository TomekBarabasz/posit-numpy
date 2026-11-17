#!/usr/bin/env bash
# =====================================================================
#  Bitstreamy do pakietow (extras positnp): jedna konfiguracja na zastosowanie.
#
#    ./release_builds.sh list                  konfiguracje i ich stan
#    ./release_builds.sh print [nazwa ...]     komendy sbt + vivado (bez uruchamiania)
#    ./release_builds.sh run   [nazwa ...]     dla kazdej po kolei: sbt (RTL) i vivado (bitstream)
#    ./release_builds.sh gen   <nazwa>         tylko RTL   (np. sbt w kontenerze SpinalHDL)
#    ./release_builds.sh build <nazwa>         tylko Vivado (RTL z ostatniego "gen" tej nazwy!)
#
#  Bez nazw: wszystkie. RTL laduje we wspolnym hw/top-modules/vec/hw/gen/p<n>/, wiec konfiguracje
#  ida po kolei (gen i build tej samej nazwy razem). Projekt Vivado: vivado/out/<nazwa>/,
#  log: vivado/out/<nazwa>/build.log, wynik: sw/positnp/overlays/<nazwa>/ (WNS < 0: nic).
#  Zmienne: SBT (domyslnie "sbt"), VIVADO ("vivado"), VIVADO_ARGS (np. "-jobs 4 -threads 8").
#  Konfiguracje: release_configs.txt. Windows (bez basha): release_builds.tcl.
# =====================================================================
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"            # hw/top-modules/vec/vivado
hw="$(cd "$here/../../.." && pwd)"                # hw/
SBT=${SBT:-sbt}
VIVADO=${VIVADO:-vivado}
VIVADO_ARGS=${VIVADO_ARGS:-}

# konfiguracje: release_configs.txt (wspólne z release_builds.tcl)
mapfile -t CONFIGS < <(grep -v '^[[:space:]]*\(#\|$\)' "$here/release_configs.txt")

field() { echo "$1" | cut -d'|' -f"$2"; }

find_cfg() {
    for c in "${CONFIGS[@]}"; do [[ "$(field "$c" 1)" == "$1" ]] && { echo "$c"; return 0; }; done
    echo "nieznana konfiguracja: $1 (./release_builds.sh list)" >&2
    return 1
}

names() { if (($#)); then printf '%s\n' "$@"; else for c in "${CONFIGS[@]}"; do field "$c" 1; done; fi; }

sbt_cmd()    { echo "cd $hw && $SBT \"top-vec/runMain posit.top.vec.VecKv260 $(field "$1" 3)\""; }
vivado_cmd() {
    local n; n=$(field "$1" 1)
    echo "cd $here && mkdir -p out/$n && $VIVADO -mode batch -nojournal -log out/$n/build.log -source build.tcl" \
         "-tclargs -engines \"$(field "$1" 4)\" -name $n -buf_mb $(field "$1" 5) -out out/$n $VIVADO_ARGS"
}

gen()   { local c; c=$(find_cfg "$1") || return 1; echo ">> gen $1";   bash -c "$(sbt_cmd "$c")" < /dev/null; }
build() { local c; c=$(find_cfg "$1") || return 1; echo ">> build $1"; bash -c "$(vivado_cmd "$c")"; }

cmd=${1:-list}; shift || true
case "$cmd" in
    list)
        printf '%-20s %-16s %-9s %-7s %s\n' nazwa extra silniki buf_MB stan
        for c in "${CONFIGS[@]}"; do
            printf '%-20s %-16s %-9s %-7s %s\n' "$(field "$c" 1)" "$(field "$c" 2)" "$(field "$c" 4)" \
                   "$(field "$c" 5)" "$(field "$c" 6)"
        done ;;
    print)
        for n in $(names "$@"); do c=$(find_cfg "$n") || exit 1; echo "# $n"; sbt_cmd "$c"; vivado_cmd "$c"; echo; done ;;
    gen)   [[ $# -eq 1 ]] || { echo "gen <nazwa>" >&2; exit 2; }; gen "$1" ;;
    build) [[ $# -eq 1 ]] || { echo "build <nazwa>" >&2; exit 2; }; build "$1" ;;
    run)
        declare -A result
        for n in $(names "$@"); do
            find_cfg "$n" > /dev/null || exit 1
            t0=$SECONDS
            if ! gen "$n"; then result[$n]="blad sbt"
            elif ! build "$n"; then result[$n]="blad vivado (out/$n/build.log)"
            else result[$n]="ok, $(( (SECONDS - t0) / 60 )) min -> sw/positnp/overlays/$n"
            fi
        done
        echo; echo "podsumowanie:"
        for n in $(names "$@"); do printf '  %-20s %s\n' "$n" "${result[$n]}"; done ;;
    *) sed -n '2,17p' "$0"; exit 2 ;;
esac
