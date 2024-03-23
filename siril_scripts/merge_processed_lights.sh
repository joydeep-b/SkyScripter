#!/bin/bash

# Script parameters: dir1 dir2 [dir3 ...] dir_dest
# Link all pp_light*.fit files from dir1, dir2, ... to dir_dest, numbered sequentially.

# Ensure that at least two input directories, and an output directory are specified.
if [ $# -lt 3 ]; then
    echo "Usage: $0 dir1 dir2 [dir3 ...] dir_dest"
    exit 1
fi

file_filter="bkg_pp_light*.fit"
in_dirs=("$@")
num_in_dirs=${#in_dirs[@]}
num_in_dirs_minus_one=$((num_in_dirs - 1))
dir_dest=${in_dirs[$num_in_dirs_minus_one]}
num_digits=${#num_in_dirs_minus_one}

# Print out the config and ask for confirmation.
echo "Will link all $file_filter files from the following directories:"
for ((i=0; i<$num_in_dirs_minus_one; i++)); do
    echo "    ${in_dirs[$i]}"
done
echo "to $dir_dest, numbered sequentially."
echo "Is this correct? (y/n)"
read answer
if [ "$answer" != "y" ]; then
    echo "Aborting."
    exit 1
fi

# Create the output directory if it doesn't exist.
mkdir -p $dir_dest

file_num=0
for ((i=0; i<$num_in_dirs_minus_one; i++)); do
    in_dir=${in_dirs[$i]}
    for file in $in_dir/$file_filter; do
        # Get absolute path of file.
        file=$(realpath $file)
        # Output file name = pp_light_<file number in NNNNN format>.fit
        link_name=$(printf "pp_light%05d.fit" $file_num)
        # Link the file.
        ln -s $file $dir_dest/$link_name
        # Increment the file number.
        file_num=$((file_num + 1))
    done
done
