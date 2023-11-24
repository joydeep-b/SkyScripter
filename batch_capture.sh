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
  echo "  -d: The directory to save the images (default: images)"
  echo "  -n: The number of images to capture (default: 1)"
  echo "  -v: View the image after capture"
  echo "  -k: Keep the image on the camera after capture"
  echo "  -h: Print this help message"
  echo -e "\nDetected cameras:"
  gphoto2 --auto-detect
  exit 0
fi

echo "Capturing $NUM images to $IMAGE_DIR"
echo "ISO: $ISO, Aperture: $APERTURE, Shutter: $SHUTTER"

mkdir -p $IMAGE_DIR

# Convert the shutter speed to a decimal
SHUTTER_DECIMAL=$(echo "scale=3; $SHUTTER" | bc)

download_last_image() {
    # Optional argument: output filename
    output_filename=$1
    # Get the index of the last file
    last_file_index=$(gphoto2 --list-files | grep -E '#[0-9]+ ' | tail -1 | awk '{print $1}' | tr -d '#')
    # Check if output filename is provided
    if [ -z "$output_filename" ]; then
        # No filename provided, download file with original name
        gphoto2 --get-file $last_file_index
    else
        # Filename provided, download and rename file
        gphoto2 --get-file $last_file_index --filename "$output_filename"
    fi
}

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
  if [ $VIEW = true ]; then
    open "$FILENAME" -a preview
  fi
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
  sleep 1
done