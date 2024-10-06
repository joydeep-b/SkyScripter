#!/bin/bash

# Accept two directories as arguments, and a file prefix.
# Move all files from the first directory with the given prefix to the second directory, and
# renumber the suffixes of the files in the second directory to be consecutive.

# Check for the correct number of arguments
if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <source_dir> <dest_dir> <prefix>"
    exit 1
fi
SRC_DIR=$1
DEST_DIR=$2
PREFIX=$3

echo -e "Moving files from:\n$SRC_DIR \n to:\n$DEST_DIR\nwith prefix:\n$PREFIX"
# Ask for confirmation.
read -p "Continue? [y/n] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborting"
    exit 1
fi

# Check that the source directory exists
if [ ! -d "$SRC_DIR" ]; then
    echo "Error: $SRC_DIR does not exist"
    exit 1
fi

# Check that the destination directory exists
if [ ! -d "$DEST_DIR" ]; then
    echo "Error: $DEST_DIR does not exist"
    exit 1
fi

# Get list of files in the source directory with the given prefix.
SRC_FILES=$(ls $SRC_DIR/$PREFIX*)
NUM_SRC_FILES=$(ls $SRC_DIR/$PREFIX* | wc -l)

# Get the number of files in the source directory
NUM_DEST_FILES=$(ls $DEST_DIR/$PREFIX* | wc -l)

NEXT_NUM=$NUM_DEST_FILES

# Move the files from the source directory to the destination directory
for FILE in $SRC_FILES; do
    # Find the next number in the sequence: increment NEXT_NUM until the file does not exist.
    while [ -e "$DEST_DIR/$PREFIX$(printf "%03d" $NEXT_NUM).fits" ]; do
        NEXT_NUM=$((NEXT_NUM + 1))
    done
    # Make new file name: PREFIX_%05d.fits
    NEW_FILE=$(printf "$DEST_DIR/$PREFIX%03d.fits" $NEXT_NUM)
    mv $FILE $NEW_FILE
done

