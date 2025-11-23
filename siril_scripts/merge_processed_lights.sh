#!/bin/bash

# Script parameters: prefix dir1 dir2 [dir3 ...] dir_dest
# Link all *.fit and *.fits files from dir1, dir2, ... to dir_dest, numbered sequentially as prefix_nnnnn.fits.

# Ensure that at least two input directories, and an output directory are specified.
if [ $# -lt 4 ]; then
    echo "Usage: $0 prefix dir1 dir2 [dir3 ...] dir_dest"
    exit 1
fi

params=("$@")

num_params=${#params[@]}
num_params_minus_one=$((num_params - 1))
dir_dest=${params[$num_params_minus_one]}
prefix=${params[0]}
num_in_dirs=$((num_params - 2))
# in_dirs is an array containing the input directories.
in_dirs=("${params[@]:1:$num_in_dirs}")

# Print out the config and ask for confirmation.
echo "Will link all *.fit and *.fits files from the following $num_in_dirs directories:"
for ((i=0; i<$num_in_dirs; i++)); do
    echo "${in_dirs[$i]}"
done
echo "to $dir_dest as ${prefix}_NNNNN.fits, numbered sequentially."
echo "Is this correct? (y/n)"
read answer
if [ "$answer" != "y" ]; then
    echo "Aborting."
    exit 1
fi

# Create the output directory if it doesn't exist.
mkdir -p $dir_dest

file_num=0
for ((i=0; i<$num_in_dirs; i++)); do
    in_dir=${in_dirs[$i]}
    # Process both .fit and .fits files
    for file in $in_dir/*.fit $in_dir/*.fits; do
        # Skip if the glob pattern didn't match any files
        [ -e "$file" ] || continue
        # Get absolute path of file.
        file=$(realpath $file)
        # Output file name = prefix_<file number in NNNNN format>.fits
        link_name=$(printf "${prefix}_%05d.fits" $file_num)
        # Link the file.
        ln -s $file $dir_dest/$link_name
        # Increment the file number.
        file_num=$((file_num + 1))
    done
done
