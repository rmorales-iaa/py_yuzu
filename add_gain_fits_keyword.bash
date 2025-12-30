#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <directory>"
  exit 1
fi

DIRECTORY="$1"

if [ ! -d "$DIRECTORY" ]; then
  echo "Error: Directory '$DIRECTORY' does not exist."
  exit 1
fi

# In case there are no matches, avoid iterating the literal pattern
shopt -s nullglob

# Support both .fits and .fit
files=( "$DIRECTORY"/*.fits "$DIRECTORY"/*.fit )

if [ ${#files[@]} -eq 0 ]; then
  echo "No FITS files found in '$DIRECTORY'."
  exit 0
fi

for fits_file in "${files[@]}"; do
  # How many HDUs?
  if ! nhdus_raw=$(astfits "$fits_file" --numhdus 2>/dev/null); then
    echo "[$fits_file] Could not read number of HDUs (skipping)."
    continue
  fi
  nhdus=$(printf '%s' "$nhdus_raw" | tr -d '[:space:]')
  if ! [[ "$nhdus" =~ ^[0-9]+$ ]]; then
    echo "[$fits_file] Non-numeric HDU count: '$nhdus_raw' (skipping)."
    continue
  fi

  has_gain=false
  gain_hdu=-1

  # Robust check: ask astfits directly for the key's value.
  for ((h=0; h<nhdus; h++)); do
    if astfits "$fits_file" --hdu="$h" --quiet --keyvalue=GAIN >/dev/null 2>&1; then
      has_gain=true
      gain_hdu=$h
      break
    fi
  done

  if $has_gain; then
    echo "[$fits_file] GAIN present in HDU $gain_hdu (no change)."
  else
    echo "[$fits_file] Adding GAIN=1.0 to HDU 0"
    astfits "$fits_file" --hdu=0 --write=GAIN,1.0,"Detector gain (e-/ADU)"
  fi
done

echo "Done."
