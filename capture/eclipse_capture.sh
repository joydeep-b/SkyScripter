#!/bin/bash
set -e

# Choice: 7 RAW + Large Fine JPEG
echo "Image format: RAW + Large Fine JPEG"
gphoto2 --set-config "/main/imgsettings/imageformat=7"

# Bracketing: 7 shots: -3, -2, -1, 0, +1, +2, +3
echo "Bracketing: 7 shots: -3, -2, -1, 0, +1, +2, +3"
gphoto2 --set-config "/main/capturesettings/aeb=3"

# ISO: 100
echo "ISO: 100"
gphoto2 --set-config "/main/imgsettings/iso=100"

# Drive mode: Continuous high speed
echo "Drive mode: Continuous high speed"
gphoto2 --set-config "/main/capturesettings/drivemode=2"

# Do while loop to execute indefinitely.
COUNT=0
NUM_IMAGES=0
while true; do
  echo "Count: $COUNT, Num images: $NUM_IMAGES"

  # First set: nominal shutter speed = 1/250
  echo "First set: nominal shutter speed = 1/250" 
  gphoto2 --set-config "/main/capturesettings/shutterspeed=1/250"
  SHUTTER_DECIMAL="2"
  gphoto2 --set-config eosremoterelease=Immediate \
          --wait-event=${SHUTTER_DECIMAL}s \
          --set-config eosremoterelease="Release Full" > /dev/null
  NUM_IMAGES=$((NUM_IMAGES + 7))

  # Second set: nominal shutter speed = 1/4
  echo "Second set: nominal shutter speed = 1/4" 
  gphoto2 --set-config "/main/capturesettings/shutterspeed=1/4"
  SHUTTER_DECIMAL="8"
  gphoto2 --set-config eosremoterelease=Immediate \
          --wait-event=${SHUTTER_DECIMAL}s \
          --set-config eosremoterelease="Release Full" > /dev/null
  NUM_IMAGES=$((NUM_IMAGES + 7))


  # Third set: nominal shutter speed = 1/100
  echo "Second set: nominal shutter speed = 1/100" 
  gphoto2 --set-config "/main/capturesettings/shutterspeed=1/100"
  SHUTTER_DECIMAL="2"
  gphoto2 --set-config eosremoterelease=Immediate \
          --wait-event=${SHUTTER_DECIMAL}s \
          --set-config eosremoterelease="Release Full" > /dev/null
  NUM_IMAGES=$((NUM_IMAGES + 7))

  COUNT=$((COUNT + 1))
done

echo "Count: $COUNT, Num images: $NUM_IMAGES"