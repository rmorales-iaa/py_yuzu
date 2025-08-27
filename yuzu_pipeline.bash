#!/usr/bin/env bash
#
# Photometry pipeline driver (WCS solve ? mosaic ? photometry ? diff. photometry ? juicer)
#
# Usage:
# ./run_photometry_pipeline.sh [INPUT_IMAGE_DIR] [OBJECT] [--object-pos "RA DEC"] [--keep]
#
# Examples:
# ./run_photometry_pipeline.sh \
# /mnt/uxmal_groups/common_data/photometry/HAT-P-16_raw HAT-P-16 \
# --object-pos "00 38 17.56 +42 27 47.2"
#
# Notes:
# - By default, this script cleans (removes) the WCS-solved and output directories before running.
# Use --keep to preserve existing outputs.
# - INPUT_IMAGE_DIR and OBJECT can also be provided via environment variables
# INPUT_IMAGE_DIR, OBJECT, OBJECT_POS (CLI args take precedence).
set -Eeuo pipefail
#-----------------------------
# user inputs (can be overridden by args or env)
#-----------------------------
INPUT_IMAGE_DIR="/mnt/uxmal_groups/common_data/photometry/input_files/HAT-P-16_raw_wcs"
OBJECT="HAT-P-16"
OBJECT_POS='00 38 17.56 +42 27 47.2'
YUZU_CONFIGURATION="/mnt/uxmal_groups/common_data/apps/py_yuzu/conf_manager/matilde_conf.txt"
#-----------------------------
# External tooling locations
ASTROMETRY_DIR="/mnt/uxmal_groups/common_data/apps/m2/input/astrometry.net"
WCS_SOLVER_SCRIPT="./wcs_classic_parallel_solve_fits.bash"
#-----------------------------
YUZU_DIR="/mnt/uxmal_groups/common_data/apps/py_yuzu"
YUZU_BIN="./yuzu"
#-----------------------------
# Timing
#-----------------------------
START_TS="$(date +%s)"
on_exit() {
  local end_ts elapsed
  end_ts="$(date +%s)"
  elapsed="$(( end_ts - START_TS ))"
  echo "Elapsed time (seconds): ${elapsed}"
}
trap on_exit EXIT
#-----------------------------
# Logging helpers
#-----------------------------
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
#-----------------------------
# Safety helpers
#-----------------------------
safe_rm_rf() {
  # Removes a directory safely if it looks like a concrete path (not empty or /).
  local target="$1"
  [[ -n "${target}" ]] || die "Refusing to remove an empty path."
  [[ "${target}" != "/" ]] || die 'Refusing to remove "/".'
  # Extra guard: only allow absolute paths under /mnt
  [[ "${target}" == /mnt/* ]] || die "Refusing to remove non-/mnt path: ${target}"
  rm -rf -- "${target}"
}
#-----------------------------
# Usage
#-----------------------------
usage() {
  cat <<'USAGE'
Usage:
  run_photometry_pipeline.sh [INPUT_IMAGE_DIR] [OBJECT] [--object-pos "RA DEC"] [--keep]
Positional:
  INPUT_IMAGE_DIR Directory with raw FITS images (default: env INPUT_IMAGE_DIR or built-in default)
  OBJECT Target object name (default: env OBJECT or built-in default)
Options:
  --object-pos "00 38 17.56 +42 27 47.2" Star position (RA DEC) for juicer (default: env OBJECT_POS or built-in)
  --keep Do NOT delete existing *_wcs_solved and output directories
  -h, --help Show this help
Environment variables (optional):
  INPUT_IMAGE_DIR, OBJECT, OBJECT_POS
USAGE
}
#-----------------------------
# Parse arguments
#-----------------------------
KEEP=0
ARG_INPUT_IMAGE_DIR="${1:-${INPUT_IMAGE_DIR:-$INPUT_IMAGE_DIR}}"
if [[ "${1-}" =~ ^- ]]; then ARG_INPUT_IMAGE_DIR="${INPUT_IMAGE_DIR:-$INPUT_IMAGE_DIR}"; fi
ARG_OBJECT="${2:-${OBJECT:-$OBJECT}}"
if [[ "${2-}" =~ ^- ]]; then ARG_OBJECT="${OBJECT:-$OBJECT}"; fi
ARG_OBJECT_POS="${OBJECT_POS:-$OBJECT_POS}"
shift_count=0
[[ $# -ge 1 && ! "$1" =~ ^- ]] && { shift; ((shift_count++)); }
[[ $# -ge 1 && ! "$1" =~ ^- ]] && { shift; ((shift_count++)); }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --object-pos)
      shift
      [[ $# -gt 0 ]] || die "--object-pos requires a value"
      ARG_OBJECT_POS="$1"
      ;;
    --keep)
      KEEP=1
      ;;
    -h|--help)
      usage; exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done
#-----------------------------
# Derived paths
#-----------------------------
INPUT_IMAGE_DIR="${ARG_INPUT_IMAGE_DIR%/}"
OBJECT="${ARG_OBJECT}"
OBJECT_POS="${ARG_OBJECT_POS}"
INPUT_IMAGE_WCS_SOLVED_DIR="${INPUT_IMAGE_DIR}_wcs_solved"
OUTPUT_DIR="/mnt/uxmal_groups/common_data/photometry/yuzu/${OBJECT}"
#-----------------------------
# Preflight checks
#-----------------------------
check_prereqs() {
  log "Checking prerequisites..."
  [[ -d "${INPUT_IMAGE_DIR}" ]] || die "INPUT_IMAGE_DIR not found: ${INPUT_IMAGE_DIR}"
  [[ -x "${ASTROMETRY_DIR}/${WCS_SOLVER_SCRIPT}" ]] || die "WCS solver not executable: ${ASTROMETRY_DIR}/${WCS_SOLVER_SCRIPT}"
  [[ -x "${YUZU_DIR}/${YUZU_BIN}" ]] || die "yuzu binary not executable: ${YUZU_DIR}/${YUZU_BIN}"
  # Show a quick count of FITS to ensure we have inputs
  local cnt
  cnt="$(find "${INPUT_IMAGE_DIR}" -maxdepth 1 -type f -name '*.fits' | wc -l | tr -d ' ')"
  [[ "${cnt}" -gt 0 ]] || die "No FITS files found in ${INPUT_IMAGE_DIR}"
  log "Found ${cnt} FITS files in input."
}
#-----------------------------
# Cleanup / prepare output dirs
#-----------------------------
prepare_dirs() {
  if [[ "${KEEP}" -eq 0 ]]; then
    log "Cleaning WCS-solved dir: ${INPUT_IMAGE_WCS_SOLVED_DIR}"
    safe_rm_rf "${INPUT_IMAGE_WCS_SOLVED_DIR}" || true
    log "Cleaning output dir: ${OUTPUT_DIR}"
    safe_rm_rf "${OUTPUT_DIR}" || true
  else
    log "Keeping existing directories."
  fi
  mkdir -p -- "${INPUT_IMAGE_WCS_SOLVED_DIR}" "${OUTPUT_DIR}"
}
#-----------------------------
# Run WCS solver
#-----------------------------
run_wcs_solver() {
  log "Running WCS solve..."
  cd "${ASTROMETRY_DIR}"
  "${WCS_SOLVER_SCRIPT}" "${INPUT_IMAGE_DIR}" "${INPUT_IMAGE_WCS_SOLVED_DIR}"
  log "WCS solve completed."
}
#-----------------------------
# Build file list from WCS-solved dir
#-----------------------------
gather_wcs_fits() {
  log "Gathering WCS-solved FITS files..."
  mapfile -t FITS_FILES < <(find "${INPUT_IMAGE_WCS_SOLVED_DIR}" -maxdepth 1 -type f -name '*.fits' | sort)
  [[ "${#FITS_FILES[@]}" -gt 0 ]] || die "No WCS-solved FITS found in ${INPUT_IMAGE_WCS_SOLVED_DIR}"
  log "Found ${#FITS_FILES[@]} WCS-solved FITS."
}
#-----------------------------
# Yuzu steps
#-----------------------------
run_mosaic() {
  log "[yuzu] Creating mosaic..."
  mkdir -p -- "${OUTPUT_DIR}"
  cd "${YUZU_DIR}"
  "${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" mosaic "${FITS_FILES[@]}" "${OUTPUT_DIR}/stacked.fits"
  log "Mosaic saved to ${OUTPUT_DIR}/stacked.fits"
}
#-----------------------------
run_photometry() {
  log "[yuzu] Photometry..."
  cd "${YUZU_DIR}"
  "${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" photometry "${OUTPUT_DIR}/stacked.fits" "${FITS_FILES[@]}" "${OUTPUT_DIR}/photometry.db"
  log "Photometry DB: ${OUTPUT_DIR}/photometry.db"
}
#-----------------------------
run_diffphot() {
  log "[yuzu] Differential photometry..."
  cd "${YUZU_DIR}"
  "${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" diffphot "${OUTPUT_DIR}/photometry.db" "${OUTPUT_DIR}/light_curve.db"
  log "Light curve DB: ${OUTPUT_DIR}/light_curve.db"
}
#-----------------------------
run_juicer() {
  log "[yuzu] Juicer (star: ${OBJECT_POS})..."
  cd "${YUZU_DIR}"
  "${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" juicer "${OUTPUT_DIR}/light_curve.db" --star "${OBJECT_POS}"
  log "Juicer completed."
}
#-----------------------------
delete_temp_files() {
  log "[yuzu] deleting temporal files"
  # Check and delete each file if it exists
  for file in "${OUTPUT_DIR}/curves.db-shm" "${OUTPUT_DIR}/curves.db-wal" \
               "${OUTPUT_DIR}/photometry.db-wal" "${OUTPUT_DIR}/photometry.db-shm" \
               "${OUTPUT_DIR}/stacked_area.fits" \
            ; do
    if [ -f "$file" ]; then
        rm -f "$file"
        echo "Deleted $file"
    else
        echo "$file does not exist"
    fi
  done
  rm -fr /tmp/*_LEMON_*
}
#-----------------------------
# Main
#-----------------------------
main() {
  log "Starting photometry pipeline"
  log "INPUT_IMAGE_DIR: ${INPUT_IMAGE_DIR}"
  log "OBJECT: ${OBJECT}"
  log "OBJECT_POS: ${OBJECT_POS}"
  log "OUTPUT_DIR: ${OUTPUT_DIR}"
  log "WCS_SOLVED_DIR: ${INPUT_IMAGE_WCS_SOLVED_DIR}"
  check_prereqs
  prepare_dirs
  run_wcs_solver
  gather_wcs_fits
  run_mosaic
  run_photometry
  run_diffphot
  delete_temp_files
  run_juicer
  log "Pipeline finished successfully."
}
main
