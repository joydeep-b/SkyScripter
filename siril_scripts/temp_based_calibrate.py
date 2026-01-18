#!/usr/bin/env python3

"""
Custom Siril calibration script that:
1. Checks all CCD-TEMP values of all files in the provided input directory
2. Evaluates calibration files on a per-sub basis (matching by header values)
3. Executes single-sub calibration of all subs
"""

import argparse
import sys
import os
import subprocess
from astropy.io import fits
import glob
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import time

# Detect Siril path
if sys.platform == 'darwin':
    SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'
else:
    # On Linux, try to find siril in PATH
    siril_path = shutil.which('siril')
    if siril_path:
        SIRIL_PATH = siril_path
    else:
        print("Error: Siril not found in PATH. Please install Siril or add it to your PATH.", file=sys.stderr)
        print("You can check if Siril is installed by running: which siril", file=sys.stderr)
        sys.exit(1)


def get_fits_header_value(fits_file, keyword, default=None):
    """Extract a header value from a FITS file."""
    try:
        with fits.open(fits_file) as hdul:
            header = hdul[0].header
            if keyword in header:
                return header[keyword]
            return default
    except Exception as e:
        print(f"Warning: Could not read {keyword} from {fits_file}: {e}", file=sys.stderr)
        return default


def construct_dark_filename(dark_dir, readmode, gain, offset, exptime, temp):
    """
    Construct dark filename: master_dark_MODE{readmode}_GAIN{gain}_OFFSET{offset}_EXPTIME{exptime}_TEMP{temp}.fit
    
    Args:
        dark_dir: Directory containing dark files
        readmode: READMODE value
        gain: GAIN value
        offset: OFFSET value
        exptime: EXPTIME value
        temp: CCD-TEMP value
    
    Returns:
        Full path to dark file if it exists, None otherwise
    """
    if not dark_dir or not os.path.exists(dark_dir):
        return None
    
    # Format all values as integers (remove decimal points)
    readmode_str = f"{int(readmode)}" if readmode is not None else "5"
    gain_str = f"{int(gain)}" if gain is not None else "56"
    offset_str = f"{int(offset)}" if offset is not None else "20"
    exptime_str = f"{int(exptime)}" if exptime is not None else "480"
    temp_str = f"{int(temp)}" if temp is not None else "-10"
    
    filename = f"master_dark_MODE{readmode_str}_GAIN{gain_str}_OFFSET{offset_str}_EXPTIME{exptime_str}_TEMP{temp_str}.fit"
    dark_path = os.path.join(dark_dir, filename)
    
    if os.path.exists(dark_path):
        return dark_path
    return None


def construct_flat_filename(flat_dir, filter_name):
    """
    Construct flat filename: master_flat_{filter}.fit
    
    Args:
        flat_dir: Directory containing flat files
        filter_name: FILTER value
    
    Returns:
        Full path to flat file if it exists, None otherwise
    """
    if not flat_dir or not os.path.exists(flat_dir):
        return None
    
    if not filter_name:
        return None
    
    filename = f"master_flat_{filter_name}.fit"
    flat_path = os.path.join(flat_dir, filename)
    
    if os.path.exists(flat_path):
        return flat_path
    return None


def get_light_file_info(light_file):
    """Extract relevant information from a light file."""
    info = {
        'file': light_file,
        'temp': None,
        'filter': None,
        'gain': None,
        'offset': None,
        'exptime': None,
        'readmode': None,
    }
    
    temp = get_fits_header_value(light_file, 'CCD-TEMP')
    if temp is not None:
        info['temp'] = float(temp)
    
    info['filter'] = get_fits_header_value(light_file, 'FILTER', 'H')
    info['gain'] = get_fits_header_value(light_file, 'GAIN')
    info['offset'] = get_fits_header_value(light_file, 'OFFSET')
    info['exptime'] = get_fits_header_value(light_file, 'EXPTIME')
    info['readmode'] = get_fits_header_value(light_file, 'READMODE')
    
    return info


def build_calibrate_command(input_file, input_dir, dark_file=None, flat_file=None):
    """
    Build the calibrate_single command parts.
    
    Args:
        input_file: Name of the input file (e.g., 'light_00001.fit')
        input_dir: Directory containing input files
        dark_file: Path to dark calibration file (optional)
        flat_file: Path to flat calibration file (optional)
    
    Returns:
        List of command parts for calibrate_single
    """
    # Siril runs from output_dir, so we need to use absolute path for input file
    input_file_abs = os.path.abspath(os.path.join(input_dir, input_file))
    cmd_parts = ['calibrate_single', input_file_abs]
    
    if dark_file:
        # Convert to absolute path so Siril can find it from its working directory
        dark_file_abs = os.path.abspath(dark_file)
        cmd_parts.append(f'-dark={dark_file_abs}')
    
    if flat_file:
        # Convert to absolute path so Siril can find it from its working directory
        flat_file_abs = os.path.abspath(flat_file)
        cmd_parts.append(f'-flat={flat_file_abs}')
    
    # Use dark calibration if dark is provided
    if dark_file:
        cmd_parts.append('-cc=dark')
    
    return cmd_parts


def calibrate_single_light(input_file, input_dir, output_file, output_dir, dark_file=None, flat_file=None, debug=False):
    """
    Calibrate a single light file using Siril.
    
    Args:
        input_file: Name of the input file (e.g., 'light_00001.fit')
        input_dir: Directory containing input files
        output_file: Name of the output file (e.g., 'pp_light_00001.fit')
        output_dir: Directory for output files (also used as Siril working directory)
        dark_file: Path to dark calibration file (optional)
        flat_file: Path to flat calibration file (optional)
    
    Returns:
        (success: bool, error_message: str)
    """
    # Build calibrate_single command using shared function
    cmd_parts = build_calibrate_command(input_file, input_dir, dark_file, flat_file)
    input_file_abs = os.path.abspath(os.path.join(input_dir, input_file))
    
    siril_commands = f"""requires 1.2.0
{' '.join(cmd_parts)}
close
"""
    
    siril_cli_command = [SIRIL_PATH, "-d", output_dir, "-s", "-"]
    
    # Print debug info if requested
    if debug:
        print(f"  DEBUG: Siril command: {' '.join(siril_cli_command)}", file=sys.stderr)
        print(f"  DEBUG: Working directory: {output_dir}", file=sys.stderr)
        print(f"  DEBUG: Input file (absolute): {input_file_abs}", file=sys.stderr)
        if dark_file:
            print(f"  DEBUG: Dark file (original): {dark_file}", file=sys.stderr)
            print(f"  DEBUG: Dark file (absolute): {os.path.abspath(dark_file)}", file=sys.stderr)
        if flat_file:
            print(f"  DEBUG: Flat file (original): {flat_file}", file=sys.stderr)
            print(f"  DEBUG: Flat file (absolute): {os.path.abspath(flat_file)}", file=sys.stderr)
        print(f"  DEBUG: Siril script:\n{siril_commands}", file=sys.stderr)
    
    try:
        result = subprocess.run(
            siril_cli_command,
            input=siril_commands,
            text=True,
            capture_output=True,
            check=True
        )
        
        if debug:
            print(f"  DEBUG: Siril stdout:\n{result.stdout}", file=sys.stderr)
            if result.stderr:
                print(f"  DEBUG: Siril stderr:\n{result.stderr}", file=sys.stderr)
        
        # Siril creates pp_* files in the working directory (output_dir)
        # The output will be named pp_{basename} where basename is from the input file
        input_basename = os.path.basename(input_file)
        # Remove extension
        base_name = os.path.splitext(input_basename)[0]
        # Siril creates pp_{basename} with the same extension as input
        input_ext = os.path.splitext(input_basename)[1]
        siril_output = os.path.join(output_dir, f"pp_{base_name}{input_ext}")
        
        if debug:
            print(f"  DEBUG: Looking for Siril output: {siril_output}", file=sys.stderr)
        
        # Check for both .fit and .fits extensions in case Siril changes it
        if not os.path.exists(siril_output):
            # Try alternate extension
            alt_ext = '.fits' if input_ext == '.fit' else '.fit'
            siril_output = os.path.join(output_dir, f"pp_{base_name}{alt_ext}")
            if debug:
                print(f"  DEBUG: Trying alternate extension: {siril_output}", file=sys.stderr)
        
        if os.path.exists(siril_output):
            # Rename to the desired output filename
            final_output = os.path.join(output_dir, output_file)
            if debug:
                print(f"  DEBUG: Renaming {siril_output} to {final_output}", file=sys.stderr)
            os.makedirs(output_dir, exist_ok=True)
            # Only rename if the names are different
            if siril_output != final_output:
                os.rename(siril_output, final_output)
            return True, result.stdout
        else:
            # List what files actually exist in the output directory
            existing_files = [f for f in os.listdir(output_dir) if f.startswith('pp_')]
            error_msg = f"Siril output file not found: {siril_output}\n"
            error_msg += f"  Expected output: {siril_output}\n"
            error_msg += f"  Input file: {input_file_abs}\n"
            error_msg += f"  Base name: {base_name}\n"
            error_msg += f"  Extension: {input_ext}\n"
            error_msg += f"  Files starting with 'pp_' in {output_dir}: {existing_files[:10] if existing_files else 'none'}"
            return False, error_msg
            
    except subprocess.CalledProcessError as e:
        error_msg = f"Siril subprocess failed with return code {e.returncode}\n"
        error_msg += f"  Command: {' '.join(siril_cli_command)}\n"
        error_msg += f"  Working directory: {output_dir}\n"
        error_msg += f"  Input file: {input_file_abs}\n"
        error_msg += f"  Dark file: {dark_file}\n"
        error_msg += f"  Flat file: {flat_file}\n"
        error_msg += f"  Siril stdout:\n{e.stdout}\n"
        error_msg += f"  Siril stderr:\n{e.stderr}\n"
        return False, error_msg
    except Exception as e:
        import traceback
        error_msg = f"Unexpected error during calibration:\n"
        error_msg += f"  Exception type: {type(e).__name__}\n"
        error_msg += f"  Exception message: {str(e)}\n"
        error_msg += f"  Traceback:\n{traceback.format_exc()}\n"
        error_msg += f"  Input file: {input_file_abs}\n"
        error_msg += f"  Input directory: {input_dir}\n"
        error_msg += f"  Output file: {output_file}\n"
        error_msg += f"  Output directory: {output_dir}"
        return False, error_msg


def main():
    parser = argparse.ArgumentParser(
        description='Custom Siril calibration script with temperature-based calibration file matching',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s /path/to/input --output-dir /path/to/output --dark-dir /path/to/darks --flat-dir /path/to/flats
  %(prog)s /path/to/input --output-dir /path/to/output --dark-dir /path/to/darks --flat-dir /path/to/flats --output-prefix "cal_"
        """
    )
    
    parser.add_argument(
        'input_dir',
        type=str,
        help='Directory containing input FITS files to calibrate'
    )
    
    parser.add_argument(
        '--output-dir',
        type=str,
        required=True,
        help='Directory for output calibrated files'
    )
    
    parser.add_argument(
        '--output-prefix',
        type=str,
        default='pp_light_',
        help='Prefix for output files (default: pp_light_)'
    )
    
    parser.add_argument(
        '--dark-dir',
        type=str,
        required=True,
        help='Directory containing dark calibration files'
    )
    
    parser.add_argument(
        '--flat-dir',
        type=str,
        required=True,
        help='Directory containing flat calibration files'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print calibration commands without executing them'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Print verbose output'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Print detailed debugging information'
    )
    
    parser.add_argument(
        '--threads',
        type=int,
        default=0,
        help='Number of parallel threads to use (0 = auto-detect CPU count, default: 0)'
    )
    
    args = parser.parse_args()
    
    # Print debug info about Siril
    if args.debug:
        print(f"DEBUG: Siril path: {SIRIL_PATH}", file=sys.stderr)
        print(f"DEBUG: Siril exists: {os.path.exists(SIRIL_PATH)}", file=sys.stderr)
        if os.path.exists(SIRIL_PATH):
            print(f"DEBUG: Siril is executable: {os.access(SIRIL_PATH, os.X_OK)}", file=sys.stderr)
    
    input_dir = os.path.abspath(args.input_dir)
    output_dir = os.path.abspath(args.output_dir)
    
    # Convert calibration directories to absolute paths
    dark_dir = os.path.abspath(args.dark_dir) if args.dark_dir else None
    flat_dir = os.path.abspath(args.flat_dir) if args.flat_dir else None
    
    if args.debug:
        print(f"DEBUG: Input directory (abs): {input_dir}", file=sys.stderr)
        print(f"DEBUG: Output directory (abs): {output_dir}", file=sys.stderr)
        print(f"DEBUG: Dark directory (abs): {dark_dir}", file=sys.stderr)
        print(f"DEBUG: Flat directory (abs): {flat_dir}", file=sys.stderr)
    
    if not os.path.exists(input_dir):
        print(f"Error: Input directory '{input_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)
    
    if dark_dir and not os.path.exists(dark_dir):
        print(f"Error: Dark directory '{dark_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)
    
    if flat_dir and not os.path.exists(flat_dir):
        print(f"Error: Flat directory '{flat_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)
    
    # Find all FITS files in the input directory
    input_files = []
    for pattern in ['*.fit', '*.fits']:
        input_files.extend(glob.glob(os.path.join(input_dir, pattern)))
    
    if not input_files:
        print(f"Error: No FITS files found in '{input_dir}'", file=sys.stderr)
        sys.exit(1)
    
    # Sort alphabetically
    input_files.sort()
    print(f"Found {len(input_files)} input files")
    
    # Extract CCD-TEMP values and file info
    print("\nChecking CCD-TEMP values...")
    file_info = []
    temp_values = []
    
    for input_file in input_files:
        info = get_light_file_info(input_file)
        file_info.append(info)
        if info['temp'] is not None:
            temp_values.append(info['temp'])
            if args.verbose:
                print(f"  {os.path.basename(input_file)}: CCD-TEMP = {info['temp']:.2f}°C")
    
    if not temp_values:
        print("Warning: No CCD-TEMP values found in input files!", file=sys.stderr)
    else:
        print(f"\nTemperature range: {min(temp_values):.2f}°C to {max(temp_values):.2f}°C")
        print(f"Temperature values: {sorted(set([round(t, 1) for t in temp_values]))}")
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Determine number of threads
    if args.threads == 0:
        num_threads = multiprocessing.cpu_count()
    else:
        num_threads = args.threads
    
    print(f"\nUsing {num_threads} parallel thread(s)")
    print(f"Processing {len(file_info)} input files...")
    
    # Record start time
    start_time = time.time()
    
    # Prepare all calibration tasks
    tasks = []
    for i, info in enumerate(file_info, 1):
        input_file = os.path.basename(info['file'])
        input_basename = os.path.basename(input_file)
        input_name, input_ext = os.path.splitext(input_basename)
        # Always use .fit extension for output files
        output_filename = f"{args.output_prefix}{i:05d}.fit"
        output_path = os.path.join(output_dir, output_filename)
        
        # Check if output file already exists
        if os.path.exists(output_path):
            if args.verbose:
                print(f"[{i}/{len(file_info)}] Skipping {input_file}: {output_filename} already exists")
            continue
        
        # Construct calibration file paths based on header values
        dark_file = None
        flat_file = None
        
        # Construct dark filename
        if dark_dir:
            dark_file = construct_dark_filename(
                dark_dir,
                info['readmode'] if info['readmode'] is not None else 5,
                info['gain'] if info['gain'] is not None else 56,
                info['offset'] if info['offset'] is not None else 20,
                info['exptime'] if info['exptime'] is not None else 480,
                info['temp'] if info['temp'] is not None else -10
            )
        
        # Construct flat filename
        if flat_dir:
            flat_file = construct_flat_filename(flat_dir, info['filter'])
        
        tasks.append({
            'index': i,
            'total': len(file_info),
            'input_file': input_file,
            'output_filename': output_filename,
            'dark_file': dark_file,
            'flat_file': flat_file,
            'info': info
        })
    
    if not tasks:
        print("All files already processed. Nothing to do.")
        return
    
    # Process files in parallel
    success_count = 0
    
    if args.dry_run:
        # Dry run: just print what would be executed
        for task in tasks:
            print(f"\n[{task['index']}/{task['total']}] Would process {task['input_file']}")
            if task['info']['temp'] is not None:
                print(f"  CCD-TEMP: {task['info']['temp']:.2f}°C")
            if task['info']['filter']:
                print(f"  Filter: {task['info']['filter']}")
            if task['dark_file']:
                print(f"  Matched dark: {os.path.basename(task['dark_file'])}")
            if task['flat_file']:
                print(f"  Matched flat: {os.path.basename(task['flat_file'])}")
            
            # Use the same function to build the command
            cmd_parts = build_calibrate_command(
                task['input_file'],
                input_dir,
                task['dark_file'],
                task['flat_file']
            )
            cmd = ' '.join(cmd_parts)
            print(f"  Would execute: {cmd}")
            print(f"  Would output to: {os.path.join(output_dir, task['output_filename'])}")
        success_count = len(tasks)
    else:
        # Actual processing with parallel execution
        def process_task(task):
            """Process a single calibration task."""
            try:
                if args.verbose:
                    print(f"[{task['index']}/{task['total']}] Processing {task['input_file']}", flush=True)
                
                success, output = calibrate_single_light(
                    task['input_file'],
                    input_dir,
                    task['output_filename'],
                    output_dir,
                    task['dark_file'],
                    task['flat_file'],
                    args.debug
                )
                
                if success:
                    if args.verbose:
                        print(f"[{task['index']}/{task['total']}] ✓ {task['input_file']} -> {task['output_filename']}", flush=True)
                    return True, task['input_file'], task['output_filename'], None
                else:
                    return False, task['input_file'], task['output_filename'], output
            except Exception as e:
                import traceback
                error_msg = f"Unexpected error processing {task['input_file']}:\n{traceback.format_exc()}"
                return False, task['input_file'], task['output_filename'], error_msg
        
        # Execute tasks in parallel
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            future_to_task = {executor.submit(process_task, task): task for task in tasks}
            
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    success, input_file, output_filename, error_msg = future.result()
                    if success:
                        success_count += 1
                    else:
                        # Error occurred - print details and exit
                        print(f"\n✗ Calibration failed for {input_file}:", file=sys.stderr)
                        if error_msg:
                            print(f"{error_msg}", file=sys.stderr)
                        print(f"\nERROR: Calibration failed. Exiting.", file=sys.stderr)
                        # Cancel remaining tasks
                        for f in future_to_task:
                            f.cancel()
                        sys.exit(1)
                except Exception as e:
                    print(f"\n✗ Unexpected error processing {task['input_file']}: {e}", file=sys.stderr)
                    import traceback
                    print(traceback.format_exc(), file=sys.stderr)
                    # Cancel remaining tasks
                    for f in future_to_task:
                        f.cancel()
                    sys.exit(1)
    
    # Calculate elapsed time
    end_time = time.time()
    elapsed_seconds = int(end_time - start_time)
    hours = elapsed_seconds // 3600
    minutes = (elapsed_seconds % 3600) // 60
    seconds = elapsed_seconds % 60
    
    print(f"\n{'='*60}")
    print(f"Summary: {success_count} files processed successfully")
    print(f"Total time: {hours:02d}:{minutes:02d}:{seconds:02d}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()

