#!/bin/bash
source ~/.bashrc
SITE_LONGITUDE=360-97.702354
SITE_LATITUDE=30.266485
SITE_ELEVATION=140.0

./power.sh on

echo "Waiting for camera to initialize..."
for i in $(seq 30 -1 1); do
  printf "\rStarting in %2ds..." "$i"
  sleep 1
done
echo -e "\rCamera initialization wait complete.              "
echo -e "\nQHY and ZWO devices:"
lsusb | grep "QHY"
lsusb | grep "ZWO"

# Function to set time and location.
set_time_location() {
  echo "Setting time and location"
  indi_setprop "ZWO AM5.GEOGRAPHIC_COORD.LAT=$SITE_LATITUDE;LONG=$SITE_LONGITUDE;ELEV=$SITE_ELEVATION"
  # sleep 3
  # Get the current time in UTC in format 2024-01-21T18:38:23
  CURRENT_UTC_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S")
  echo "Current UTC time: $CURRENT_UTC_TIME"
  # Get local UTC offset in format -5
  UTC_OFFSET=$(date +"%z")
  # Convert UTC offset from format -0530 to -5.5
  UTC_OFFSET_HOURS=$(echo $UTC_OFFSET | cut -c1-3)
  UTC_OFFSET_MINUTES=$(echo $UTC_OFFSET | cut -c4-5)
  UTC_OFFSET_HOURS_DECIMAL=$(echo "scale=1; $UTC_OFFSET_HOURS + $UTC_OFFSET_MINUTES / 60" | bc)
  echo "UTC offset: $UTC_OFFSET_HOURS_DECIMAL"

  # Repeat Up to 10 times until there is no error code.
  for i in {1..10}
  do
    indi_setprop "ZWO AM5.TIME_UTC.UTC=$CURRENT_UTC_TIME;OFFSET=$UTC_OFFSET_HOURS_DECIMAL"
    retcode=$?
    if [ "$retcode" -eq 0 ]; then
        break
    fi
    echo "Failed to set time and location. Retrying..."
    sleep 1
  done
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to set time and location"
      exit 1
  fi
}

# Connect to mount
connect_mount() {
  echo "Connecting to mount"
  indi_setprop "ZWO AM5.CONNECTION.CONNECT=On"
  retcode=$?
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to connect to mount"
      exit 1
  fi
  echo "Mount connected"
}

# Connect to focuser
connect_focuser() {
  echo "Connecting to focuser"
  indi_setprop "ZWO EAF.CONNECTION.CONNECT=On"
  retcode=$?
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to connect to focuser"
      exit 1
  fi
  echo "Focuser connected"
}

# Connect both the imaging (QHY 268M) and guiding (ASI 120MM) cameras.
connect_cameras() {
  echo "Connecting to QHY 268M camera"
  indi_setprop "QHY CCD QHY268M.CONNECTION.CONNECT=On"
  retcode=$?
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to connect to camera"
      exit 1
  fi
  sleep 2
  indi_setprop "QHY CCD QHY268M.ACTIVE_DEVICES.ACTIVE_TELESCOPE=ZWO AM5"
  retcode=$?
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to set active telescope"
      exit 1
  fi
  indi_setprop "QHY CCD QHY268M.ACTIVE_DEVICES.ACTIVE_FILTER=QHY CCD QHY268M"
  retcode=$?
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to set active filter"
      exit 1
  fi
  indi_setprop "QHY CCD QHY268M.ACTIVE_DEVICES.ACTIVE_FOCUSER=ZWO EAF"
  retcode=$?
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to set active focuser"
      exit 1
  fi
  echo "Connecting to PlayerOne Sedna-M"
  indi_setprop "PlayerOne CCD Sedna-M.CONNECTION.CONNECT=On"
  retcode=$?
  if [ "$retcode" -ne 0 ]; then
      echo "Failed to connect to PlayerOne Sedna-M"
      exit 1
  fi
  echo "Cameras connected"
}

INDI_RUNNING=$(pgrep indiserver)
if [ -z "$INDI_RUNNING" ]; then
    echo "Starting INDI server"
    screen -mdS indi indiserver indi_lx200am5 indi_asi_focuser indi_qhy_ccd indi_playerone_ccd
    sleep 1
else
    echo "INDI server already running"
fi
connect_mount
connect_focuser
connect_cameras

indi_setprop "ZWO AM5.GUIDE_RATE.RATE=1.0"

# Call the function to set the time and location
set_time_location
sleep 1
./setup/read_site.py
