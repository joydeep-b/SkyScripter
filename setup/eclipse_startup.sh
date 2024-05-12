#!/bin/bash

SITE_LONGITUDE=262.297595
SITE_LATITUDE=30.266521
SITE_ELEVATION=140.0

# Function to set time and location.
set_time_location() {
  echo "Setting time and location"
  indi_setprop "SkyAdventurer GTi.GEOGRAPHIC_COORD.LAT=$SITE_LATITUDE;LONG=$SITE_LONGITUDE;ELEV=$SITE_ELEVATION"
  # Get the current time in UTC in format 2024-01-21T18:38:23
  CURRENT_UTC_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S")
  echo "Current UTC time: $CURRENT_UTC_TIME"
  # indi_setprop "SkyAdventurer GTi.TIME_UTC.UTC=$CURRENT_UTC_TIME"
}    

INDI_RUNNING=$(pgrep indiserver)
if [ -z "$INDI_RUNNING" ]; then
    echo "Starting INDI server"
    screen -mdS indi /Applications/kstars.app/Contents/MacOS/indiserver /Applications/kstars.app/Contents/MacOS/indi_skyadventurergti_telescope /Applications/kstars.app/Contents/MacOS/indi_asi_focuser
    sleep 1
    echo "Connecting to mount"
    indi_setprop "SkyAdventurer GTi.CONNECTION.CONNECT=On"    
    retcode=$?
    if [ "$retcode" -ne 0 ]; then
        echo "Failed to connect to mount"
        exit 1
    fi
    echo "Mount connected"
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
    CONNECTED=$(indi_getprop "SkyAdventurer GTi.CONNECTION.CONNECT" | grep -o "CONNECT=On")
    if [ -z "$CONNECTED" ]; then
        echo "Connecting to mount"
        indi_setprop "SkyAdventurer GTi.CONNECTION.CONNECT=On"
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

indi_setprop "SkyAdventurer GTi.GUIDE_RATE.GUIDE_RATE_WE=1.0"
indi_setprop "SkyAdventurer GTi.GUIDE_RATE.GUIDE_RATE_NS=1.0"

# Call the function to set the time and location
set_time_location
sleep 1
./setup/read_site.py