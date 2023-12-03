#!/bin/bash

set -e

# If gphoto2 is not found, exit
if ! [ -x "$(command -v gphoto2)" ]; then
  echo 'Error: gphoto2 is not installed.' >&2
  exit 1
fi

ISO=100
APERTURE=5.6
SHUTTER=1/100
HELP=false
VIEW=false
KEEP=""
FILENAME="capture.CR3"


# Read the ISO, aperture, and shutter speed from the command line
while getopts i:a:s:f:vkh option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    a) APERTURE=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    f) FILENAME=${OPTARG};;
    v) VIEW=true;;
    k) KEEP="--keep";;
    h) HELP=true;;
  esac
done


# Print the help message
if [ "$HELP" = true ]; then
  echo "Usage: single_capture.sh [-i ISO] [-a APERTURE] [-s SHUTTER]"
  echo "  -i ISO: The ISO of the image (default: 100)"
  echo "  -a APERTURE: The aperture of the image (default: 5.6)"
  echo "  -s SHUTTER: The shutter speed of the image (default: 1/100)"
  echo "  -f: Filename of the image (default: capture.CR3)"
  echo "  -v: View the image after capture"
  echo "  -k: Keep the image on the camera after capture"
  echo "  -h: Print this help message"
  echo -e "\nDetected cameras:"
  gphoto2 --auto-detect
  exit 0
fi

echo "Capturing $FILENAME with ISO $ISO, Aperture $APERTURE, Shutter $SHUTTER"

# Ensure capturing in RAW
gphoto2 --set-config /main/imgsettings/imageformat=RAW

# Convert the shutter speed to a decimal
SHUTTER_DECIMAL=$(echo "scale=3; $SHUTTER" | bc)

download_last_image() {
    # Optional argument: output filename
    output_filename=$1
    # Get the index of the last file
    last_file_index=$(gphoto2 --list-files | grep -E '#[0-9]+ ' | tail -1 | awk '{print $1}' | tr -d '#')
    # Downloading the last image is a bit flakey, so we keep the file on the camera just in case (--keep)
    # Check if output filename is provided
    if [ -z "$output_filename" ]; then
        # No filename provided, download file with original name
        gphoto2 --get-file $last_file_index --keep --force-overwrite
    else
        # Filename provided, download and rename file
        gphoto2 --get-file $last_file_index \
                --filename "$output_filename" \
                --keep \
                --force-overwrite
    fi
}

echo "Capturing image..."
# If the shutter speed is greater than 30, then we need to use bulb mode.
if (( $(echo "$SHUTTER_DECIMAL > 30" | bc -l) )); then
  echo "Shutter speed is greater than 30 seconds. Using bulb mode."
  gphoto2 --set-config /main/capturesettings/autoexposuremodedial=Bulb
  gphoto2 --set-config /main/imgsettings/imageformat=RAW \
          --set-config iso=$ISO \
          --set-config aperture=$APERTURE
  echo "Shutter Release Immediate"
  gphoto2 --set-config /main/actions/eosremoterelease=5
  sleep $SHUTTER_DECIMAL
  echo "Shutter Release Full"
  gphoto2 --set-config /main/actions/eosremoterelease=4
  download_last_image $FILENAME
else
  # Capture the image
  gphoto2 --set-config /main/imgsettings/imageformat=RAW \
          --set-config /main/capturesettings/autoexposuremodedial=Manual \
          --set-config iso=$ISO \
          --set-config shutterspeed=$SHUTTER \
          --set-config aperture=$APERTURE \
          --capture-image-and-download --filename "$FILENAME" \
          --force-overwrite \
          $KEEP
fi

if [ $VIEW = true ]; then
  # If on Mac, open the image in Preview
  if [ "$(uname)" == "Darwin" ]; then
    open "$FILENAME" -a preview
  elif [ "$(expr substr $(uname -s) 1 5)" == "Linux" ]; then
    # If on Linux, open the image in geeqie
    # Check if geeqie is installed
    if ! [ -x "$(command -v geeqie)" ]; then
      echo 'Error: geeqie is not installed.' >&2
      echo 'Please install geeqie using "sudo apt install geeqie"'  >&2
      exit 1
    fi
    geeqie "$FILENAME"
  fi

fi