#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  "") temperature_c=60 ;;
  *[!0-9.+-]*) echo "temperature must be numeric" >&2; exit 2 ;;
  *) temperature_c=$1 ;;
esac
case "${2:-}" in
  "") control_v=0.3 ;;
  *[!0-9.+-]*) echo "control voltage must be numeric" >&2; exit 2 ;;
  *) control_v=$2 ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
run_dir="$script_dir/scratch/patched-t${temperature_c}-v${control_v}"
mkdir -p "$run_dir"
cp "$script_dir/.spiceinit" "$run_dir/.spiceinit"
sed \
  -e "s/^\.param ctrl_v=.*/.param ctrl_v=${control_v}/" \
  -e "s/^\.temp .*/.temp ${temperature_c}/" \
  "$script_dir/repro.spice" > "$run_dir/repro.spice"

timeout 600 docker run --rm \
  -v "$script_dir:/source:ro" \
  -v "$run_dir:/work" \
  localhost/sandboxy-local-simra:latest \
  bash -lc '
    set -euo pipefail
    model_root=$(mktemp -d)
    mkdir -p "$model_root/ihp-sg13g2/libs.tech/ngspice"
    cp -a /foss/pdks/ihp-sg13g2/libs.tech/ngspice/models \
      "$model_root/ihp-sg13g2/libs.tech/ngspice/models"
    patch --quiet -d "$model_root" -p1 < /source/ihp-sg13g2-dsubw-tlevc.patch
    cd /work
    ln -sfn "$model_root/ihp-sg13g2/libs.tech/ngspice/models"/* .
    ngspice -b repro.spice
  ' 2>&1 | tee "$run_dir/ngspice.log"
