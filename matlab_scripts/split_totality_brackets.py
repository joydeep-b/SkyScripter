#!/usr/bin/env python3

import os
import sys
import shutil

def split_totality_brackets(input_directory, 
                            output_directory,
                            start_file, 
                            end_file):
  # Example start file: 7B9A5729.CR3. 
  # Get the start index of the file, in this case 5729.
  start_index = int(start_file.split('.')[0][4:])
  end_index = int(end_file.split('.')[0][4:])
  total_files = end_index - start_index + 1
  
  # There should be 21 files per set.
  # Calculate the number of sets.
  if total_files % 21 != 0:
    print("The number of files is not a multiple of 21, truncating the last set.")
    total_files = total_files - (total_files % 21)
  num_sets = total_files // 21

  for i in range(num_sets):
    subdir_name = f'totality_set_{i+1:02d}'
    os.makedirs(os.path.join(output_directory, subdir_name), exist_ok=True)
    for j in range(21):
      src = os.path.join(input_directory, f'7B9A{start_index:04d}.CR3')
      # Ensure the file exists.
      if not os.path.exists(src):
        print(f"File {src} does not exist.")
        # sys.exit(1)
        continue
      else:
        # Convert to absolute path.
        src = os.path.abspath(src)
      dst = os.path.join(output_directory, subdir_name, f'7B9A{start_index:04d}.CR3')
      # Create a symlink from src to dst.
      os.symlink(src, dst)
      # print(f'ln -s {src} {dst}')
      start_index += 1

if __name__ == "__main__":
  # if len(sys.argv) != 5:
  #   print("Usage: python split_totality_brackets.py <input_directory> <output_directory> <start_file> <end_file>")
  #   sys.exit(1)

  # input_directory = sys.argv[1]
  # output_directory = sys.argv[2]
  # start_file = sys.argv[3]
  # end_file = sys.argv[4]
  input_directory = '../high_speed_bursts/RAW'
  output_directory = '../totality_brackets'
  start_file = '7B9A5729.CR3'
  end_file = '7B9A5980.CR3'
  split_totality_brackets(input_directory, output_directory, start_file, end_file)