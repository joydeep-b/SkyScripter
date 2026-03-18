#!/bin/bash

source ~/.bashrc

usage() {
  echo "Usage: $0 [-i <iso>] [-s <shutter_seconds>] [-n <num_frames>]" >&2
  exit 1
}

ISO=800
SHUTTER=120
NUM=1000
MOUNT_DEVICE="SkyAdventurer GTi"

while getopts ":i:s:n:" opt; do
  case "$opt" in
    i) ISO="$OPTARG" ;;
    s) SHUTTER="$OPTARG" ;;
    n) NUM="$OPTARG" ;;
    *) usage ;;
  esac
done


echo "Using capture parameters: ISO=$ISO, SHUTTER=$SHUTTER, NUM=$NUM"
echo "Using mount device: $MOUNT_DEVICE"

set_param() {
  if ! indi_setprop "$@"; then
    echo "Error: indi_setprop failed for args: $*" >&2
    exit 1
  fi
}

echo "Running startup script"
./setup/bigbend_startup.sh


echo "Setting mount tracking mode to sidereal"
set_param "$MOUNT_DEVICE.TELESCOPE_TRACK_MODE.TRACK_SIDEREAL=On"
echo "Setting mount tracking state to on"
set_param "$MOUNT_DEVICE.TELESCOPE_TRACK_STATE.TRACK_ON=On"
# Start capture job in a screen session called "capture". Reuse the screen session if it already exists.
screen -mdS capture ./capture/batch_capture.sh -i $ISO -s $SHUTTER -n $NUM

python capture/auto_meridian_flip.py -d "$MOUNT_DEVICE"



