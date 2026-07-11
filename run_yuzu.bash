#!/usr/bin/env bash
#------------------------------
# Simple Photometry Pipeline
# Usage: ./run_yuzu.bash [INPUT_DIR] [OBJECT] [--object-pos "RA DEC"]
#------------------------------
set -Eeuo pipefail

# Default values
#INPUT_DIR="/mnt/uxmal_groups/common_data/photometry/roberto/calibrated/science_calibrated_wcs_grouped/J02465_I/"
#OBJECT="J02465+164"
#OBJECT_POS="02:46:34.71 +16:24:55.357"

INPUT_DIR="/mnt/uxmal_groups/stars/lemon_testing/HAT-P-16/"
OBJECT="HAT-P-16"
OBJECT_POS="00:38:17.529 +42:27:47.06"


# Parse arguments
if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
    INPUT_DIR="$1"
    shift
fi

if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
    OBJECT="$1"
    shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --object-pos)
      shift
      [[ $# -gt 0 ]] || { echo "ERROR: --object-pos requires a value" >&2; exit 1; }
      OBJECT_POS="$1"
      shift
      # Greedily consume tokens for position if they don't start with '-'
      while [[ $# -gt 0 && ! "$1" =~ ^- ]]; do
          OBJECT_POS="${OBJECT_POS} $1"
          shift
      done
      continue
      ;;
    -h|--help)
      echo "Usage: ./run_yuzu.bash [INPUT_DIR] [OBJECT] [--object-pos \"RA DEC\"]"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      exit 1
      ;;
  esac
  shift
done

# Derived paths
INPUT_DIR="${INPUT_DIR%/}"
OUTPUT_DIR="/mnt/uxmal_groups/common_data/photometry/yuzu/${OBJECT}"
YUZU_CONFIGURATION="/mnt/uxmal_groups/common_data/apps/py_yuzu/src/py_yuzu/conf_manager/matilde_conf.txt"
YUZU_DIR="/mnt/uxmal_groups/common_data/apps/py_yuzu"
YUZU_BIN=(python3 "$YUZU_DIR/main.py")

# Ensure we use the correct system-wide MontagePy and other libraries
export PYTHONPATH="${YUZU_DIR}/src/py_yuzu/python_packages:/usr/local/lib64/python3.14/site-packages:${PYTHONPATH:-}"
# Ensure tools are in PATH
export PATH="/mnt/uxmal_groups/common_data/apps/astromatic/sextractor/:$PATH"
# Disable DRI error message
export LIBGL_ALWAYS_SOFTWARE=1
# Keep GTK juicer on pure software rendering and disable inaccessible desktop
# bridges in remote/headless sessions.
export GTK_A11Y="${GTK_A11Y:-none}"
export NO_AT_BRIDGE="${NO_AT_BRIDGE:-1}"
export GSK_RENDERER="${GSK_RENDERER:-cairo}"
export GDK_DISABLE="${GDK_DISABLE:-gl,vulkan,egl}"
# Silence non-standard FITS card warnings (e.g., NOTES)
export ASTROPY_SKIP_FITS_VERIFY=1
export PYTHONWARNINGS="ignore::SyntaxWarning"

echo "Starting simple pipeline..."
echo "INPUT_DIR:  $INPUT_DIR"
echo "OBJECT:     $OBJECT"
echo "OBJECT_POS: $OBJECT_POS"
echo "OUTPUT_DIR: $OUTPUT_DIR"

# Gather FITS files (following symlinks)
mapfile -t FITS_FILES < <(find -L "${INPUT_DIR}" -maxdepth 1 -type f -name '*.fits' | sort)
[[ "${#FITS_FILES[@]}" -gt 0 ]] || { echo "ERROR: No FITS files found in $INPUT_DIR" >&2; exit 1; }
echo "Found ${#FITS_FILES[@]} FITS files."

# Prepare directories
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# Step 1. Image stacking
echo "[yuzu] mosaic..."
( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" mosaic "${FITS_FILES[@]}" "${OUTPUT_DIR}/stacked.fits" --overwrite )

# Step 2. Absolute photometry
echo "[yuzu] photometry..."
( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" photometry "${OUTPUT_DIR}/stacked.fits" "${FITS_FILES[@]}" "${OUTPUT_DIR}/photometry.db" )

# Step 3. Differential photometry
echo "[yuzu] diffphot..."
( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" diffphot "${OUTPUT_DIR}/photometry.db" "${OUTPUT_DIR}/light_curve.db" --precision-mode --detrend-airmass --diagnostics )

# Step 4. Juicer
echo "[yuzu] juicer (star: $OBJECT_POS)..."
( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" juicer "${OUTPUT_DIR}/light_curve.db" --star "${OBJECT_POS}" )

echo "Pipeline finished successfully."
