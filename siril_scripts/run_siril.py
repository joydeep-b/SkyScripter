#!/usr/bin/env python

import os
import sys
import subprocess

if sys.platform == 'darwin':
  SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'
else:
  SIRIL_PATH = 'siril-cli'

def main():
  # The first argument is the script name.
  if len(sys.argv) < 3:
    print('Usage: run_siril.py <script_name> <directory>')
    sys.exit(1)
  script_name = sys.argv[1]
  directory = sys.argv[2]
  script_content = open(script_name).read()
  # Get full absolute path to the specified directory.
  directory = os.path.abspath(directory)
  # Run the script in the specified directory, piping stdout and stderr to the console.
  subprocess.run([SIRIL_PATH, '-d', directory, '-s', "-"], input=script_content, text=True)

if __name__ == '__main__':
  main()