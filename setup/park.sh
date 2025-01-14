#!/bin/bash

SITE_LONGITUDE=262.297595
SITE_LATITUDE=30.266521
SITE_ELEVATION=140.0

INDI_RUNNING=$(pgrep indiserver)
if [ -z "$INDI_RUNNING" ]; then
    echo "Starting INDI server"
    screen -mdS indi indiserver indi_lx200am5
    sleep 1
    echo "Connecting to mount"
    indi_setprop "ZWO AM5.CONNECTION.CONNECT=On"
    retcode=$?
    if [ "$retcode" -ne 0 ]; then
        echo "Failed to connect to mount"
        exit 1
    fi
    echo "Mount connected"
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
fi

indi_setprop "ZWO AM5.TELESCOPE_PARK.PARK=On"