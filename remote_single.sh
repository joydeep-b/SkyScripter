#!/bin/bash

set -e

HOST=astropc
ISO=12800
SHUTTER=2
APERTURE=8

# Check command line arguments to override defaults.
while getopts i:s:a:h: option; do
  case "${option}" in
    i) ISO=${OPTARG};;
    s) SHUTTER=${OPTARG};;
    a) APERTURE=${OPTARG};;
    h) HOST=${OPTARG};;
  esac
done

ssh $HOST "cd ~/astro_gphoto; ./single_capture.sh -i $ISO -s $SHUTTER -a $APERTURE -f /tmp/capture.CR3"

scp $HOST:/tmp/capture.CR3 ./remote_capture.CR3

open ./remote_capture.CR3 -a preview

