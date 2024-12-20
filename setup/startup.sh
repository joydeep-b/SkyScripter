#!/bin/bash

SITE_LONGITUDE=262.297595
SITE_LATITUDE=30.266521
SITE_ELEVATION=140.0

# Function to set time and location.
set_time_location() {
  echo "Setting time and location"
  indi_setprop "ZWO AM5.GEOGRAPHIC_COORD.LAT=$SITE_LATITUDE;LONG=$SITE_LONGITUDE;ELEV=$SITE_ELEVATION"
  sleep 1
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

  indi_setprop "ZWO AM5.TIME_UTC.UTC=$CURRENT_UTC_TIME;OFFSET=$UTC_OFFSET_HOURS_DECIMAL"
  # echo "ZWO AM5.TIME_UTC.UTC=$CURRENT_UTC_TIME;OFFSET=$UTC_OFFSET_HOURS_DECIMAL"
}    

INDI_RUNNING=$(pgrep indiserver)
if [ -z "$INDI_RUNNING" ]; then
    echo "Starting INDI server"
    screen -mdS indi indiserver indi_lx200am5 indi_asi_focuser
    sleep 1
    echo "Connecting to mount"
    indi_setprop "ZWO AM5.CONNECTION.CONNECT=On"    
    retcode=$?
    if [ "$retcode" -ne 0 ]; then
        echo "Failed to connect to mount"
        exit 1
    fi
    echo "Mount connected"
    sleep 1
    echo "Connecting to focuser"
    indi_setprop "ASI EAF.CONNECTION.CONNECT=On"
    retcode=$?
    if [ "$retcode" -ne 0 ]; then
        echo "Failed to connect to focuser"
        exit 1
    fi
    echo "Focuser connected"
else
    echo "INDI server already running"
    CONNECTED=$(indi_getprop "ZWO AM5.CONNECTION.CONNECT" | grep -o "CONNECT=On")
    if [ -z "$CONNECTED" ]; then
        echo "Connecting to mount"
        indi_setprop "ZWO AM5.CONNECTION.CONNECT=On"
        retcode=$?
        if [ "$retcode" -ne 0 ]; then
            echo "Failed to connect to mount"
            exit 1
        fi
        echo "Mount connected"
    else
        echo "Mount already connected"
    fi
    CONNECTED=$(indi_getprop "ASI EAF.CONNECTION.CONNECT" | grep -o "CONNECT=On")
    if [ -z "$CONNECTED" ]; then
        echo "Connecting to focuser"
        indi_setprop "ASI EAF.CONNECTION.CONNECT=On"
        retcode=$?
        if [ "$retcode" -ne 0 ]; then
            echo "Failed to connect to focuser"
            exit 1
        fi
        echo "Focuser connected"
    else
        echo "Focuser already connected"
    fi
fi

indi_setprop "ZWO AM5.GUIDE_RATE.RATE=1.0"

# Call the function to set the time and location
set_time_location
sleep 1
./setup/read_site.py