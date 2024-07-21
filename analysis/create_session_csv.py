#!/usr/bin/env python3

# This script takes a directory name, and checks all the fits files under the sub-directories. It
# then creates a csv file with the astrophotography session information based on the files found.

import csv
from astropy.io import fits
from datetime import datetime
from tqdm import tqdm

default_values = {
    'bortle': '8',
    'darks': '100',
    'flats': '100',
    'bias': '100',
}

filter_lookup = {
    'L': 4452,
    'R': 4457,
    'G': 4447,
    'B': 4442
}

class Session:
    def __init__(self, date, filter, duration, gain, sensorCooling, darks, flats, bias, bortle, temperature):
        self.date = date
        self.filter = filter_lookup[filter]
        self.number = 0
        self.duration = duration
        self.gain = gain
        self.sensorCooling = int(sensorCooling +0.5)
        self.darks = darks
        self.flats = flats
        self.bias = bias
        self.bortle = bortle
        self.temperature = temperature

    # Static method to display the header of the csv file.
    @staticmethod
    def header():
        return ['date', 'filter', 'number', 'duration', 'gain', 'sensorCooling', 'darks', 'flats', 'bias', 'bortle', 'temperature']

    def __str__(self):
        datestr = self.date.strftime('%Y-%m-%d')
        return f'{datestr},{self.filter},{self.number},{self.duration},{self.gain},{self.sensorCooling},{self.darks},{self.flats},{self.bias},{self.bortle},{self.temperature}'

    # Add an increment oparator to increment the number of images taken.
    def __iadd__(self, other):
        self.number += other
        return self

    # Comparison operators to sort the sessions by date.
    def __lt__(self, other):
        return self.date < other.date

    # Equality operator to compare the sessions.
    def __eq__(self, other):
        return self.date == other.date and \
          self.filter == other.filter and \
          self.duration == other.duration and \
          self.gain == other.gain

    def __iter__(self):
        return iter([self.date, self.filter, self.number, self.duration, self.gain, self.sensorCooling, self.darks, self.flats, self.bias, self.bortle, self.temperature])

def save_session_csv(sessions, output_file):
    for header in Session.header():
        print(header, end='')
        if header != Session.header()[-1]:
            print(',', end='')
        else:
            print()
    for session in sessions:
        print(session)
    return

def get_session_data(directory):
    # List the files in the sub-directories.
    subdirs = ['L', 'R', 'G', 'B', 'Halpha', 'OIII', 'SII']
    sessions = []
    for subdir in subdirs:
        # See if the sub-directory exists.
        if not (directory / subdir).exists():
            continue
        # print(f'Processing {subdir} files.')
        files = directory.glob(f'**/{subdir}/*.fits')
        # Use TQDM to show a progress bar.
        for file in files:
            # print(f'Processing {file}')
            with fits.open(file) as hdul:
                header = hdul[0].header
                date = datetime.strptime(header['DATE-OBS'], '%Y-%m-%dT%H:%M:%S.%f').date()
                filter = header['FILTER']
                duration = header['EXPTIME']
                gain = header['GAIN']
                sensorCooling = header['CCD-TEMP']
                temperature = header['FOCUSTEM']
                session = Session(date, filter, duration, gain, sensorCooling,
                                  default_values['darks'], default_values['flats'],
                                  default_values['bias'], default_values['bortle'], temperature)
                if session in sessions:
                    index = sessions.index(session)
                    sessions[index] += 1
                else:
                    sessions.append(session)
    sessions.sort()
    return sessions


def main():
    # Parse the command line arguments.
    import argparse
    parser = argparse.ArgumentParser(description='Create a csv file with astrophotography session information.')
    parser.add_argument('dir', type=str, help='Directory containing the fits files.')
    parser.add_argument('-out', type=str, help='Output csv file.')
    args = parser.parse_args()

    print(f'Creating csv file with session data from {args.dir}.')
    # Get the session data.
    from pathlib import Path
    directory = Path(args.dir)
    sessions = get_session_data(directory)

    # Save the session data to a csv file.
    save_session_csv(sessions, args.out)

if __name__ == '__main__':
    main()