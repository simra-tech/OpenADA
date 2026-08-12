#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$script_dir/scratch"
evidence_dir=$(mktemp -d "$script_dir/scratch/cv.XXXXXX")

timeout 600 docker run --rm \
  -v "$script_dir:/source:ro" \
  -v "$evidence_dir:/work" \
  localhost/sandboxy-local-simra:latest \
  bash -lc '
    set -euo pipefail
    model_root=$(mktemp -d)
    mkdir -p "$model_root/original/ihp-sg13g2/libs.tech/ngspice"
    cp -a /foss/pdks/ihp-sg13g2/libs.tech/ngspice/models \
      "$model_root/original/ihp-sg13g2/libs.tech/ngspice/models"
    cp -a "$model_root/original" "$model_root/patched"
    patch --quiet -d "$model_root/patched" -p1 \
      < /source/ihp-sg13g2-dsubw-tlevc.patch

    gate_cases="minus3:-3 minus1:-1 zero:0 point3:0.3 plus1:1 plus3:3"
    for variant in original patched; do
      model_dir="$model_root/$variant/ihp-sg13g2/libs.tech/ngspice/models"
      for gate_case in $gate_cases; do
        label=${gate_case%%:*}
        gate_v=${gate_case#*:}
        case_dir="/work/$variant-$label"
        mkdir -p "$case_dir"
        cp /source/.spiceinit "$case_dir/.spiceinit"
        sed "s/^\.param gate_v=.*/.param gate_v=$gate_v/" \
          /source/cv.spice > "$case_dir/cv.spice"
        ln -sfn "$model_dir"/* "$case_dir/"
        (cd "$case_dir" && ngspice -b cv.spice > ngspice.log 2>&1)
      done
    done

    printf "gate_V original_F patched_F delta_F exact_file_match\n" \
      > /work/cv-comparison.txt
    for gate_case in $gate_cases; do
      label=${gate_case%%:*}
      gate_v=${gate_case#*:}
      original=/work/original-$label/cv.dat
      patched=/work/patched-$label/cv.dat
      original_f=$(awk "NR > 1 { value=\$2 } END { print value }" "$original")
      patched_f=$(awk "NR > 1 { value=\$2 } END { print value }" "$patched")
      delta_f=$(awk -v lhs="$patched_f" -v rhs="$original_f" \
        "BEGIN { printf \"%.17e\", lhs-rhs }")
      if cmp -s "$original" "$patched"; then
        exact_match=yes
      else
        exact_match=no
      fi
      printf "%s %s %s %s %s\n" \
        "$gate_v" "$original_f" "$patched_f" "$delta_f" "$exact_match" \
        >> /work/cv-comparison.txt
    done
  '

cat "$evidence_dir/cv-comparison.txt"
echo "evidence: $evidence_dir"
