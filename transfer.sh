#!/bin/bash

# Configuration variables
REMOTE_SRC_DIR="/home/joydeepb/Pictures"
LOCAL_DEST_DIR="/Users/joydeepbiswas/Astrophotography"

# Compute the basename of the remote source directory (e.g., "Pictures")
SRC_BASENAME=$(basename "$REMOTE_SRC_DIR")

# Get list of new top-level directories with files modified in the last 24 hours
new_dirs=$(ssh astropc "find ${REMOTE_SRC_DIR} -type f -mmin -1440 -printf '%h\n' | sed -E \"s#^/[^/]+/[^/]+/${SRC_BASENAME}/([^/]+).*#\1#\" | grep -v '^$' | sort -u")

# Check if any new directories were found
if [ -z "$new_dirs" ]; then
    echo "No new directories found in ${REMOTE_SRC_DIR}."
    exit 0
fi

# Display the list of new directories with separators
echo "New directories found:"
echo "-----------------------"
echo "$new_dirs"
echo "-----------------------"

# Print the rsync commands that will be executed
echo "The following rsync commands will be executed:"
echo "========================"
for dir in $new_dirs; do
    echo "rsync -av astropc:\"${REMOTE_SRC_DIR}/$dir\" \"${LOCAL_DEST_DIR}/\""
done
echo "========================"

# Prompt for confirmation (default = Y)
read -r -p "Do you want to execute these commands? (Y/n) " response
response=${response:-Y}

if [[ $response =~ ^[Yy] ]]; then
    # Ensure the local target directory exists
    mkdir -p "${LOCAL_DEST_DIR}"
    # Loop through each directory and rsync from the remote host
    for dir in $new_dirs; do
        echo "Downloading directory: $dir"
        rsync -av astropc:"${REMOTE_SRC_DIR}/$dir" "${LOCAL_DEST_DIR}/"
    done
    echo "Download completed."
else
    echo "Download canceled."
fi
