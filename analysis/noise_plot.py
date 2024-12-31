#!/usr/bin/env python3
import argparse
from pathlib import Path
import os
import sys
import subprocess
import re

SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'
STRETCH=True

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
  print(f"Calibrating images in {indir}")
  calibration_script = f"""requires 1.3.5
convert light -out={process_dir}
cd {process_dir}
calibrate light -dark=$defdark -flat=$defflat -cc=dark
register pp_light -2pass
# seqapplyreg pp_light -drizzle -scale=1 -pixfrac=0.9 -framing=min
seqapplyreg pp_light -drizzle -scale=1 -pixfrac=0.9 -framing=cog
"""
  siril_cli_command = [SIRIL_PATH, "-d", indir, "-s", "-"]
  try:
    result = subprocess.run(siril_cli_command,
                            input=calibration_script,
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
  for i in range(0, min(stack_size, len(r_pp_files))):
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

def get_bg(f):
  # # Manually selected area of interest with dark background in S II image.
  (x, y, w, h) = (1165, 997, 117, 83)
  # (x, y, w, h) = (3297, 580, 30, 50)
  # Manually selected area of interest with dark background in Ha image.
  # (x, y, w, h) = (5826, 3672, 44, 26)
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

def get_noise_stats(starless_dir):
  global STRETCH
  # Get list of all "starless_*.fit" files in starless_dir
  starless_files = list(Path(starless_dir).glob('starless_*.fit'))
  # starless_files = list(Path(starless_dir).glob('stack_*.fit'))
  starless_files.sort()
  # # Manually selected area of interest with faint nebulosity in S II image.
  (x, y, w, h) = (2712, 2252, 73, 53)
  # Manually selected area of interest with faint nebulosity in Ha image.
  # (x, y, w, h) = (3729, 2964, 74, 52)
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
        bg, bgnoise = get_bg(f)
        snr = (mean - bg) / sigma
        print(f"{f.name}: Mean: {mean:5.2f}, Median: {median}, Sigma: {sigma}, Min: {min}, Max: {max}, bgnoise: {bgnoise} bg: {bg} SNR: {snr:.2f}")
      else:
        print(f"Error parsing output of stat command for {f}")
      stats.append((f, mean, median, sigma, min, max, bgnoise, bg, snr))
    except subprocess.CalledProcessError as e:
      print(f"Error running Siril: {e}")
      sys.exit(1)
  return stats

def cleanup(outdir):
  os.system(f"rm -rf {os.path.join(outdir, '.process')}")
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
    print(f"Desired stack size for SNR={desired_snr:02f}: {desired_stack_size:.0f}")


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
  plt.savefig(os.path.join(outdir, f"snr_vs_stack_{label}_{"stretch" if STRETCH else "nostretch"}.png"))
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
  parser = argparse.ArgumentParser(description='Create a csv file with astrophotography session information.')
  parser.add_argument('indir', type=str, help='Input directory containing the fits files.')
  parser.add_argument('outdir', type=str, help='Output directory.')
  parser.add_argument('-graph', action='store_true', help='Skip processing, just generate the graph.')
  parser.add_argument('-nostretch', action='store_true', help='Do not stretch the starless images.')
  args = parser.parse_args()
  if args.nostretch:
    STRETCH = False

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
    create_starless(stack_sizes, outdir)

  stats = get_noise_stats(os.path.join(outdir, '.starless'))
  label = os.path.basename(args.indir)
  plot_stats(stack_sizes, stats, outdir, label)
  # generate_gif(stack_sizes, outdir)

  cleanup(outdir)

if __name__ == "__main__":
  main()