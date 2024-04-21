#!/bin/bash

# Script parameters: prefix dir1 dir2 [dir3 ...] dir_dest
# Link all pp_light*.fit files from dir1, dir2, ... to dir_dest, numbered sequentially.

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

file_filter="$prefix*.fit"

# Print out the config and ask for confirmation.
echo "Will link all $file_filter files from the following $num_in_dirs directories:"
for ((i=0; i<$num_in_dirs; i++)); do
    echo "${in_dirs[$i]}"
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
for ((i=0; i<$num_in_dirs; i++)); do
    in_dir=${in_dirs[$i]}
    for file in $in_dir/$file_filter; do
        # Get absolute path of file.
        file=$(realpath $file)
        # Output file name = pp_light_<file number in NNNNN format>.fit
        link_name=$(printf "pp_light_%05d.fit" $file_num)
        # Link the file.
        ln -s $file $dir_dest/$link_name
        # Increment the file number.
        file_num=$((file_num + 1))
    done
done
