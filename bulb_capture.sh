#!/bin/bash

# If gphoto2 is not found, exit
if ! [ -x "$(command -v gphoto2)" ]; then
  echo 'Error: gphoto2 is not installed.' >&2
  exit 1
fi

ISO=100
APERTURE=5.6
SHUTTER=60
HELP=false
FORCE=""
NUM=1

# Read the ISO, aperture, and shutter speed from the command line
while getopts i:a:s:n:fh option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    a) APERTURE=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    n) NUM=${OPTARG};;
    f) FORCE="--force-overwrite";;
    h) HELP=true;;
  esac
done

# Print the help message
if [ "$HELP" = true ]; then
  echo "Usage: single_capture.sh [-i ISO] [-a APERTURE] [-s SHUTTER]"
  echo "  -i ISO: The ISO of the image (default: 100)"
  echo "  -a APERTURE: The aperture of the image (default: 5.6)"
  echo "  -s SHUTTER: The shutter speed of the image (default: 1/100)"
  echo "  -n NUM: The number of images to capture (default: 1)"
  echo "  -f: Force overwrite of existing files"
  echo "  -h: Print this help message"
  echo -e "\nDetected cameras:"
  gphoto2 --auto-detect
  exit 0
fi

# Helper function to find the next available filename of the form
# capture_nnnn.jpg where nnnn is a zero-appended number
next_filename() {
  i=0
  while [ -f capture_$(printf "%04d" $i).jpg ]; do
    i=$((i+1))
  done 
  echo capture_$(printf "%04d" $i).jpg
}

# Make sure camera is in bulb mode

echo "ISO: $ISO, Aperture: $APERTURE, Shutter: $SHUTTER"
gphoto2 --set-config iso=$ISO --set-config aperture=$APERTURE

FILENAME=$(next_filename)
for (( c=1; c<=$NUM; c++ ))
do
  FILENAME=$(next_filename)
  echo "$(printf "%03d" $c)"

  # Capture the image
  echo "Shutter Immediate"
  gphoto2 --set-config /main/actions/eosremoterelease=5
  sleep $SHUTTER
  echo "Shutter Release Full"
  gphoto2 --set-config /main/actions/eosremoterelease=4
  if [ "$c" -lt "$NUM" ]; then
    # Wait for the camera to be ready
    echo "Waiting for camera to be ready..."
    sleep 100
  fi
done