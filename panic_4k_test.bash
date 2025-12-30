#------------------------------
#start of script
#------------------------------
#set -x #debug
#------------------------------

INPUT_DIR=/mnt/uxmal_groups/stars/lemon_testing_om_wcs
OBJECT=HAT-P-16
OBJECT_POS="00 38 17.55 +42:27:47.22"

#INPUT_DIR=/mnt/uxmal_groups/stars/panic_4k/jmiguel/M67_om_wcs/
#OBJECT=M67
#OBJECT_POS="08 51 23.52 +11 48 54"


#------------------------------
OUTPUT_DIR="/mnt/uxmal_groups/common_data/photometry/yuzu/${OBJECT}/"
#------------------------------
YUZU_CONFIGURATION="/mnt/uxmal_groups/common_data/apps/py_yuzu/conf_manager/matilde_conf.txt"
YUZU_DIR="/mnt/uxmal_groups/common_data/apps/py_yuzu"
YUZU_BIN="./yuzu"
export PATH="/mnt/uxmal_groups/common_data/apps/astromatic/sextractor/:$PATH"
#------------------------------
#gather FITS files
FITS_FILES="${INPUT_DIR}/*.fits"
#------------------------------
#disable DRI error message
export LIBGL_ALWAYS_SOFTWARE=1

#------------------------------
#ensure GAIN FITS keyword
#./add_gain_fits_keyword.bash  $INPUT_DIR
#------------------------------
#prepare directories
rm -fr $OUTPUT_DIR
mkdir -p $OUTPUT_DIR
#------------------------------
#step 1. image stacking
cd $YUZU_DIR
"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" mosaic "${FITS_FILES}" "${OUTPUT_DIR}/stacked.fits" --overwrite

#step 2. absolute photometry
cd $YUZU_DIR
"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" photometry "${OUTPUT_DIR}/stacked.fits" "${FITS_FILES}" "${OUTPUT_DIR}/photometry.db"

#step 3. differential photometry
cd $YUZU_DIR
"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" diffphot "${OUTPUT_DIR}/photometry.db" "${OUTPUT_DIR}/light_curve.db"

#step 4. juicer
cd $YUZU_DIR
"${YUZU_BIN}" --config "${YUZU_CONFIGURATION}" juicer "${OUTPUT_DIR}/light_curve.db" --star "${OBJECT_POS}"

#------------------------------
#end of script
#------------------------------
