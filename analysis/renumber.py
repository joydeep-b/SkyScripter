#!/usr/bin/env python

import argparse
import os
import sys

def get_args():
    parser = argparse.ArgumentParser(description='Renumber fits files')
    parser.add_argument('-d', '--directory', type=str, required=True,
        help='Directory to renumber fits files in')
    parser.add_argument('-p', '--prefix', type=str, required=True,
        help='Filename prefix')

    return parser.parse_args(), parser

def renumber_files(directory, prefix):
    # Find all prefix*.fit and prefix*.fits files in the directory
    files = [f for f in os.listdir(directory) if f.startswith(prefix) and (f.endswith('.fit') or f.endswith('.fits'))]
    files.sort()
    for i, file in enumerate(files):
        file_extenstion = file.split('.')[-1]
        new_name = f'{prefix}{i+1:03}.{file_extenstion}'
        if file == new_name:
            continue
        if os.path.exists(os.path.join(directory, new_name)):
            print(f'Error: {new_name} already exists')
            sys.exit(1)
        os.rename(os.path.join(directory, file), os.path.join(directory, new_name))
        print(f'Renamed {file} to {new_name}')

def main():
    args, parser = get_args()
    renumber_files(args.directory, args.prefix)

if __name__ == '__main__':
    main()