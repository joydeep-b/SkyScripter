#!/bin/bash

# Get today's and yesterday' dates in YYYY-MM-DD format
TODAY=$(date +"%Y-%m-%d")
YESTERDAY=$(date -v-1d -v13H '+%Y-%m-%d')

echo "Today is $TODAY"
echo "Yesterday was $YESTERDAY"

mkdir -p ~/Astrophotography/$TODAY/logs
mkdir -p ~/Astrophotography/$YESTERDAY/logs
# exit

# Captured images
rsync -avz astropc:~/sky_scripter/$TODAY ~/Astrophotography
rsync -avz astropc:~/sky_scripter/$YESTERDAY ~/Astrophotography

# SkyScripter logs
rsync -avz astropc:/home/joydeepb/sky_scripter/.logs/*$TODAY* ~/Astrophotography/$TODAY/logs/
rsync -avz astropc:/home/joydeepb/sky_scripter/.logs/*$YESTERDAY* ~/Astrophotography/$YESTERDAY/logs/

# Focus plots
rsync -avz astropc:/home/joydeepb/sky_scripter/.focus/$TODAY*focus_plot.png ~/Astrophotography/$TODAY/logs/
rsync -avz astropc:/home/joydeepb/sky_scripter/.focus/$YESTERDAY*focus_plot.png ~/Astrophotography/$YESTERDAY/logs/

# PHD2 logs
rsync -avz astropc:~/Documents/PHD2/*$TODAY* ~/Astrophotography/$TODAY/
rsync -avz astropc:~/Documents/PHD2/*$YESTERDAY* ~/Astrophotography/$YESTERDAY/
