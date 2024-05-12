#!/usr/bin/env python3

import os
import sys
import argparse
import subprocess
import time
import tempfile

AUTO_YES = False

def run_siril_script(script, input_dir):
  if sys.platform == 'darwin':
    SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'
  else:
    SIRIL_PATH = '/home/joydeepb/Siril-1.2.1-x86_64.AppImage'
  # Define the command to run
  siril_cli_command = [SIRIL_PATH, "-d", input_dir, "-s", "-"]
  try:
    result = subprocess.run(siril_cli_command, 
                            input=script,
                            text=True, 
                            capture_output=True)
    with open("siril.log", "a") as f:
      f.write("="*80)
      f.write(f"Command: {siril_cli_command}\n")
      f.write("-"*80)
      f.write(f"Script:\n{script}\n")
      f.write("-"*80)
      f.write(f"stdout:\n{result.stdout}\n")
      f.write("-"*80)
      f.write(f"stderr:\n{result.stderr}\n")
      f.write("="*80)
    if result.returncode != 0:
      print("Error running Siril.")
      print(f"stdout:\n{result.stdout}")
      print(f"stderr:\n{result.stderr}")
      # sys.exit(1)
  except subprocess.CalledProcessError as e:
    print(f"Error running Siril: {e}")
    # sys.exit(1)

def delete_with_confirmation(file_glob):
  global AUTO_YES
  if AUTO_YES:
    os.system(f"rm -rf {file_glob}")
    return
  print(f"Deleting all files matching {file_glob}")
  confirmation = ""
  while confirmation not in ["y", "n"]:
    confirmation = input("Proceed? (y/n): ")
    if confirmation not in ["y", "n"]:
      print(f"Please enter 'y' or 'n'.")
  if confirmation == "y":
    os.system(f"rm -rf {file_glob}")

def get_num_light_frames(output_dir):
  # Count the number of light frames in the output directory, matching the
  # pattern "bkg_p_*.fit"
  files = [name for name in os.listdir(output_dir) if (name.startswith("r_bkg_pp_light_") and name.endswith(".fit"))]
  files = [os.path.join(output_dir, f) for f in files]
  files.sort()
  num_light_frames = len(files)
  return num_light_frames, files

def create_sub_stack(dirname, files, outputfile):
  tmpdirname = os.path.join(dirname, "tmp")
  if not os.path.exists(tmpdirname):
    os.makedirs(tmpdirname)
  # remove all files in the temporary directory
  for f in os.listdir(tmpdirname):
    os.remove(os.path.join(tmpdirname, f))
  # print(f"Using temporary directory: {tmpdirname}")
  # Create symbolic links for n r_bkg_pp_light*.fit files from the output_dir
  # to tmpdirname. 
  i = 1
  for f in files:
    os.symlink(f, os.path.join(tmpdirname, f"light_{i:05d}.fit"))
    i += 1
  # List all the files in the temporary directory
  # print("Files in temporary directory:")
  # print(os.listdir(tmpdirname))
  processing_script = f"""requires 1.2.0
register light
stack r_light rej 3 3 -norm=addscale -output_norm -rgb_equal -out={outputfile}
"""
  try:
    run_siril_script(processing_script, tmpdirname)
  except subprocess.CalledProcessError as e:
    print(f"Error running Siril: {e}")


def main():
  dirname = "/Users/joydeepbiswas/Astrophotography/2024-03-28-comet_62p/.process/"
  n = 10
  num_light_frames, files = get_num_light_frames(dirname)
  # for f in files:
  #   print(f"File: {f}")
  # sys.exit(0)

  print(f"Number of light frames: {num_light_frames}")
  
  for i in range(0, num_light_frames - n):
    sub_files = files[i:i+n]
    print(f"Creating stack {i+1:3d} to {i+n:3d} with files:")
    for f in sub_files:
      # Get the base filename
      print(f"{os.path.basename(f)} ", end="")
    print("\n")
    create_sub_stack(dirname, sub_files, os.path.join(dirname, f"stack/stack_{i+1:05d}.fit"))

if __name__ == "__main__":
  main()