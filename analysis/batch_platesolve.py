from astroquery.simbad import Simbad
from astropy.coordinates import SkyCoord
import astropy.units as units
import argparse
import os
import sys
import subprocess
import re

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
    print(f"RA: {alpha_h}h{alpha_m}m{alpha_s}s, DEC: {delta_sign}{delta_d}°{delta_m}'{delta_s}")

    # Convert alpha (RA) to decimal degrees
    alpha = 180/12 * (float(alpha_h) + float(alpha_m)/60 + float(alpha_s)/3600)

    # Convert delta (DEC) to decimal degrees
    delta_multiplier = 1 if delta_sign == '+' else -1
    delta = delta_multiplier * (float(delta_d) + float(delta_m)/60 + float(delta_s)/3600)

    return alpha, delta

def extract_and_convert_coordinates_siril(output):
    # Define the regex pattern
    regex = r"Image center: alpha: ([0-9]+)h([0-9]+)m([0-9]+)s, delta: ([+-])([0-9]+)°([0-9]+)'([0-9]+)"

    # Search for the pattern in the output
    match = re.search(regex, output)
    if not match:
        print("No match found")
        return None, None

    # Extract matched groups
    alpha_h, alpha_m, alpha_s, delta_sign, delta_d, delta_m, delta_s = match.groups()
    # print(f"RA: {alpha_h}h{alpha_m}m{alpha_s}s, DEC: {delta_sign}{delta_d}°{delta_m}'{delta_s}")

    # Convert alpha (RA) to decimal degrees
    alpha = 180/12 * (int(alpha_h) + int(alpha_m)/60 + int(alpha_s)/3600)

    # Convert delta (DEC) to decimal degrees
    delta_multiplier = 1 if delta_sign == '+' else -1
    delta = delta_multiplier * (int(delta_d) + int(delta_m)/60 + int(delta_s)/3600)

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
    # If MacOS, use the Siril.app version
    if sys.platform == 'darwin':
      SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/siril-cli'
    else:
      SIRIL_PATH = 'siril-cli'
      
    siril_commands = f"""requires 1.2.0
load {file}
findstar
close
"""
    # Define the command to run
    siril_cli_command = [SIRIL_PATH, "-d", this_dir, "-s", "-"]

    # Run the command and capture output
    try:
        result = subprocess.run(siril_cli_command, 
                                input=siril_commands,
                                text=True, 
                                capture_output=True,
                                check=True)
        # Extract the number of stars detected, and the FWHM. Sample output:
        # Found 343 Gaussian profile stars in image, channel #0 (FWHM 5.428217)
        regex = r"Found ([0-9]+) Gaussian profile stars in image, channel #0 \(FWHM ([0-9]+\.[0-9]+)\)"
        match = re.search(regex, result.stdout)
        if not match:
            print("No match found")
            return None, None
        num_stars, fwhm = match.groups()
        return num_stars, fwhm
    except subprocess.CalledProcessError as e:
        return None, None
    
def run_plate_solve_siril(this_dir, file, wcs_coords, focal_option):
    # If MacOS, use the Siril.app version
    if sys.platform == 'darwin':
      SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/siril-cli'
    else:
      SIRIL_PATH = 'siril-cli'

    siril_commands = f"""requires 1.2.0
load {file}
platesolve {wcs_coords} -platesolve -catalog=nomad -limitmag=10 {focal_option}
close
"""
    # Define the command to run
    siril_cli_command = [SIRIL_PATH, "-d", this_dir, "-s", "-"]

    # Run the command and capture output
    try:
        result = subprocess.run(siril_cli_command, 
                                input=siril_commands,
                                text=True, 
                                capture_output=True,
                                check=True)
        ra, dec = extract_and_convert_coordinates_siril(result.stdout)
        return ra, dec
    except subprocess.CalledProcessError as e:
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

    current_dir = os.getcwd()
    csv_file = None
    if args.csv != '':
        csv_file = open(args.csv, 'w')
        csv_file.write('Filename,RA,DEC,NumStars,FWHM\n')
        print(f"Writing results to {args.csv}")
    # Run platesolve on all images in the directory
    for filename in sorted(os.listdir(args.directory)):
        # Exclude system files.
        if filename.startswith('.'):
            continue
        # Exclude non-image files.
        allowed_extensions = ['.fit', '.fits', '.cr2', '.cr3', '.jpg', '.png', '.tif', '.tiff']
        if not any(filename.lower().endswith(ext) for ext in allowed_extensions):
            continue
        file = args.directory + '/' + filename
        if coordinates is None:
          result = run_plate_solve_astap(current_dir, file, coordinates, focal_option)
        else:
          result = run_plate_solve_siril(current_dir, file, coordinates, focal_option)
        if result[0] is not None and result[1] is not None:
            # print(f"RA={result[0]:.12f}, DEC={result[1]:.12f}")
            pass
        else:
            print(f"Platesolve failed")
        num_stars, fwhm = run_star_detect_siril(current_dir, file)
        print(f"File: {filename}, RA={result[0]:.12f}, DEC={result[1]:.12f}, NumStars={num_stars}, FWHM={fwhm}")
        if csv_file is not None:
            # Write the numbers in 6 decimal places
            csv_file.write(f"{filename}, {result[0]:.12f}, {result[1]:.12f}, {num_stars}, {fwhm}\n")
        # sys.exit(1)