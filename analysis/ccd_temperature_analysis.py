#!/usr/bin/env python3

"""
Analyze CCD temperature and observation dates from FITS files.

This script reads all FITS files in a directory (recursively) and extracts
CCD temperature and observation dates. It then groups the data by date
and temperature (with configurable granularity) and optionally displays
visualizations.
"""

import argparse
from pathlib import Path
from astropy.io import fits
from datetime import datetime
from collections import defaultdict
import sys

try:
    import matplotlib.pyplot as plt
    import numpy as np
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


def parse_date_obs(date_obs_str):
    """Parse DATE-OBS header value into datetime object."""
    # Try different date formats that might be in FITS headers
    formats = [
        '%Y-%m-%dT%H:%M:%S.%f',  # ISO format with microseconds
        '%Y-%m-%dT%H:%M:%S',     # ISO format without microseconds
        '%Y-%m-%d %H:%M:%S.%f',  # Space-separated with microseconds
        '%Y-%m-%d %H:%M:%S',     # Space-separated without microseconds
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_obs_str, fmt)
        except ValueError:
            continue
    
    # If all formats fail, try to parse just the date part
    try:
        return datetime.strptime(date_obs_str.split('T')[0], '%Y-%m-%d')
    except (ValueError, IndexError):
        raise ValueError(f"Unable to parse date: {date_obs_str}")


def round_temperature(temp, granularity):
    """Round temperature to the nearest granularity value."""
    return round(temp / granularity) * granularity


def extract_fits_data(directory, temp_granularity=5.0):
    """
    Extract CCD temperature and observation dates from all FITS files.
    
    Args:
        directory: Path to directory containing FITS files
        temp_granularity: Temperature grouping granularity in degrees C
    
    Returns:
        tuple: (data_list, date_temp_map)
            - data_list: List of (datetime, temperature, filename) tuples
            - date_temp_map: Dict mapping (date, rounded_temp) to count
    """
    directory = Path(directory)
    if not directory.exists():
        print(f"Error: Directory '{directory}' does not exist.", file=sys.stderr)
        sys.exit(1)
    
    # Find all FITS files recursively
    print(f"Searching for FITS files in {directory}...")
    fits_files = list(directory.rglob('*.fits')) + list(directory.rglob('*.fit'))
    print(f"Found {len(fits_files)} FITS files")
    
    data_list = []
    date_temp_map = defaultdict(int)
    errors = []
    
    for fits_file in fits_files:
        file_path = str(fits_file)
        # Skip hidden files
        if "/." in file_path:
            continue
        
        try:
            with fits.open(fits_file) as hdul:
                header = hdul[0].header
                
                # Extract DATE-OBS
                if 'DATE-OBS' not in header:
                    errors.append(f"{fits_file}: Missing DATE-OBS header")
                    continue
                
                date_obs = parse_date_obs(header['DATE-OBS'])
                
                # Extract CCD-TEMP
                if 'CCD-TEMP' not in header:
                    errors.append(f"{fits_file}: Missing CCD-TEMP header")
                    continue
                
                ccd_temp = float(header['CCD-TEMP'])
                rounded_temp = round_temperature(ccd_temp, temp_granularity)
                
                # Store data
                data_list.append((date_obs, ccd_temp, fits_file))
                date_temp_map[(date_obs.date(), rounded_temp)] += 1
                
        except Exception as e:
            errors.append(f"{fits_file}: {e}")
            continue
    
    if errors:
        print(f"\nWarnings ({len(errors)} files with errors):", file=sys.stderr)
        for error in errors[:10]:  # Show first 10 errors
            print(f"  {error}", file=sys.stderr)
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more errors", file=sys.stderr)
    
    return data_list, date_temp_map


def print_date_temperature_summary(date_temp_map, temp_granularity):
    """Print summary of which dates had which CCD temperatures."""
    print(f"\n{'='*60}")
    print(f"CCD Temperature Summary (granularity: {temp_granularity}°C)")
    print(f"{'='*60}\n")
    
    # Group by temperature first, then by date
    by_temp = defaultdict(dict)
    for (date, temp), count in sorted(date_temp_map.items()):
        by_temp[temp][date] = count
    
    # Print summary
    for temp in sorted(by_temp.keys()):
        print(f"Temperature: {temp:6.1f}°C")
        dates = sorted(by_temp[temp].items())
        for date, count in dates:
            print(f"  Date: {date}  ({count:4d} subs)")
        print()


def plot_temperature_vs_time(data_list):
    """Plot continuous temperature vs time."""
    if not MATPLOTLIB_AVAILABLE:
        print("Error: matplotlib is not available. Cannot generate plot.", file=sys.stderr)
        return
    
    if not data_list:
        print("No data to plot.", file=sys.stderr)
        return
    
    # Sort by datetime
    data_list.sort(key=lambda x: x[0])
    
    times = [d[0] for d in data_list]
    temps = [d[1] for d in data_list]
    
    plt.figure(figsize=(12, 6))
    plt.plot(times, temps, 'b-', alpha=0.6, linewidth=0.5)
    plt.scatter(times, temps, s=10, alpha=0.4, c='blue')
    plt.xlabel('Observation Time')
    plt.ylabel('CCD Temperature (°C)')
    plt.title('CCD Temperature vs. Time')
    plt.grid(True, alpha=0.3)
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.show()


def plot_temperature_histogram(data_list):
    """Plot histogram of CCD temperatures."""
    if not MATPLOTLIB_AVAILABLE:
        print("Error: matplotlib is not available. Cannot generate plot.", file=sys.stderr)
        return
    
    if not data_list:
        print("No data to plot.", file=sys.stderr)
        return
    
    temps = [d[1] for d in data_list]
    
    plt.figure(figsize=(10, 6))
    plt.hist(temps, bins=30, edgecolor='black', alpha=0.7)
    plt.xlabel('CCD Temperature (°C)')
    plt.ylabel('Number of Subs')
    plt.title('CCD Temperature Distribution')
    plt.grid(True, alpha=0.3, axis='y')
    plt.tight_layout()
    plt.show()


def main():
    parser = argparse.ArgumentParser(
        description='Analyze CCD temperature and observation dates from FITS files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s /path/to/fits/directory
  %(prog)s /path/to/fits/directory --granularity 2.5
  %(prog)s /path/to/fits/directory --graph
  %(prog)s /path/to/fits/directory --histogram
        """
    )
    
    parser.add_argument(
        'directory',
        type=str,
        help='Directory containing FITS files to analyze (searched recursively)'
    )
    
    parser.add_argument(
        '--granularity',
        type=float,
        default=5.0,
        help='Temperature grouping granularity in degrees C (default: 5.0)'
    )
    
    parser.add_argument(
        '--graph',
        action='store_true',
        help='Display a graph of continuous temperature vs. time'
    )
    
    parser.add_argument(
        '--histogram',
        action='store_true',
        help='Display a histogram of CCD temperatures'
    )
    
    args = parser.parse_args()
    
    # Extract data from FITS files
    data_list, date_temp_map = extract_fits_data(args.directory, args.granularity)
    
    if not data_list:
        print("No valid FITS files found with CCD-TEMP and DATE-OBS headers.", file=sys.stderr)
        sys.exit(1)
    
    # Print summary
    print_date_temperature_summary(date_temp_map, args.granularity)
    
    # Generate plots if requested
    if args.graph:
        plot_temperature_vs_time(data_list)
    
    if args.histogram:
        plot_temperature_histogram(data_list)


if __name__ == '__main__':
    main()

