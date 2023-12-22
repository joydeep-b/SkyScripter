#!/bin/bash


# This script computes the drift over a sequence of images

SIRIL_PATH="/Applications/Siril.app/Contents/MacOS/siril-cli"
WCS_COORDS=""
IMAGE_DIR=""
THIS_DIR=$(pwd)
OUT_FILE="drift.csv"

usage() {
    echo "Usage: $0 [-w wcs_coords] [-i image_dir]" 1>&2
    exit 1
}

while getopts ":w:i:o:" o; do
    case "${o}" in
        w)
            WCS_COORDS=${OPTARG}
            ;;
        i)
            IMAGE_DIR=${OPTARG}
            ;;
        o)
            OUT_FILE=${OPTARG}
            ;;
        *)
            usage
            ;;
    esac
done

if [ -z "${WCS_COORDS}" ] || [ -z "${IMAGE_DIR}" ]; then
    usage
fi

get_wcs_coords() {
  local image=$1
  local output=$($SIRIL_PATH -d $THIS_DIR -s - 2>/dev/null <<ENDSIRIL
requires 1.2.0
load $FILE
platesolve $WCS_COORDS -platesolve -catalog=nomad -limitmag=8 
close
ENDSIRIL
)
  regex="Image center: alpha: ([0-9]+)h([0-9]+)m([0-9]+)s, delta: ([+-])([0-9]+)°([0-9]+)'([0-9]+)"
  if [[ $output =~ $regex ]]; then
      alpha_h=${BASH_REMATCH[1]}
      alpha_m=${BASH_REMATCH[2]}
      alpha_s=${BASH_REMATCH[3]}
      delta_sign=${BASH_REMATCH[4]}
      delta_d=${BASH_REMATCH[5]}
      delta_m=${BASH_REMATCH[6]}
      delta_s=${BASH_REMATCH[7]}
  else
      echo ""
      return
  fi

  # echo "RA/DEC ${alpha_h}h${alpha_m}m${alpha_s}s ${delta_sign}${delta_d}°${delta_m}'${delta_s}"

  # Convert the alpha and delta values to decimal degrees
  alpha=$(echo "scale=9; 180/12*($alpha_h + $alpha_m/60 + $alpha_s/3600)" | bc)
  delta=$(echo "scale=9; 0 $delta_sign 1 * ($delta_d + $delta_m/60 + $delta_s/3600)" | bc)

  echo "$alpha, $delta"
}

echo "RA(deg), DEC(deg)" > $OUT_FILE

for FILE in $IMAGE_DIR/*.CR3; do
    # echo "Processing $FILE"
    coords=$(get_wcs_coords $FILE)
    if [ -z "$coords" ]; then
        echo "Could not get WCS coordinates for $FILE"
        continue
    else
        echo "WCS coordinates: $coords"
        echo $coords >> $OUT_FILE
    fi
done




# echo -e $output

# From the output, extract the image coordinates from the line that looks like: "Image center: alpha: 06h32m10s, delta: +04°59'58"
# The regex below matches the line and extracts the alpha and delta values


