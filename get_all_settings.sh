#!/bin/bash

# If gphoto2 is not found, exit
if ! [ -x "$(command -v gphoto2)" ]; then
  echo 'Error: gphoto2 is not installed.' >&2
  exit 1
fi

ALL_SETTINGS=$(gphoto2 --list-config)
echo -e "All settings:\n$ALL_SETTINGS"
echo "============================================="
for setting in $ALL_SETTINGS; do
  echo "============================================="
  echo "Setting: $setting"
  gphoto2 --get-config $setting
  echo "============================================="
done