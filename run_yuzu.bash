#!/usr/bin/env bash
#------------------------------
# Simple Photometry Pipeline
# Usage: ./run_yuzu.bash [STEP] [INPUT_DIR] [OBJECT] [options]
#------------------------------
set -Eeuo pipefail

# Default values
#INPUT_DIR="/mnt/uxmal_groups/common_data/photometry/roberto/calibrated/science_calibrated_wcs_grouped/J02465_I/"
#OBJECT="J02465+164"
#OBJECT_POS="02:46:34.71 +16:24:55.357"

INPUT_DIR="/mnt/uxmal_groups/stars/lemon_testing/HAT-P-16/"
OBJECT="HAT-P-16"
OBJECT_POS="00:38:17.529 +42:27:47.06"
ALL_STEPS=(mosaic photometry diffphot juicer)
declare -A RUN_STEP=()
PIPELINE_STEP=""
for step in "${ALL_STEPS[@]}"; do
    RUN_STEP["$step"]=1
done

select_only_step() {
    local selected_step="${1,,}"
    local step
    case "$selected_step" in
        mosaic|photometry|diffphot|juicer)
            [[ -z "$PIPELINE_STEP" ]] || {
                echo "ERROR: Only one pipeline step may be selected." >&2
                exit 1
            }
            for step in "${ALL_STEPS[@]}"; do RUN_STEP["$step"]=0; done
            RUN_STEP["$selected_step"]=1
            PIPELINE_STEP="$selected_step"
            ;;
        *)
            echo "ERROR: Unknown step: $1. Valid steps: ${ALL_STEPS[*]}" >&2
            exit 1
            ;;
    esac
}


# Parse arguments. A bare pipeline step selects exactly that step; omitted step
# runs the complete pipeline.
if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
    case "${1,,}" in
        mosaic|photometry|diffphot|juicer)
            select_only_step "$1"
            ;;
        *)
            INPUT_DIR="$1"
            ;;
    esac
    shift
fi

if [[ -n "$PIPELINE_STEP" && $# -gt 0 && ! "$1" =~ ^- ]]; then
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
      cat <<'EOF'
Usage: ./run_yuzu.bash [STEP] [INPUT_DIR] [OBJECT] [options]

By default runs: mosaic, photometry, diffphot, juicer.

Options:
  --object-pos "RA DEC"       Target coordinates for photometry and Juicer.
  -h, --help                  Show this help.

STEP runs exactly one step: mosaic, photometry, diffphot, or juicer.

Examples:
  ./run_yuzu.bash
  ./run_yuzu.bash diffphot
  ./run_yuzu.bash photometry /data/fits HAT-P-16
EOF
      exit 0
      ;;
    *)
      case "${1,,}" in
        mosaic|photometry|diffphot|juicer)
          select_only_step "$1"
          shift
          continue
          ;;
        *)
          echo "ERROR: Unknown option or step: $1" >&2
          exit 1
          ;;
      esac
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
SELECTED_STEPS=()
for step in "${ALL_STEPS[@]}"; do
    [[ "${RUN_STEP[$step]}" -eq 1 ]] && SELECTED_STEPS+=("$step")
done
echo "STEPS:      ${SELECTED_STEPS[*]}"

FITS_FILES=()
if [[ "${RUN_STEP[mosaic]}" -eq 1 || "${RUN_STEP[photometry]}" -eq 1 ]]; then
    # Gather FITS files only for steps that need them.
    mapfile -t FITS_FILES < <(find -L "${INPUT_DIR}" -maxdepth 1 -type f -name '*.fits' | sort)
    [[ "${#FITS_FILES[@]}" -gt 0 ]] || { echo "ERROR: No FITS files found in $INPUT_DIR" >&2; exit 1; }
    echo "Found ${#FITS_FILES[@]} FITS files."
fi

# Prepare directories
mkdir -p "$OUTPUT_DIR"

# Step 1. Image stacking
if [[ "${RUN_STEP[mosaic]}" -eq 1 ]]; then
    echo "[yuzu] mosaic..."
    ( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" mosaic "${FITS_FILES[@]}" "${OUTPUT_DIR}/stacked.fits" --overwrite )
fi

# Step 2. Absolute photometry
if [[ "${RUN_STEP[photometry]}" -eq 1 ]]; then
    echo "[yuzu] photometry..."
    ( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" photometry "${OUTPUT_DIR}/stacked.fits" "${FITS_FILES[@]}" "${OUTPUT_DIR}/photometry.db" --object-pos "$OBJECT_POS" )
fi

# Step 3. Differential photometry
# Match upstream LEMON's scientific profile by default.  The precision profile
# deliberately enforces SNR >= 1100 and can leave a one-star ensemble, which is
# unsuitable for direct LEMON comparisons.
if [[ "${RUN_STEP[diffphot]}" -eq 1 ]]; then
    echo "[yuzu] diffphot..."
    ( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" diffphot "${OUTPUT_DIR}/photometry.db" "${OUTPUT_DIR}/light_curve.db" --lemon-mode --diagnostics )
fi

# Step 4. Juicer
if [[ "${RUN_STEP[juicer]}" -eq 1 ]]; then
    echo "[yuzu] juicer (star: $OBJECT_POS)..."
    ( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" juicer "${OUTPUT_DIR}/light_curve.db" --star "${OBJECT_POS}" )
fi

echo "Pipeline finished successfully."
