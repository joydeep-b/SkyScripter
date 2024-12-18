#!/bin/bash

# Takes just one argument, the Lights directory.
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <Lights directory>"
    exit 1
fi

LIGHTS_DIR=$1/Light
# Ensure the directory exists.
if [ ! -d "$LIGHTS_DIR" ]; then
    echo "Directory $LIGHTS_DIR does not exist."
    exit 1
fi
PROCESS_DIR=$LIGHTS_DIR/.process

echo "Lights directory: $LIGHTS_DIR"
echo "Process directory: $PROCESS_DIR"

# Ask for confirmation.
read -p "Do you want to continue? " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

# Delete the process directory if it exists.
if [ -d "$PROCESS_DIR" ]; then
    echo "Deleting existing process directory."
    rm -r $PROCESS_DIR
fi

./siril_scripts/run_siril.py siril_scripts/preprocess_broadband.ssf $LIGHTS_DIR/B
rm -r $PROCESS_DIR
./siril_scripts/run_siril.py siril_scripts/preprocess_broadband.ssf $LIGHTS_DIR/G
rm -r $PROCESS_DIR
./siril_scripts/run_siril.py siril_scripts/preprocess_broadband.ssf $LIGHTS_DIR/R
rm -r $PROCESS_DIR
./siril_scripts/run_siril.py siril_scripts/preprocess_broadband.ssf $LIGHTS_DIR/L
