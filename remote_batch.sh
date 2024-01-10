#!/bin/bash

set -e

HOST=astropc
ISO=800
APERTURE=8
SHUTTER=60
NUM=1
REMOTE_ASTRO_GPHOTO_DIR=~/astro_gphoto
REMOTE_DIR=$(date +%Y-%m-%d)
LOCAL_DIR=~/Astrophotography/$(date +%Y-%m-%d)
HELP=false

# Check command line arguments to override defaults.
while getopts i:s:a:c:r:l:g:h option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    a) APERTURE=${OPTARG};;
    c) HOST=${OPTARG};;
    r) REMOTE_DIR=${OPTARG};;
    l) LOCAL_DIR=${OPTARG};;
    g) REMOTE_ASTRO_GPHOTO_DIR=${OPTARG};;
    h) HELP=true;;
  esac
done

# Print the help message
if [ "$HELP" = true ]; then
  echo "Usage: remote_batch.sh [-i ISO] [-s SHUTTER] [-a APERTURE] [-c HOST] [-r REMOTE_DIR] [-l LOCAL_DIR]"
  echo "  -i ISO: The ISO of the image (default: 800)"
  echo "  -s SHUTTER: The shutter speed of the image (default: 60)"
  echo "  -a APERTURE: The aperture of the image (default: 8)"
  echo "  -c HOST: The hostname of the remote machine (default: astropc)"
  echo "  -r REMOTE_DIR: The directory to save the images on the remote machine (default: images)"
  echo "  -l LOCAL_DIR: The directory to save the images on the local machine (default: ~/Astrophotography/images)"
  echo "  -g REMOTE_ASTRO_GPHOTO_DIR: The directory of the astro_gphoto repository on the remote machine (default: ~/astro_gphoto)"
  echo "  -h: Print this help message"
  echo "Example: remote_batch.sh -i 800 -s 60 -a 8 -c astropc -r $(date +%Y-%m-%d)/seq01 -l ~/Astrophotography/$(date +%Y-%m-%d)"
  exit 0
fi
ssh $HOST "cd $REMOTE_ASTRO_GPHOTO_DIR; ./batch_capture.sh -i $ISO -s $SHUTTER -a $APERTURE -n $NUM -d $REMOTE_DIR"

rsync -avz $HOST:$REMOTE_ASTRO_GPHOTO_DIR/$REMOTE_DIR $LOCAL_DIR