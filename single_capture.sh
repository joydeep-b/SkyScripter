#!/bin/bash

# If gphoto2 is not found, exit
if ! [ -x "$(command -v gphoto2)" ]; then
  echo 'Error: gphoto2 is not installed.' >&2
  exit 1
fi

ISO=100
APERTURE=5.6
SHUTTER=1/100
HELP=false
FORCE=""
IMAGE_DIR="images"
NUM=1
VIEW=false
KEEP=""

# Read the ISO, aperture, and shutter speed from the command line
while getopts i:a:s:fd:n:vkh option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    a) APERTURE=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    f) FORCE="--force-overwrite";;
    d) IMAGE_DIR=${OPTARG};;
    n) NUM=${OPTARG};;
    v) VIEW=true;;
    k) KEEP="--keep";;
    h) HELP=true;;
  esac
done

# Helper function to find the next available filename of the form
# capture_nnnn.jpg where nnnn is a zero-appended number
next_filename() {
  i=0
  while [ -f $IMAGE_DIR/capture_$(printf "%04d" $i).CR3 ]; do
    i=$((i+1))
  done 
  echo $IMAGE_DIR/capture_$(printf "%04d" $i).CR3
}

FILENAME=$(next_filename)

# Print the help message
if [ "$HELP" = true ]; then
  echo "Usage: single_capture.sh [-i ISO] [-a APERTURE] [-s SHUTTER]"
  echo "  -i ISO: The ISO of the image (default: 100)"
  echo "  -a APERTURE: The aperture of the image (default: 5.6)"
  echo "  -s SHUTTER: The shutter speed of the image (default: 1/100)"
  echo "  -f: Force overwrite of existing files"
  echo "  -h: Print this help message"
  echo -e "\nDetected cameras:"
  gphoto2 --auto-detect
  exit 0
fi

echo "ISO: $ISO, Aperture: $APERTURE, Shutter: $SHUTTER"

# Convert the shutter speed to a decimal
SHUTTER_DECIMAL=$(echo "scale=3; $SHUTTER" | bc)

# If the shutter speed is greater than 30, then we need to use bulb mode.
if (( $(echo "$SHUTTER_DECIMAL > 30" | bc -l) )); then
  echo "Shutter speed is greater than 30 seconds. Using bulb mode."
  
  exit 1
fi

# Otherwise, we can use Manual mode
for (( c=1; c<=$NUM; c++ ))
do
  FILENAME=$(next_filename)
  echo "$(printf "%03d" $c)"

  # Capture the image
  gphoto2 --set-config /main/imgsettings/imageformat=RAW \
          --set-config /main/capturesettings/autoexposuremodedial=Manual \
          --set-config iso=$ISO \
          --set-config shutterspeed=$SHUTTER \
          --set-config aperture=$APERTURE \
          --capture-image-and-download --filename "$FILENAME" $KEEP $FORCE
  if [ $VIEW = true ]; then
    open "$FILENAME" -a preview
  fi
done