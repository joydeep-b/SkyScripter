#!/usr/bin/env python3
import argparse
from pathlib import Path
import os
import sys
import subprocess

SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'

def get_fits_files(indir):
  input_dir = Path(indir)
  fits_files = list(input_dir.glob('*.fits'))
  fits_files.sort()
  # # Print the list of fits files.
  # for f in fits_files:
  #   print(f)
  return fits_files

def get_stack_size(fits_files):
  stack_sizes = [2]
  while 2 * stack_sizes[-1] <= len(fits_files):
    stack_sizes.append(stack_sizes[-1] * 2)
  if stack_sizes[-1] < len(fits_files):
    stack_sizes.append(len(fits_files))
  return stack_sizes

def calibrate_images(indir, process_dir):
  calibration_script = f"""requires 1.3.5
convert light -out={process_dir}
cd {process_dir}
calibrate light -dark=$defdark -flat=$defflat -cc=dark
register pp_light -2pass
seqapplyreg pp_light -drizzle -scale=1 -pixfrac=0.9 -framing=min
"""
  siril_cli_command = [SIRIL_PATH, "-d", indir, "-s", "-"]
  try:
    result = subprocess.run(siril_cli_command,
                            input=calibration_script,
                            text=True)
    if result.returncode != 0:
      print("Error running Siril.")
      print(f"stdout:\n{result.stdout}")
      print(f"stderr:\n{result.stderr}")
      sys.exit(1)
  except subprocess.CalledProcessError as e:
    print(f"Error running Siril: {e}")
    sys.exit(1)
  pass

def create_sub_stack(process_dir, outdir, stack_size):
  # Ensure that stack_size is <= number of r_pp*.fit files in process_dir
  num_processed_files = len(list(Path(process_dir).glob('r_pp*.fit')))
  # Output file will be stack_nnnn.fit
  output_file = os.path.join(outdir, f"stack_{stack_size:04d}.fit")
  # Create a stacking dir, and delete all files in it if it exists.
  stacking_dir = os.path.join(outdir, '.stacking')
  os.makedirs(stacking_dir, exist_ok=True)
  if os.path.exists(stacking_dir):
    os.system(f"rm -rf {stacking_dir}/r_*")
  # Copy the first stack_size files from process_dir to stacking_dir
  r_pp_files = list(Path(process_dir).glob('r_pp*.fit'))
  r_pp_files.sort()
  for i in range(stack_size):
    # Make a symlink to the file in the stacking dir
    os.symlink(r_pp_files[i], os.path.join(stacking_dir, r_pp_files[i].name))
  # Run the stacking script
  stacking_script = f"""requires 1.3.5
register r_pp_light
stack r_r_pp_light rej 5 5  -norm=addscale -output_norm -weight=wfwhm -out={output_file}
"""
  siril_cli_command = [SIRIL_PATH, "-d", stacking_dir, "-s", "-"]
  try:
    result = subprocess.run(siril_cli_command,
                            input=stacking_script,
                            text=True)
    if result.returncode != 0:
      print("Error running Siril.")
      print(f"stdout:\n{result.stdout}")
      print(f"stderr:\n{result.stderr}")
      sys.exit(1)
  except subprocess.CalledProcessError as e:
    print(f"Error running Siril: {e}")
    sys.exit(1)
  pass

def create_starless(outdir):
  # Get list of all "stack_*.fit" files in outdir
  stack_files = list(Path(outdir).glob('stack_*.fit'))
  # Create a starless directory and delete all files in it if it exists.
  starless_dir = os.path.join(outdir, '.starless')
  os.makedirs(starless_dir, exist_ok=True)
  if os.path.exists(starless_dir):
    os.system(f"rm -rf {starless_dir}/*.fit")
    os.system(f"rm -rf {starless_dir}/*.seq")
  # Symlink all stack files to the starless dir
  for f in stack_files:
    os.symlink(f, os.path.join(starless_dir, f.name))
  # Run the starless script for each stack file in the starless dir
  for f in stack_files:
    starless_script = f"""requires 1.3.5
load {f}
starnet -stretch -nostarmask
"""
    siril_cli_command = [SIRIL_PATH, "-d", starless_dir, "-s", "-"]
    try:
      result = subprocess.run(siril_cli_command,
                              input=starless_script,
                              text=True)
      if result.returncode != 0:
        print("Error running Siril.")
        print(f"stdout:\n{result.stdout}")
        print(f"stderr:\n{result.stderr}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
      print(f"Error running Siril: {e}")
      sys.exit(1)

def get_noise_stats(starless_dir):
  # Get list of all "starless_*.fit" files in starless_dir
  starless_files = list(Path(starless_dir).glob('starless_*.fit'))
  starless_files.sort()
  # (x, y, w, h) = (5013, 3129, 462, 298)
  # (x, y, w, h) = (5013, 3129, 16, 16)
  (x, y, w, h) = (3216, 1338, 108, 57)
  # Go through each of them, run a Siril script to select a box with parameters x, y, w, h. Then run
  # the stat command.
  stats = []
  for f in starless_files:
    script = f"""requires 1.3.5
load {f}
autostretch
bg
boxselect {x} {y} {w} {h}
stat
"""
    siril_cli_command = [SIRIL_PATH, "-d", starless_dir, "-s", "-"]
    try:
      result = subprocess.run(siril_cli_command,
                              input=script,
                              text=True,
                              capture_output=True)
      if result.returncode != 0:
        print("Error running Siril.")
        print(f"stdout:\n{result.stdout}")
        print(f"stderr:\n{result.stderr}")
        sys.exit(1)
      # Parse the output of the stat command. Look for a string like "Mean: 13.6, Median: 13.6,
      # Sigma: 1.7, Min: 8.0, Max: 22.2, bgnoise: 1.5" and extract the values.
      import re
      m = re.search(r'Mean: ([0-9.]+), Median: ([0-9.]+), Sigma: ([0-9.]+), Min: ([0-9.]+), Max: ([0-9.]+), bgnoise: ([0-9.]+)', result.stdout)
      if m:
        mean = float(m.group(1))
        median = float(m.group(2))
        sigma = float(m.group(3))
        min = float(m.group(4))
        max = float(m.group(5))
        bgnoise = float(m.group(6))
        print(f"{f.name}: Mean: {mean:5.2f}, Median: {median}, Sigma: {sigma}, Min: {min}, Max: {max}, bgnoise: {bgnoise}")
      else:
        print(f"Error parsing output of stat command for {f}")
      # Now find the bg value. It'll be in the format "Background value (channel: #0): 22 "
      m = re.search(r'Background value \(channel: #0\): ([0-9.]+)', result.stdout)
      if m:
        bg = float(m.group(1))
        # print(f"{f.name}: Background value: {bg}")
      stats.append((f, mean, median, sigma, min, max, bgnoise, bg))
    except subprocess.CalledProcessError as e:
      print(f"Error running Siril: {e}")
      sys.exit(1)
  return stats

def cleanup(outdir):
  os.system(f"rm -rf {os.path.join(outdir, '.process')}")
  os.system(f"rm -rf {os.path.join(outdir, '.stacking')}")

def plot_stats(stack_sizes, stats, outdir):
  # Create a line plot of snr vs. stack size. Display the plot and save it to outdir.
  import matplotlib.pyplot as plt
  import numpy as np
  # Order of stats: (f, mean, median, sigma, min, max, bgnoise, bg)
  bgnoise = [s[6] for s in stats]
  # SNR = (mean - min) / bgnoise
  # snr = [(s[2] - s[7]) / s[6] for s in stats]
  snr = [s[2] / s[6] for s in stats]
  fig, ax1 = plt.subplots()
  ax1.set_xlabel('Stack size')
  ax1.set_ylabel('Background noise')
  plt.plot(stack_sizes, bgnoise, marker='x', color='blue')
  ax2 = ax1.twinx()
  ax2.set_ylabel('SNR')
  ax2.plot(stack_sizes, snr, marker='x', color='red')
  # Add a joint legend for both lines
  fig.legend(['Background noise', 'SNR'], loc='upper right')
  plt.grid()
  # plt.plot(stack_sizes, bgnoise, marker='x')
  # for i, txt in enumerate(bgnoise):
  #   plt.annotate(f"{stack_sizes[i]} : {txt:.2f}", (stack_sizes[i]+1, txt+0.125))



  # Make the x-axis logarithmic of base 2
  plt.xscale('log', base=2)
  # Save the plot to outdir
  plt.savefig(os.path.join(outdir, 'noise_plot.png'))
  # plt.show()

def main():
  parser = argparse.ArgumentParser(description='Create a csv file with astrophotography session information.')
  parser.add_argument('indir', type=str, help='Input directory containing the fits files.')
  parser.add_argument('outdir', type=str, help='Output directory.')
  parser.add_argument('-graph', action='store_true', help='Skip processing, just generate the graph.')
  args = parser.parse_args()

  fits_files = get_fits_files(args.indir)
  stack_sizes = get_stack_size(fits_files)

  outdir = Path(args.outdir)
  outdir = outdir.resolve()
  outdir.mkdir(parents=True, exist_ok=True)

  process_dir = os.path.join(outdir, '.process')
  process_dir = os.path.abspath(process_dir)
  os.makedirs(process_dir, exist_ok=True)
  print(f"Processing directory: {process_dir}")

  if not args.graph:
    calibrate_images(args.indir, process_dir)

    for s in stack_sizes:
      print(f"Creating sub-stack of size {s}")
      create_sub_stack(process_dir, outdir, s)

    create_starless(outdir)

  stats = get_noise_stats(os.path.join(outdir, '.starless'))
  plot_stats(stack_sizes, stats, outdir)
  cleanup(outdir)

if __name__ == "__main__":
  main()