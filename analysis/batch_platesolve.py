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

def run_star_detect_siril(this_dir, file):
    global SIRIL_PATH
    siril_commands = f"""requires 1.2.0
convert light -debayer -out=.
load light_00001
findstar
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
                                    check=True)
            # Extract the number of stars detected, and the FWHM. Sample output:
            # Found 343 Gaussian profile stars in image, channel #1 (FWHM 5.428217)
            regex = r"Found ([0-9]+) Gaussian profile stars in image, channel #[0-2] \(FWHM ([0-9]+\.[0-9]+)\)"
            # print(result.stdout)
            match = re.search(regex, result.stdout)
            if not match:
                print("No match found")
                return None, None
            num_stars, fwhm = match.groups()
            return int(num_stars), float(fwhm)
        except subprocess.CalledProcessError as e:
            return None, None

def run_plate_solve_siril(this_dir, file, wcs_coords, focal_option):
    global SIRIL_PATH
    siril_commands = f"""requires 1.2.0
convert light -debayer -out=.
load light_00001
# platesolve {wcs_coords} -platesolve -catalog=nomad {focal_option}
platesolve {wcs_coords}
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
                                    check=True)
            # print(result.stdout)
            # print(result.stderr)
            ra, dec = extract_and_convert_coordinates_siril(result.stdout)
            return ra, dec
        except subprocess.CalledProcessError as e:
            # print(f"Error running Siril: {e}")
            return None, None

def run_plate_solve_astap(this_dir, file, wcs_coords, focal_option):
    ASTAP_PATH = '/Applications/ASTAP.app/Contents/MacOS/astap'
    astap_cli_command = [ASTAP_PATH, "-f", file]
    try:
        result = subprocess.run(astap_cli_command,
                                text=True,
                                capture_output=True,
                                check=True)
        ra, dec = extract_and_convert_coordinates_astap(result.stdout)
        return ra, dec
    except subprocess.CalledProcessError as e:
        return None, None

def load_prev_files(filename):
    if not os.path.exists(filename):
        return [], []
    # The file is a CSV file with several columns, with the filename in the first column.
    filenames = []
    star_stats = []
    with open(filename, 'r') as f:
        lines = f.readlines()
        for line in lines[1:]:
            # print(line)
            parts = line.split(',')
            filenames.append(parts[0])
            star_stats.append((float(parts[1]), int(parts[4]), float(parts[5])))
    return filenames, star_stats

def plot_star_stats(data):
    # print(data)
    # Unpack the data into two separate lists
    times, num_stars, fwhm = zip(*data)

    # Create a figure and a single subplot
    fig, ax1 = plt.subplots()

    # Plot num_stars on the left y-axis
    color = 'tab:red'
    ax1.set_xlabel('Image number')
    ax1.set_ylabel('num_stars', color=color)
    # sort num_stars in ascending order
    num_stars = [x for x in sorted(num_stars)]
    ax1.plot(num_stars, color=color)
    ax1.tick_params(axis='y', labelcolor=color)

    # Create a second y-axis for FWHM
    ax2 = ax1.twinx()
    color = 'tab:blue'
    ax2.set_ylabel('FWHM', color=color)
    # sort fwhm in ascending order
    fwhm = [x for x in sorted(fwhm)]
    ax2.plot(fwhm, color=color)
    ax2.tick_params(axis='y', labelcolor=color)
    # Set Y axis labels to show 2 decimal places.
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.2f}'))

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

def process_file(current_dir, coordinates, focal_option, filename, star_stats, csv_file, lock):
    # print(f"Processing {filename}")
    t_start = time.time()
    filename_without_path = os.path.basename(filename)
    capture_time = int(get_image_capture_time(filename).timestamp())
    if coordinates is None:
        result = run_plate_solve_astap(current_dir, filename, coordinates, focal_option)
    else:
        result = run_plate_solve_siril(current_dir, filename, coordinates, focal_option)
    if result[0] is not None and result[1] is not None:
        # print(f"RA={result[0]:.12f}, DEC={result[1]:.12f}")
        pass
    else:
        print(f"{filename_without_path} [Platesolve failed]")
        return
    num_stars, fwhm = run_star_detect_siril(current_dir, filename)
    if num_stars is None:
        num_stars = 0
    if fwhm is None:
        fwhm = 0
    analysis_time = time.time() - t_start
    print(f"{filename_without_path} CaptureTime={capture_time:10d}, RA={result[0]:.12f}, DEC={result[1]:.12f}, NumStars={num_stars:5d}, FWHM={fwhm:.3f}, AnalysisTime={analysis_time:.2f}s")
    with lock:
        star_stats.append((capture_time, int(num_stars), float(fwhm)))
        if csv_file is not None:
            with open(csv_file, 'a') as f:
                f.write(f"{filename_without_path}, {capture_time:10d}, {result[0]:.12f}, {result[1]:.12f}, {num_stars}, {fwhm}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Platesolve all images in a directory')
    parser.add_argument('-d', '--directory', type=str, help='Directory containing images to platesolve')
    parser.add_argument('-o', '--object', type=str, help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
    parser.add_argument('-w', '--wcs', type=str, help='WCS coordinates')
    parser.add_argument('-f', '--focal', type=str, help='Override focal length', default='')
    parser.add_argument('-c', '--csv', type=str, help='CSV file to write results to', default='')
    args = parser.parse_args()
    coordinates = None
    if args.directory is None:
        print('ERROR: No directory specified')
        parser.print_help()
        sys.exit(1)
    # if args.object is None and args.wcs is None:
    #     print('ERROR: No object or WCS coordinates specified')
    #     parser.print_help()
    #     sys.exit(1)
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
        print('\nWARNING!\nBlind platesolving using ASTAP, no WCS coordinates specified, and no object name specified -- this will be slow! \nIf you know the approximate RA and DEC of the image, specify it with the -w option, or specify an object name with the -o option.\n')

    prev_filenames, star_stats = load_prev_files(args.csv)
    if len(prev_filenames) > 0:
        print(f"Skipping {len(prev_filenames)} files that have already been platesolved")
        if False:
            n = len(prev_filenames)
            for i in range(n):
                print(f"{prev_filenames[i]}: {star_stats[i]}")
            exit(1)

    current_dir = os.getcwd()
    csv_filename = None
    if args.csv != '':
        csv_filename = args.csv
        csv_file = open(args.csv, 'a')
        if len(prev_filenames) == 0:
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
    for filename in files:
        if filename in prev_filenames:
            print(f"{filename} [Previously solved, skipping]")
            # remove the filename from the list of files to process
        else:
            new_files.append(filename)
    # Append the directory to the filenames
    files = [args.directory + '/' + f for f in new_files]
    lock = multiprocessing.Lock()

    with multiprocessing.Pool(10) as pool:
        m = multiprocessing.Manager()
        l = m.Lock()
        pool.starmap(process_file, [(current_dir, coordinates, focal_option, f, star_stats, csv_filename, l) for f in files])

    star_stats.sort(key=lambda x: x[0])
    plot_star_stats(star_stats)
