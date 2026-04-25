#!/bin/bash
source ~/.bashrc
# INDI telescope longitude is east-positive in the 0..360 range.
# Values taken from https://status.starfront.space/:
# At Lat / Long: 31.5475 / -99.38194444
SITE_LONGITUDE=260.61805556
SITE_LATITUDE=31.5475
SITE_ELEVATION=140.0
MOUNT_DEVICE="ZWO AM5"

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
  # Get the current time in UTC in format 2024-01-21T18:38:23
  CURRENT_UTC_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S")
  echo "Current UTC time: $CURRENT_UTC_TIME"
  UTC_OFFSET_HOURS_DECIMAL=$(python3 - <<'PY'
from datetime import datetime

offset = datetime.now().astimezone().utcoffset()
print(f"{offset.total_seconds() / 3600:.1f}")
PY
)
  echo "UTC offset: $UTC_OFFSET_HOURS_DECIMAL"

  # Repeat up to 10 times until INDI confirms the new values.
  for i in {1..10}
  do
    indi_setprop "$MOUNT_DEVICE.GEOGRAPHIC_COORD.LAT=$SITE_LATITUDE;LONG=$SITE_LONGITUDE;ELEV=$SITE_ELEVATION"
    indi_setprop "$MOUNT_DEVICE.TIME_UTC.UTC=$CURRENT_UTC_TIME;OFFSET=$UTC_OFFSET_HOURS_DECIMAL"
    if verify_time_location; then
        break
    fi
    echo "Failed to verify time and location. Retrying..."
    CURRENT_UTC_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S")
    sleep 1
  done
  if ! verify_time_location; then
      echo "Failed to set time and location"
      exit 1
  fi
}

read_indi_value() {
  indi_getprop -1 "$1"
}

verify_time_location() {
  local time_state location_state site_utc actual_lat actual_lon actual_elev

  time_state=$(read_indi_value "$MOUNT_DEVICE.TIME_UTC._STATE" 2>/dev/null) || return 1
  location_state=$(read_indi_value "$MOUNT_DEVICE.GEOGRAPHIC_COORD._STATE" 2>/dev/null) || return 1
  if [ "$time_state" = "Alert" ] || [ "$location_state" = "Alert" ]; then
    return 1
  fi

  site_utc=$(read_indi_value "$MOUNT_DEVICE.TIME_UTC.UTC" 2>/dev/null) || return 1
  actual_lat=$(read_indi_value "$MOUNT_DEVICE.GEOGRAPHIC_COORD.LAT" 2>/dev/null) || return 1
  actual_lon=$(read_indi_value "$MOUNT_DEVICE.GEOGRAPHIC_COORD.LONG" 2>/dev/null) || return 1
  actual_elev=$(read_indi_value "$MOUNT_DEVICE.GEOGRAPHIC_COORD.ELEV" 2>/dev/null) || return 1

  python3 - "$site_utc" "$SITE_LATITUDE" "$SITE_LONGITUDE" "$SITE_ELEVATION" \
      "$actual_lat" "$actual_lon" "$actual_elev" <<'PY'
from datetime import datetime, timezone
import sys

site_utc, target_lat, target_lon, target_elev, actual_lat, actual_lon, actual_elev = sys.argv[1:]
site_dt = datetime.fromisoformat(site_utc.replace("Z", "+00:00"))
if site_dt.tzinfo is None:
    site_dt = site_dt.replace(tzinfo=timezone.utc)
time_delta = abs((datetime.now(timezone.utc) - site_dt.astimezone(timezone.utc)).total_seconds())

def angle_delta(a, b):
    return abs((a - b + 180) % 360 - 180)

location_ok = (
    abs(float(actual_lat) - float(target_lat)) < 0.01
    and angle_delta(float(actual_lon), float(target_lon)) < 0.01
    and abs(float(actual_elev) - float(target_elev)) < 1.0
)
sys.exit(0 if time_delta <= 10 and location_ok else 1)
PY
}

# Connect to mount
connect_mount() {
  echo "Connecting to mount"
  indi_setprop "$MOUNT_DEVICE.CONNECTION.CONNECT=On"
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

indi_setprop "$MOUNT_DEVICE.GUIDE_RATE.RATE=1.0"

# Call the function to set the time and location
set_time_location
sleep 1
./setup/read_site.py
