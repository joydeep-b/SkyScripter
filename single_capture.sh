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

# Read the ISO, aperture, and shutter speed from the command line
while getopts i:a:s:h option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    a) APERTURE=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    h) HELP=true;;
  esac
done

# Print the help message
if [ "$HELP" = true ]; then
  echo "Usage: single_capture.sh [-i ISO] [-a APERTURE] [-s SHUTTER]"
  echo "  -i ISO: The ISO of the image (default: 100)"
  echo "  -a APERTURE: The aperture of the image (default: 5.6)"
  echo "  -s SHUTTER: The shutter speed of the image (default: 1/100)"
  echo "  -h: Print this help message"
  echo -e "\nDetected cameras:"
  gphoto2 --auto-detect
  exit 0
fi

echo "ISO: $ISO, Aperture: $APERTURE, Shutter: $SHUTTER"

# Capture the image
gphoto2 --set-config iso=$ISO --set-config aperture=$APERTURE --set-config shutterspeed=$SHUTTER --capture-image-and-download --filename "capture.jpg"