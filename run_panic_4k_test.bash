#------------------------------
#start of script
#------------------------------
set -x #debug
#------------------------------
INPUT_DIR=/home/rafa/apps/lemon/data/in/input_dir
OBJECT=HAT-P-16
OBJECT_POS="00 38 17.53 +42 27 47.15"

#INPUT_DIR=/home/rafa/Downloads/deleteme/test_1/
#OBJECT=SA98
#OBJECT_POS="06 52 01.89 -0 27 21.6"
#------------------------------
OUTPUT_DIR="/mnt/uxmal_groups/common_data/photometry/yuzu/${OBJECT}"
#------------------------------
YUZU_CONFIGURATION="/mnt/uxmal_groups/common_data/apps/py_yuzu/conf_manager/matilde_conf.txt"

YUZU_DIR="/home/rafa/proyecto/py_yuzu"
#YUZU_DIR="/mnt/uxmal_groups/common_data/apps/py_yuzu"
YUZU_BIN="./yuzu"
#------------------------------
#gather FITS files
FITS_FILES="${INPUT_DIR}/*.fits"
#------------------------------
#ensure GAIN FITS keyword
./add_gain_fits_keyword.bash  $INPUT_DIR
#------------------------------
#prepare directories
rm -fr $OUTPUT_DIR
mkdir -p $OUTPUT_DIR
#------------------------------
"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" mosaic "${FITS_FILES}" "${OUTPUT_DIR}/stacked.fits"

"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" photometry "${OUTPUT_DIR}/stacked.fits" "${FITS_FILES}" "${OUTPUT_DIR}/photometry.db"

"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" diffphot "${OUTPUT_DIR}/photometry.db" "${OUTPUT_DIR}/light_curve.db"

"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" juicer "${OUTPUT_DIR}/light_curve.db" --star "${OBJECT_POS}"

#------------------------------
#end of script
#------------------------------
