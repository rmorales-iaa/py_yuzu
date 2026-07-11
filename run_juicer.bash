#!/usr/bin/env bash
#------------------------------
# Simple Photometry Pipeline
# Usage: ./run_yuzu.bash [INPUT_DIR] [OBJECT] [--object-pos "RA DEC"]
#------------------------------
set -Eeuo pipefail

# Default values
#INPUT_DIR="/mnt/uxmal_groups/common_data/photometry/roberto/calibrated/science_calibrated_wcs_grouped/J02465_I/"
#OBJECT="J02465+164"
#OBJECT_POS="02:46:33.232 +16:24:55.44"


INPUT_DIR="/mnt/uxmal_groups/stars/lemon_testing/HAT-P-16/"
OBJECT="HAT-P-16"
OBJECT_POS="00:38:17.529 +42:27:47.06"


# Derived paths
INPUT_DIR="${INPUT_DIR%/}"
OUTPUT_DIR="/mnt/uxmal_groups/common_data/photometry/yuzu/${OBJECT}"
YUZU_CONFIGURATION="/mnt/uxmal_groups/common_data/apps/py_yuzu/src/py_yuzu/conf_manager/matilde_conf.txt"
YUZU_DIR="/mnt/uxmal_groups/common_data/apps/py_yuzu"
YUZU_BIN=(python3 "$YUZU_DIR/main.py")

# Keep GTK juicer on pure software rendering and disable inaccessible desktop
# bridges in remote/headless sessions.
export GTK_A11Y="${GTK_A11Y:-none}"
export NO_AT_BRIDGE="${NO_AT_BRIDGE:-1}"
export GSK_RENDERER="${GSK_RENDERER:-cairo}"
export GDK_DISABLE="${GDK_DISABLE:-gl,vulkan,egl}"

# Step 4. Juicer
echo "[yuzu] juicer (star: $OBJECT_POS)..."
( cd "$YUZU_DIR" && "${YUZU_BIN[@]}" --config "$YUZU_CONFIGURATION" juicer "${OUTPUT_DIR}/light_curve.db" --star "${OBJECT_POS}" )
