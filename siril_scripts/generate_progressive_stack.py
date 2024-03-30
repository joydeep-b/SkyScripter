#!/usr/bin/env python3

import os
import sys
import argparse
import subprocess
import time

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
      sys.exit(1)
  except subprocess.CalledProcessError as e:
    print(f"Error running Siril: {e}")
    sys.exit(1)

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

def run_preprocessing(input_dir, output_dir, dark_master, flat_master):
  print(f"Running preprocessing...")
  t_start = time.time()
  preprocessing_script = f"""requires 1.2.0
convertraw light -out={output_dir}
cd {output_dir}
calibrate light -dark={dark_master} -flat={flat_master} -cc=dark -cfa -debayer
seqsubsky pp_light 2
register bkg_pp_light
"""
  # Run the preprocessing script.
  run_siril_script(preprocessing_script, input_dir)
  t_end = time.time()
  print(f"Preprocessing complete. Time taken: {t_end - t_start:.3f} seconds.")
  
  # Delete intermediate files.
  delete_with_confirmation(f"{output_dir}/light_*.fit*")
  delete_with_confirmation(f"{output_dir}/pp_light_*.fit*")
  delete_with_confirmation(f"{output_dir}/bkg_pp_light_*.fit*")

def get_num_light_frames(output_dir):
  # Count the number of light frames in the output directory, matching the pattern "bkg_p_*.fit"
  num_light_frames = len([name for name in os.listdir(output_dir) if (name.startswith("r_bkg_pp_light_") and name.endswith(".fit"))])
  return num_light_frames

def check_args(args):
  global AUTO_YES
  # Check if the input directory exists
  if not os.path.exists(args.input):
    print(f"Input directory {args.input} does not exist.")
    sys.exit(1)

  # Check if the output directory exists, if not create it.
  if not os.path.exists(args.output):
    print(f"Output directory {args.output} does not exist. Creating it.")
    os.makedirs(args.output)

  # If the dark master is provided, check if it exists.
  if args.dark and not os.path.exists(args.dark):
    print(f"Dark master {args.dark} does not exist.")
    sys.exit(1)

  # If the flat master is provided, check if it exists.
  if args.flat and not os.path.exists(args.flat):
    print(f"Flat master {args.flat} does not exist.")
    sys.exit(1)

  if args.yes:
    AUTO_YES = True
    print("Automatic yes to prompts enabled.")
  # Convert all arguments to absolute paths.
  args.input = os.path.abspath(args.input)
  args.output = os.path.abspath(args.output)
  if args.dark:
    args.dark = os.path.abspath(args.dark)
  else:
    args.dark = "/Users/joydeepbiswas/Astrophotography/masters/master_bias_ISO$ISOSPEED:%d$"
  if args.flat:
    args.flat = os.path.abspath(args.flat)
  else:
    args.flat = "/Users/joydeepbiswas/Astrophotography/masters/master_flat_ISO$ISOSPEED:%d$"

def create_sub_stack(output_dir, sub_stack_size):
  sub_stack_dir = f"{output_dir}/sub_stack_{sub_stack_size}"
  if not os.path.exists(sub_stack_dir):
    os.makedirs(sub_stack_dir)
  delete_with_confirmation(f"{sub_stack_dir}/*")
  t_start = time.time()
  # Create symbolic links for sub_stack_size number of r_bkg_pp_light*.fit
  # files from the output_dir to tmpdirname. 
  lights = [name for name in os.listdir(output_dir) if (name.startswith("r_bkg_pp_light_") and name.endswith(".fit"))]
  lights.sort()
  for i in range(sub_stack_size):
    os.symlink(f"{output_dir}/{lights[i]}", f"{sub_stack_dir}/light_{i+1:05d}.fit")
  sub_stack_fits_file = f"{output_dir}/sub_stack_{sub_stack_size:05d}.fit"
  sub_stack_jpg_file = f"{output_dir}/sub_stack_{sub_stack_size:05d}"
  print(f"Creating sub-stack of size {sub_stack_size}")
  print(f"Output FITS file: {sub_stack_fits_file}")
  print(f"Output JPG file: {sub_stack_jpg_file}.jpg")
  processing_script = f"""requires 1.2.0
register light
stack r_light rej 3 3 -norm=addscale -output_norm -rgb_equal -out={sub_stack_fits_file}
load {sub_stack_fits_file}
fixbanding 1 1 -vertical
fixbanding 1 1 
autostretch
savejpg {sub_stack_jpg_file}
"""
  run_siril_script(processing_script, sub_stack_dir)
  t_end = time.time()
  print(f"Sub-stack creation complete. Time taken: {t_end - t_start:.3f} seconds.")
  # Delete intermediate files.
  delete_with_confirmation(f"{sub_stack_dir}/r_light*.fit*")

def creat_main_stack(output_dir):
  print(f"Creating main stack...")
  t_start = time.time()
  main_stack_fits_file = f"{output_dir}/main_stack.fit"
  main_stack_jpg_file = f"{output_dir}/main_stack"
  print(f"Output FITS file: {main_stack_fits_file}")
  print(f"Output JPG file: {main_stack_jpg_file}.jpg")
  processing_script = f"""requires 1.2.0
stack r_bkg_pp_light rej 3 3 -norm=addscale -output_norm -rgb_equal -filter-fwhm=90% -out={main_stack_fits_file}
load {main_stack_fits_file}
fixbanding 1 1 -vertical
fixbanding 1 1 
save {main_stack_fits_file}
autostretch
savejpg {main_stack_jpg_file}
"""
  run_siril_script(processing_script, output_dir)
  t_end = time.time()
  print(f"Main stack creation complete. Time taken: {t_end - t_start:.3f} seconds.")

def main():
  print("Generating progressive stack...")
  parser = argparse.ArgumentParser(description = 
      "Generate a progressive stack from a directory of raw captures.")

  parser.add_argument("-i", "--input", help="Input directory.", required=True)
  parser.add_argument("-o", "--output", help="Output directory.", required=True)
  parser.add_argument("--dark", help="Dark master.", required=False)
  parser.add_argument("--flat", help="Flat master.", required=False)
  parser.add_argument("-y", "--yes", help="Automatic yes to prompts.", 
                      action="store_true")
  args = parser.parse_args()
  check_args(args)
  print(f"Args: {args}")

  run_preprocessing(args.input, args.output, args.dark, args.flat)
  num_light_frames = get_num_light_frames(args.output)
  print(f"Number of light frames: {num_light_frames}")
  creat_main_stack(args.output)

  return
  sub_stack_size = num_light_frames
  while sub_stack_size > 1:
    create_sub_stack(args.output, sub_stack_size)
    sub_stack_size = sub_stack_size // 2

if __name__ == "__main__":
  main()