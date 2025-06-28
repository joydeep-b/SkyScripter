#!/usr/bin/env python3
import argparse
from pathlib import Path
import os
import sys
import subprocess
import re
import json
import platform

STRETCH=True

def get_siril_path():
  """Get the Siril executable path based on the operating system."""
  system = platform.system()
  
  if system == "Darwin":  # macOS
    return '/Applications/Siril.app/Contents/MacOS/Siril'
  elif system == "Linux":
    # Try to find siril using 'which' command
    try:
      result = subprocess.run(['which', 'siril'], 
                              capture_output=True, 
                              text=True, 
                              check=True)
      siril_path = result.stdout.strip()
      if siril_path:
        return siril_path
      else:
        print("Error: Siril not found in PATH. Please install Siril or ensure it's in your PATH.")
        sys.exit(1)
    except subprocess.CalledProcessError:
      print("Error: Siril not found in PATH. Please install Siril or ensure it's in your PATH.")
      sys.exit(1)
  else:
    print(f"Error: Unsupported operating system: {system}")
    sys.exit(1)

# Get the Siril path based on the operating system
SIRIL_PATH = get_siril_path()

def load_config(config_file):
  """Load configuration from JSON file."""
  try:
    with open(config_file, 'r') as f:
      return json.load(f)
  except FileNotFoundError:
    print(f"Error: Config file '{config_file}' not found.")
    sys.exit(1)
  except json.JSONDecodeError as e:
    print(f"Error: Invalid JSON in config file '{config_file}': {e}")
    sys.exit(1)

def get_calibrated_files(indir):
  input_dir = Path(indir)
  calibrated_files = list(input_dir.glob('pp_light_*.fit'))
  calibrated_files.sort()
  # # Print the list of calibrated files.
  # for f in calibrated_files:
  #   print(f)
  return calibrated_files

def get_stack_size(fits_files):
  stack_sizes = [2]
  while 2 * stack_sizes[-1] <= len(fits_files):
    stack_sizes.append(stack_sizes[-1] * 2)
  if stack_sizes[-1] < len(fits_files):
    stack_sizes.append(len(fits_files))
  return stack_sizes

def create_sub_stack(indir, outdir, stack_size):
  # Convert indir to an absolute path.
  indir = os.path.abspath(indir)
  # Ensure that stack_size is <= number of pp_light*.fit files in indir
  num_calibrated_files = len(list(Path(indir).glob('pp_light_*.fit')))
  if stack_size > num_calibrated_files:
    print(f"Warning: Requested stack size {stack_size} is larger than available files {num_calibrated_files}")
    stack_size = num_calibrated_files
  
  # Output file will be stack_nnnn.fit
  output_file = os.path.join(outdir, f"stack_{stack_size:04d}.fit")
  
  # Create a stacking dir, and delete all files in it if it exists.
  stacking_dir = os.path.join(outdir, '.stacking')
  os.makedirs(stacking_dir, exist_ok=True)
  if os.path.exists(stacking_dir):
    os.system(f"rm -rf {stacking_dir}/pp_*")
    os.system(f"rm -rf {stacking_dir}/r_pp_*")
  
  # Copy the first stack_size files from indir to stacking_dir
  pp_light_files = list(Path(indir).glob('pp_light_*.fit'))
  pp_light_files.sort()
  for i in range(0, min(stack_size, len(pp_light_files))):
    # print(f"Linking {pp_light_files[i]} to {os.path.join(stacking_dir, pp_light_files[i].name)}")
    os.symlink(str(pp_light_files[i]), os.path.join(stacking_dir, pp_light_files[i].name))
  
  # Run the registration and stacking script
  stacking_script = f"""requires 1.3.5
register pp_light
stack r_pp_light rej 5 5  -norm=addscale -output_norm -weight=wfwhm -out={output_file}
"""
  siril_cli_command = [SIRIL_PATH, "-d", stacking_dir, "-s", "-"]
  try:
    result = subprocess.run(siril_cli_command,
                            input=stacking_script,
                            text=True,
                            capture_output=True)
    if result.returncode != 0:
      print("Error running Siril.")
      print(f"stdout:\n{result.stdout}")
      print(f"stderr:\n{result.stderr}")
      sys.exit(1)
  except subprocess.CalledProcessError as e:
    print(f"Error running Siril: {e}")
    sys.exit(1)
  pass

def create_starless(stack_sizes, outdir):
  # Create the list of expected stacked images in outdir.
  stack_files = [f"stack_{s:04d}.fit" for s in stack_sizes]
  # # Get list of all "stack_*.fit" files in outdir
  # stack_files = list(Path(outdir).glob('stack_*.fit'))
  # Create a starless directory and delete all files in it if it exists.
  starless_dir = os.path.join(outdir, '.starless')
  os.makedirs(starless_dir, exist_ok=True)
  if os.path.exists(starless_dir):
    os.system(f"rm -rf {starless_dir}/*.fit")
    os.system(f"rm -rf {starless_dir}/*.seq")
  # Symlink all stack files to the starless dir
  for f in stack_files:
    os.symlink(os.path.join(outdir, f), os.path.join(starless_dir, f))
  # Run the starless script for each stack file in the starless dir
  for f in stack_files:
    print(f"Creating starless image for {f}")
    starless_script = f"""requires 1.3.5
load {f}
starnet -stretch -nostarmask
"""
    siril_cli_command = [SIRIL_PATH, "-d", starless_dir, "-s", "-"]
    try:
      result = subprocess.run(siril_cli_command,
                              input=starless_script,
                              text=True,
                              capture_output=True)
      if result.returncode != 0:
        print("Error running Siril.")
        print(f"stdout:\n{result.stdout}")
        print(f"stderr:\n{result.stderr}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
      print(f"Error running Siril: {e}")
      sys.exit(1)

def get_bg(f, config):
  # Get background region from config
  bg_region = config['background_region']
  (x, y, w, h) = (bg_region['x'], bg_region['y'], bg_region['w'], bg_region['h'])
  script = f"""requires 1.3.5
load {f}
boxselect {x} {y} {w} {h}
stat
"""
  siril_cli_command = [SIRIL_PATH, "-d", os.path.dirname(f), "-s", "-"]
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

    m = re.search(r'Mean: ([0-9.]+), Median: ([0-9.]+), Sigma: ([0-9.]+), Min: ([0-9.]+), Max: ([0-9.]+), bgnoise: ([0-9.]+)', result.stdout)
    if m:
      bg = float(m.group(2))
      bgnoise = float(m.group(3))
      # print(f"{f.name}: Background value: {bg}")
    else:
      print(f"Error parsing output of stat command for {f}")
      sys.exit(1)
    return bg, bgnoise
  except subprocess.CalledProcessError as e:
    print(f"Error running Siril: {e}")
    sys.exit(1)

def get_noise_stats(starless_dir, config):
  global STRETCH
  # Get list of all "starless_*.fit" files in starless_dir
  starless_files = list(Path(starless_dir).glob('starless_*.fit'))
  # starless_files = list(Path(starless_dir).glob('stack_*.fit'))
  starless_files.sort()
  # Get noise region from config
  noise_region = config['noise_region']
  (x, y, w, h) = (noise_region['x'], noise_region['y'], noise_region['w'], noise_region['h'])
  # Go through each of them, run a Siril script to select a box with parameters x, y, w, h. Then run
  # the stat command.
  stats = []
  for f in starless_files:
    # Get the stats, stretch the image if required.
    script = f"""requires 1.3.5
load {f}
{"autostretch" if STRETCH else ""}
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
      m = re.search(r'Mean: ([0-9.]+), Median: ([0-9.]+), Sigma: ([0-9.]+), Min: ([0-9.]+), Max: ([0-9.]+), bgnoise: ([0-9.]+)', result.stdout)
      if m:
        mean = float(m.group(1))
        median = float(m.group(2))
        sigma = float(m.group(3))
        min = float(m.group(4))
        max = float(m.group(5))
        # bgnoise = float(m.group(6))
        bg, bgnoise = get_bg(f, config)
        snr = (mean - bg) / sigma
        print(f"{f.name}: Mean: {mean:6.2f}, Median: {median:6.2f}, Sigma: {sigma:6.2f}, Min: {min:6.2f}, Max: {max:6.2f}, bgnoise: {bgnoise:6.2f} bg: {bg:6.2f} SNR: {snr:6.2f}")
      else:
        print(f"Error parsing output of stat command for {f}")
      stats.append((f, mean, median, sigma, min, max, bgnoise, bg, snr))
    except subprocess.CalledProcessError as e:
      print(f"Error running Siril: {e}")
      sys.exit(1)
  return stats

def cleanup(outdir):
  os.system(f"rm -rf {os.path.join(outdir, '.stacking')}")

def plot_stats(stack_sizes, stats, outdir, label):
  # Create a line plot of snr vs. stack size. Display the plot and save it to outdir.
  import matplotlib.pyplot as plt
  import numpy as np
  if len(stack_sizes) != len(stats):
    print(f"Error: stack_sizes and stats have different lengths: {len(stack_sizes)} vs. {len(stats)}")
    return
  # Order of stats: (f, mean, median, sigma, min, max, bgnoise, bg, SNR)
  bgnoise = [s[6] for s in stats]
  noise = [s[3] for s in stats]
  # snr = (mean - bg) / sigma
  snr = [(s[8]) for s in stats]
  # colors = plt.cm.viridis(np.linspace(0, 1, 3))
  colors = plt.cm.tab10(np.arange(3))

  fig, ax1 = plt.subplots()
  ax1.set_xlabel('Stack size')
  ax1.set_ylabel('SNR')
  plt.plot(stack_sizes, snr, marker='x', color=colors[0])

  # Find the best fit for the SNR curve by optimizing the parameters of the curve.
  # The curve is of the form y = a * x^b + c
  # where x is the stack size, y is the SNR, and a, b, c are the parameters to be optimized.
  # The curve is fitted to the data using the least squares method.
  def func(x, a, b, c):
    return a * np.power(x, b) + c
  from scipy.optimize import curve_fit
  popt, pcov = curve_fit(func, stack_sizes, snr)
  # Add a text box with the optimized parameters near the curve.
  plt.text(0.1, 0.2, f'SNR = {popt[0]:.2f} * N^{popt[1]:.2f} + {popt[2]:.2f}', transform=ax1.transAxes, fontsize=9, verticalalignment='top')

  plt.plot(stack_sizes, func(stack_sizes, *popt), 'r-', label=f'fit: a={popt[0]:.2f}, b={popt[1]:.2f}')
  for desired_snr in [10, 12, 14, 16, 18]:
    desired_stack_size = ((desired_snr - popt[2]) / popt[0]) ** (1/popt[1])
    print(f"Desired stack size for SNR={desired_snr:4.1f}: {int(desired_stack_size):5d}")


  ax2 = ax1.twinx()
  ax2.set_ylabel('BG/FG Noise')
  ax2.plot(stack_sizes, bgnoise, marker='x', color=colors[1])
  ax2.plot(stack_sizes, noise, marker='x', color=colors[2])


  # Add a joint legend for both lines
  fig.legend(['SNR', 'SNR Fit', 'BG Noise', 'FG Noise'], loc=[0.7, 0.5])
  plt.grid()
  # plt.plot(stack_sizes, bgnoise, marker='x')
  # for i, txt in enumerate(snr):
  #   plt.annotate(f"{stack_sizes[i]} : {txt:.2f}", (stack_sizes[i]+1, txt+0.125))
  plt.title(f'SNR vs. Stack Size - {label} ({"stretch" if STRETCH else "nostretch"})')
  # Make the x-axis logarithmic of base 2
  # plt.xscale('log', base=2)
  # Save the plot to outdir
  stretch_str = "stretch" if STRETCH else "nostretch"
  plt.savefig(os.path.join(outdir, f"snr_vs_stack_{label}_{stretch_str}.png"))
  # plt.show()

def generate_gif(stack_sizes, outdir):
  # Create a list of the starless files.
  starless_fits = [f"starless_stack_{s:04d}.fit" for s in stack_sizes]
  starless_dir = os.path.join(outdir, '.starless')
  # Use Siril to rescale and save the starless images as PNGs.
  png_files = [os.path.join(starless_dir, f"starless_stack_{s:04d}.png") for s in stack_sizes]
  for f, png_file in zip(starless_fits, png_files):
    png_file_without_ext = os.path.splitext(png_file)[0]
    print(f"Creating PNG file: {png_file}")
    script = f"""requires 1.3.5
load {f}
resample -height=800 -interp=area
autostretch
savepng {png_file_without_ext}
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
    except subprocess.CalledProcessError as e:
      print(f"Error running Siril: {e}")
      sys.exit(1)
  # Use ImageMagick to create a gif from the PNGs.
  # gif_file = os.path.join(outdir, 'starless.gif')
  # convert_command = ['convert'] + png_files + [gif_file]
  gif_file = os.path.join(outdir, 'starless.mov')
  convert_command = ['magick'] + png_files + [gif_file]
  print(f"Convert command: {convert_command}")
  try:
    result = subprocess.run(convert_command,
                            text=True,
                            capture_output=True)
    if result.returncode != 0:
      print("Error running convert.")
      print(f"stdout:\n{result.stdout}")
      print(f"stderr:\n{result.stderr}")
      sys.exit(1)
  except subprocess.CalledProcessError as e:
    print(f"Error running convert: {e}")
    sys.exit(1)

def main():
  global STRETCH
  parser = argparse.ArgumentParser(description='Analyze noise statistics for astrophotography stacks.')
  parser.add_argument('indir', type=str, help='Input directory containing the calibrated pp_light_*.fit files.')
  parser.add_argument('outdir', type=str, help='Output directory.')
  parser.add_argument('-graph', action='store_true', help='Skip processing, just generate the graph.')
  parser.add_argument('-nostretch', action='store_true', help='Do not stretch the starless images.')
  parser.add_argument('-config', type=str, default='noise_stats_config.json', help='Path to config file (default: noise_stats_config.json)')
  args = parser.parse_args()
  if args.nostretch:
    STRETCH = False

  calibrated_files = get_calibrated_files(args.indir)
  stack_sizes = get_stack_size(calibrated_files)

  outdir = Path(args.outdir)
  outdir = outdir.resolve()
  outdir.mkdir(parents=True, exist_ok=True)

  if not args.graph:
    for s in stack_sizes:
      print(f"Creating sub-stack of size {s}")
      create_sub_stack(args.indir, outdir, s)
    create_starless(stack_sizes, outdir)

  config = load_config(args.config)
  stats = get_noise_stats(os.path.join(outdir, '.starless'), config)
  label = os.path.basename(args.indir)
  plot_stats(stack_sizes, stats, outdir, label)
  # generate_gif(stack_sizes, outdir)

  cleanup(outdir)

if __name__ == "__main__":
  main()