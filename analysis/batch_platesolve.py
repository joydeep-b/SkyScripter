#!/usr/bin/env python

from astroquery.simbad import Simbad
from astropy.coordinates import SkyCoord
import astropy.units as units
import argparse
import os
import sys
import subprocess
import re
import tempfile
import datetime
import matplotlib.pyplot as plt
import time
import multiprocessing
from functools import partial

if sys.platform == 'darwin':
  SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'
else:
  SIRIL_PATH = 'siril-cli'

def extract_and_convert_coordinates_astap(output):
    # Define the regex pattern, to match output like this:
    # Solution found: 05: 36 03.8	-05° 27 14
    regex = r"Solution found: ([0-9]+): ([0-9]+) ([0-9]+\.[0-9]+)\t([+-])([0-9]+)° ([0-9]+) ([0-9]+)"

    # Search for the pattern in the output
    match = re.search(regex, output)
    if not match:
        print("No match found")
        return None, None

    # Extract matched groups
    alpha_h, alpha_m, alpha_s, delta_sign, delta_d, delta_m, delta_s = match.groups()
    # print(f"RA: {alpha_h}h{alpha_m}m{alpha_s}s, DEC: {delta_sign}{delta_d}°{delta_m}'{delta_s}")

    # Convert alpha (RA) to hour angle
    alpha = float(alpha_h) + float(alpha_m)/60 + float(alpha_s)/3600

    # Convert delta (DEC) to decimal degrees
    delta_multiplier = 1 if delta_sign == '+' else -1
    delta = delta_multiplier * (float(delta_d) + float(delta_m)/60 + float(delta_s)/3600)

    return alpha, delta

def extract_and_convert_coordinates_siril(output):
    # Define the regex pattern
    regex = r"Image center: alpha: ([0-9]+) ([0-9]+) ([0-9\.]+), delta: ([+-])([0-9]+) ([0-9]+) ([0-9\.]+)"

    # Search for the pattern in the output
    match = re.search(regex, output)
    if not match:
        print("No match found")
        return None, None

    # Extract matched groups
    alpha_h, alpha_m, alpha_s, delta_sign, delta_d, delta_m, delta_s = match.groups()
    # print(f"RA: {alpha_h}h{alpha_m}m{alpha_s}s, DEC: {delta_sign}{delta_d}°{delta_m}'{delta_s}")

    # Convert alpha (RA) to decimal degrees
    alpha = 180/12 * (int(alpha_h) + int(alpha_m)/60 + float(alpha_s)/3600)

    # Convert delta (DEC) to decimal degrees
    delta_multiplier = 1 if delta_sign == '+' else -1
    delta = delta_multiplier * (int(delta_d) + int(delta_m)/60 + float(delta_s)/3600)

    return alpha, delta

def get_wcs_coordinates(object_name):
    # Query the object
    result_table = Simbad.query_object(object_name)

    if result_table is None:
        print(f"ERROR: Unable to find object '{object_name}'")
        sys.exit(1)

    # Extract RA and DEC
    ra = result_table['RA'][0]
    dec = result_table['DEC'][0]

    # Create a SkyCoord object
    coord = SkyCoord(ra, dec, unit=(units.hourangle, units.deg))

    return coord.ra.to_string(unit=units.hour, sep=':') + ' ' + \
           coord.dec.to_string(unit=units.degree, sep=':')
    # Return RA, DEC in degrees
    return coord.ra.deg, coord.dec.deg

def findstar_and_platesolve_siril(wcs_coords: str, file: str)  -> (tuple[int, float, float | None, float | None] | tuple[None, None, None, None]):
    global SIRIL_PATH
    if wcs_coords is None:
        platesolve_command = 'platesolve'
    else:
        platesolve_command = f'platesolve {wcs_coords}'
    siril_commands = f"""requires 1.2.0
convert light -out=.
calibrate_single light_00001 -dark=/Users/joydeepbiswas/Astrophotography/masters/dark/master_dark_MODE$READMODE:%1d$_GAIN$GAIN:%2d$_OFFSET$OFFSET:%2d$_EXPTIME$EXPTIME:%3d$_TEMP$CCD-TEMP:%d$ -flat=/Users/joydeepbiswas/Astrophotography/masters/flat/master_flat_$FILTER:%s$ -cc=dark
load pp_light_00001
findstar
{platesolve_command}
close
"""
    # Create a temp directory for Siril to use.
    with tempfile.TemporaryDirectory() as temp_dir:
        # Copy the file to the temp directory.
        temp_file = temp_dir + '/' + os.path.basename(file)
        os.system(f"cp {file} {temp_file}")
        # Define the command to run
        siril_cli_command = [SIRIL_PATH, "-d", temp_dir, "-s", "-"]
        # Run the command and capture output
        try:
            result = subprocess.run(siril_cli_command,
                                    input=siril_commands,
                                    text=True,
                                    capture_output=True,
                                    check=False)
            # Extract the number of stars detected, and the FWHM. Sample output:
            # Found 343 Gaussian profile stars in image, channel #1 (FWHM 5.428217)
            regex = r"Found ([0-9]+) Gaussian profile stars in image, channel #[0-2] \(FWHM ([0-9]+\.[0-9]+)\)"
            # print(result.stdout)
            match = re.search(regex, result.stdout)
            if not match:
                print("No stars found")
                num_stars, fwhm = None, None
            else:
                num_stars, fwhm = match.groups()
            ra, dec = extract_and_convert_coordinates_siril(result.stdout)
            return int(num_stars), float(fwhm), ra, dec
        except subprocess.CalledProcessError as e:
            print(f"Error running Siril: {e}")
            return None, None, None, None

def load_prev_files(filename):
    if not os.path.exists(filename):
        return []
    prev_results = []
    with open(filename, 'r') as f:
        lines = f.readlines()
        for line in lines[1:]:
            # print(line)
            parts = line.split(',')
            # Note: Row format is: Filename,CaptureTime,RA,DEC,NumStars,FWHM
            prev_results.append((parts[0], int(parts[1]), float(parts[2]), float(parts[3]), int(parts[4]), float(parts[5])))
    return prev_results

def plot_star_stats(num_stars, fwhm, min_num_stars, max_fwhm):
    # Create a figure and a single subplot
    fig, ax1 = plt.subplots()

    # Plot num_stars on the left y-axis
    color = 'tab:red'
    ax1.set_xlabel('Image number')
    ax1.set_ylabel('num_stars', color=color)
    # sort num_stars in descending order
    # num_stars = [x for x in sorted(num_stars, reverse=True)]
    ax1.plot(num_stars, color=color)
    ax1.tick_params(axis='y', labelcolor=color)
    # Add a horizontal line for the minimum number of stars
    if min_num_stars > 0:
        ax1.axhline(y=min_num_stars, color='r', linestyle='--')

    # Create a second y-axis for FWHM
    ax2 = ax1.twinx()
    color = 'tab:blue'
    ax2.set_ylabel('FWHM', color=color)
    # sort fwhm in ascending order
    # fwhm = [x for x in sorted(fwhm)]
    ax2.plot(fwhm, color=color)
    ax2.tick_params(axis='y', labelcolor=color)
    # Set Y axis labels to show 2 decimal places.
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.2f}'))
    if max_fwhm > 0:
        ax2.axhline(y=max_fwhm, color='b', linestyle='--')

    # Set the title.
    plt.title('Number of stars detected and FWHM')
    # Show the plot
    plt.show()

def get_fits_image_capture_time(image_file):
    command = f'fitsheader {image_file} | grep DATE-OBS'
    try:
        output = subprocess.check_output(command, shell=True)
    except subprocess.CalledProcessError as e:
        print(f"Error calling fitsheader: {e}")
        os.exit(1)
    try:
        output = output.decode('utf-8')
        date_part = output.split('=')[1].strip()
        # Extract only the date and time part, and convert to a datetime object.
        re2 = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+)')
        date_part = re2.search(date_part).group(1)
        result = datetime.datetime.strptime(date_part, '%Y-%m-%dT%H:%M:%S.%f')
    except Exception as e:
        print(f"Error parsing date: {e}\n Output: {output}\n Command: {command}")
        return None
    return result

def get_raw_image_capture_time(image_file):
  command = f'exiftool -DateTimeOriginal {image_file}'
  try:
    output = subprocess.check_output(command, shell=True)
  except subprocess.CalledProcessError as e:
    print(f"Error calling exiftool: {e}")
    os.exit(1)
  try:
    output = output.decode('utf-8')
    date_part = output.split(': ')[1]
    result = datetime.datetime.strptime(date_part, '%Y:%m:%d %H:%M:%S\n')
  except Exception as e:
    print(f"Error parsing date: {e}\n Output: {output}\n Command: {command}")
    return None
  return result

def get_image_capture_time(image_file):
    # If the image is a FITS file, use fitsheader to get the capture time.
    if image_file.lower().endswith('.fit') or image_file.lower().endswith('.fits'):
        return get_fits_image_capture_time(image_file)
    else:
        return get_raw_image_capture_time(image_file)

def process_file(current_dir, coordinates, focal_option, filename, results, bad_files, csv_file, lock):
    # print(f"Processing {filename}")
    t_start = time.time()
    filename_without_path = os.path.basename(filename)
    capture_time = int(get_image_capture_time(filename).timestamp())
    num_stars, fwhm, ra, dec = findstar_and_platesolve_siril(coordinates, filename)
    if num_stars is None or fwhm is None or ra is None or dec is None:
        print(f"{filename_without_path} [Image analysis failed]")
        bad_files.append(filename_without_path)
        return
    analysis_time = time.time() - t_start
    print(f"{filename_without_path} CaptureTime={capture_time:10d}, RA={ra:.12f}, DEC={dec:.12f}, NumStars={num_stars:5d}, FWHM={fwhm:.3f}, AnalysisTime={analysis_time:.2f}s")
    lock.acquire()
    results.append((filename_without_path, capture_time, ra, dec, num_stars, fwhm))
    if csv_file is not None:
        with open(csv_file, 'a') as f:
            f.write(f"{filename_without_path}, {capture_time:10d}, {ra:.12f}, {dec:.12f}, {num_stars}, {fwhm}\n")
    lock.release()

def filter_subs(results, min_num_stars, max_fwhm):
    culled_files = [x[0] for x in results if x[4] < min_num_stars or x[5] > max_fwhm]
    if len(culled_files) == 0:
        print("No files to cull")
        return
    print(f"Recommend culling {len(culled_files)} files out of {len(results)} ({len(culled_files)/len(results)*100:.2f}%)")
    for f in culled_files:
        print(f, end=' ')
    print()

def sort_and_save_csv(filename):
    lines = load_prev_files(filename)
    lines.sort(key=lambda x: x[1])
    with open(filename, 'w') as f:
        f.write('Filename,CaptureTime,RA,DEC,NumStars,FWHM\n')
        for line in lines:
            f.write(f"{line[0]},{line[1]},{line[2]},{line[3]},{line[4]},{line[5]}\n")
    return lines


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Platesolve all images in a directory')
    parser.add_argument('-d', '--directory', type=str, help='Directory containing images to platesolve')
    parser.add_argument('-o', '--object', type=str, help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
    parser.add_argument('-w', '--wcs', type=str, help='WCS coordinates')
    parser.add_argument('-f', '--focal', type=str, help='Override focal length', default='')
    parser.add_argument('-c', '--csv', type=str, help='CSV file to write results to', default='')
    parser.add_argument('-s', '--min-num-stars', type=int, help='Minimum number of stars for filtering', default=0)
    parser.add_argument('-m', '--max-fwhm', type=float, help='Maximum FWHM for filtering', default=3)
    args = parser.parse_args()
    coordinates = None
    if args.directory is None:
        print('ERROR: No directory specified')
        parser.print_help()
        sys.exit(1)
    if args.object is not None and args.wcs is not None:
        print('ERROR: Both object and WCS coordinates specified')
        parser.print_help()
        sys.exit(1)
    if args.object is not None:
        coordinates = get_wcs_coordinates(args.object)
        # Print WCS coordinates in 6 decimal places
        print(f"Using WCS coordinates of '{args.object}': {coordinates}")
    else:
        coordinates = args.wcs
        print(f"Using WCS coordinates: {coordinates}")
    if args.focal != '':
        focal_option = f'-focal={args.focal}'
    else:
        focal_option = ''

    if coordinates is None:
        print('\nWARNING!\nBlind platesolving, no WCS coordinates specified, and no object name specified -- this may be inaccurate! \nIf you know the approximate RA and DEC of the image, specify it with the -w option, or specify an object name with the -o option.\n')

    prev_results = load_prev_files(args.csv)

    current_dir = os.getcwd()
    csv_filename = None
    if args.csv != '':
        csv_filename = args.csv
        csv_file = open(args.csv, 'a')
        if len(prev_results) == 0:
            csv_file.write('Filename,CaptureTime,RA,DEC,NumStars,FWHM\n')
        print(f"Writing results to {args.csv}")
        csv_file.close()

    # Run platesolve on all images in the directory
    files = sorted(os.listdir(args.directory))
    allowed_extensions = ['.fit', '.fits', '.cr2', '.cr3', '.jpg', '.png', '.tif', '.tiff']
    files = [f for f in files if any(f.lower().endswith(ext) for ext in allowed_extensions)]
    files = [f for f in files if not f.startswith('.')]
    print(f"Processing {len(files)} files in directory {args.directory}")

    new_files = []
    if len(prev_results) > 0:
        prev_filenames = [x[0] for x in prev_results]
    else:
        prev_filenames = []
    for filename in files:
        if filename in prev_filenames:
            print(f"{filename} [Previously solved, skipping]")
        else:
            new_files.append(filename)
    # Append the directory to the filenames
    files = [args.directory + '/' + f for f in new_files]

    results = prev_results
    lock = multiprocessing.Lock()
    with multiprocessing.Pool(10) as pool:
        m = multiprocessing.Manager()
        bad_files = m.list()
        l = m.Lock()
        pool.starmap(process_file, [(current_dir, coordinates, focal_option, f, results, bad_files, csv_filename, l) for f in files])

    if len(bad_files) > 0:
        print(f"{len(bad_files)} files failed to platesolve:")
        for f in bad_files:
            print(f, end=' ')
        print()

    results = sort_and_save_csv(args.csv)

    filter_subs(results, args.min_num_stars, args.max_fwhm)

    num_stars = [x[4] for x in results[1:]]
    fwhm = [x[5] for x in results[1:]]
    # print(num_stars)
    # print(fwhm)
    plot_star_stats(num_stars, fwhm, args.min_num_stars, args.max_fwhm)

