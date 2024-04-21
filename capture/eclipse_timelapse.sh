#!/bin/bash
set -e

ISO=100
SHUTTER=1/100
HELP=false
IMAGE_DIR="eclipse_timelapse"
KEEP=""

# Read the ISO, aperture, and shutter speed from the command line
while getopts i:a:s:fd:n:vkh option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    f) FORCE="--force-overwrite";;
    d) IMAGE_DIR=${OPTARG};;
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

echo "Image format: RAW"
gphoto2 --set-config "/main/imgsettings/imageformat=21"

echo "Bracketing: No AEB"
gphoto2 --set-config "/main/capturesettings/aeb=0"

gphoto2 --set-config "/main/capturesettings/shutterspeed=$SHUTTER"

echo "ISO: 100"
gphoto2 --set-config "/main/imgsettings/iso=100"

# Drive mode: Continuous high speed
echo "Drive mode: Continuous high speed"
gphoto2 --set-config "/main/capturesettings/drivemode=2"

mkdir -p $IMAGE_DIR

# Do while loop to execute indefinitely.
COUNT=0
NUM_IMAGES=0
while true; do
  FILENAME=$(next_filename)
  echo "Capturing image $NUM_IMAGES: $FILENAME"
  gphoto2 --capture-image-and-download --filename "$FILENAME" --force-overwrite > /dev/null
  COUNT=$((COUNT + 1))
  echo "Sleeping for 5 seconds..."
  # sleep 1
done

echo "Count: $COUNT, Num images: $NUM_IMAGES"