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

echo "Capturing $NUM images to \"$IMAGE_DIR\""
echo "ISO: $ISO, Aperture: $APERTURE, Shutter: $SHUTTER"

mkdir -p $IMAGE_DIR

# Convert the shutter speed to a decimal
SHUTTER_DECIMAL=$(echo "scale=3; $SHUTTER" | bc)

# If the shutter speed is greater than 30, then we need to use bulb mode.
if (( $(echo "$SHUTTER_DECIMAL > 30" | bc -l) )); then
  echo "Shutter speed is greater than 30 seconds. Using bulb mode."
  gphoto2 --set-config /main/capturesettings/autoexposuremodedial=Bulb
  gphoto2 --set-config /main/imgsettings/imageformat=RAW \
          --set-config iso=$ISO \
          --set-config aperture=$APERTURE

  t_start=$(date +%s.%N)
  t_per_image=$(echo "scale=3; 3 + $SHUTTER_DECIMAL" | bc -l)
  for (( c=1; c<=$NUM; c++ ))
  do
    FILENAME=$(next_filename)
    t_left=$(echo "scale=3; $t_per_image * ($NUM - $c + 1)" | bc -l)
    t_left_hr=$(echo "scale=0; $t_left / 3600" | bc -l)
    t_left_min=$(echo "scale=0; ($t_left - $t_left_hr * 3600)/60" | bc -l)
    t_left_sec=$(echo "scale=0; ($t_left - $t_left_hr * 3600 - $t_left_min * 60)" | bc -l)

    # Print status of the form 001/100 t_left: 0.000
    printf "%d / %d Estimated time left: %.0fhr %.0fmin %.0fs\n" $c $NUM $t_left_hr $t_left_min $t_left_sec
    
    gphoto2 --set-config eosremoterelease=Immediate \
            --wait-event=${SHUTTER_DECIMAL}s \
            --set-config eosremoterelease="Release Full" \
            --wait-event-and-download=2s \
            --filename "$FILENAME" \
            --force-overwrite \
            ${KEEP} > /dev/null
    if [ $VIEW = true ]; then
      open "$FILENAME" -a preview
    fi
    sleep 1
    t_now=$(date +%s.%N)
    t_diff=$(echo "$t_now - $t_start" | bc -l)
    t_per_image=$(echo "scale=3; $t_diff / $c" | bc -l)
  done
else
  # Otherwise, we can use Manual mode
  gphoto2 --set-config /main/capturesettings/autoexposuremodedial=Manual
  t_start=$(date +%s.%N)
  t_per_image=$(echo "scale=3; 3 + $SHUTTER_DECIMAL" | bc -l)
  for (( c=1; c<=$NUM; c++ ))
  do
    FILENAME=$(next_filename)
    t_left=$(echo "scale=3; $t_per_image * ($NUM - $c + 1)" | bc -l)
    t_left_hr=$(echo "scale=0; $t_left / 3600" | bc -l)
    t_left_min=$(echo "scale=0; ($t_left - $t_left_hr * 3600)/60" | bc -l)
    t_left_sec=$(echo "scale=0; ($t_left - $t_left_hr * 3600 - $t_left_min * 60)" | bc -l)

    # Print status of the form 001/100 t_left: 0.000
    printf "%d / %d Estimated time left: %.0fhr %.0fmin %.0fs\n" $c $NUM $t_left_hr $t_left_min $t_left_sec

    # Capture the image
    gphoto2 --set-config /main/imgsettings/imageformat=RAW \
            --set-config /main/capturesettings/autoexposuremodedial=Manual \
            --set-config iso=$ISO \
            --set-config shutterspeed=$SHUTTER \
            --set-config aperture=$APERTURE \
            --capture-image-and-download --filename "$FILENAME" $KEEP $FORCE \
            > /dev/null
    if [ $VIEW = true ]; then
      open "$FILENAME" -a preview
    fi
    sleep 1
    t_now=$(date +%s.%N)
    t_diff=$(echo "$t_now - $t_start" | bc -l)
    t_per_image=$(echo "scale=3; $t_diff / $c" | bc -l)
    # echo "Time per image: $t_per_image seconds"
    # estimated_time=$(echo "scale=3; $t_diff / $c * ($NUM - $c)" | bc -l)
    # echo "Estimated time remaining: $estimated_time seconds"
  done
fi