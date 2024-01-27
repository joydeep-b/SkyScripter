#!/bin/bash
set -e

# If gphoto2 is not found, exit
if ! [ -x "$(command -v gphoto2)" ]; then
  echo 'Error: gphoto2 is not installed.' >&2
  exit 1
fi

ISO=100
SHUTTER=1/100
HELP=false
NUM=1
VIEW=false

# Read the options.
while getopts i:s:n:h option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    n) NUM=${OPTARG};;
    h) HELP=true;;
  esac
done

# Print the help message
if [ "$HELP" = true ]; then
  echo "Usage: batch_capture.sh [-i ISO] [-a APERTURE] [-s SHUTTER] [-n NUM]"
  echo "  -i ISO: The ISO of the image (default: 100)"
  echo "  -s SHUTTER: The shutter speed of the image (default: 1/100)"
  echo "  -n: The number of images to capture (default: 1)"
  echo "  -h: Print this help message"
  echo -e "\nDetected cameras:"
  gphoto2 --auto-detect
  exit 0
fi

echo "Capturing $NUM images to camera card with ISO=$ISO and shutter=$SHUTTER"

# Helper function to print time left
print_time_left() {
  t_left_hr=$(echo "scale=0; $t_left / 3600" | bc -l)
  t_left_min=$(echo "scale=0; ($t_left - $t_left_hr * 3600)/60" | bc -l)
  t_left_sec=$(echo "scale=0; ($t_left - $t_left_hr * 3600 - $t_left_min * 60)" | bc -l)

  # Compute estimated time of completion.
  t_now=$(date +%s)
  t_complete=$(echo "scale=0; $t_now + $t_left" | bc -l | awk '{print int($1)}')
  if [ "$(uname)" == "Darwin" ]; then
    t_complete_hr=$(date -r $t_complete +%H | sed 's/^0*//')
    t_complete_min=$(date -r $t_complete +%M | sed 's/^0*//')
    t_complete_sec=$(date -r $t_complete +%S | sed 's/^0*//')
  else
    t_complete_hr=$(date -d @$t_complete +%H | sed 's/^0*//')
    t_complete_min=$(date -d @$t_complete +%M | sed 's/^0*//')
    t_complete_sec=$(date -d @$t_complete +%S | sed 's/^0*//')
  fi

  # Print status of the form 001/100 t_left: 0.000
  printf "%3d / %3d Time left: %2.0fh %2.0fm %2.0fs;" \
         $c $NUM $t_left_hr $t_left_min $t_left_sec
  printf " Completion time: %02d:%02d:%02d;\n" \
         $t_complete_hr $t_complete_min $t_complete_sec
}

gphoto2 --set-config /main/capturesettings/autoexposuremodedial=Manual \
        --set-config /main/imgsettings/imageformat=RAW \
        --set-config iso=$ISO \
        --set-config shutterspeed=$SHUTTER

t_start=$(date +%s.%N)
SHUTTER_DECIMAL=$(echo "scale=3; $SHUTTER" | bc)
t_per_image=$(echo "scale=3; 1.5 + $SHUTTER_DECIMAL" | bc -l)
for (( c=1; c<=$NUM; c++ ))
do
  t_left=$(echo "scale=3; $t_per_image * ($NUM - $c + 1)" | bc -l)
  print_time_left

  gphoto2 --capture-image > /dev/null
  
  t_now=$(date +%s.%N)
  t_diff=$(echo "$t_now - $t_start" | bc -l)
  t_per_image=$(echo "scale=3; $t_diff / $c" | bc -l)
done