import subprocess
import sys
import time

def read_indi(device, propname, timeout=2):
  # Call indi_getprop to get the property value
  command = "indi_getprop -t %d \"%s.%s\"" % (timeout, device, propname)
  # Execute the command and get the output.
  output = subprocess.run(command, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
  # Check for multiple lines of output.
  lines = output.splitlines()
  if len(lines) == 1:
    # Parse the output to get the property value.
    output = output.split("=")[1].strip()
    return output  
  else:
    # Parse the output from each line to get all property values.
    output = []
    for line in lines:
      # Get key-value pair. 
      # Example:"SkyAdventurer GTi.RASTATUS.RAInitialized=Ok"
      # Key="RAInitialized" Value="Ok"
      key = line.split("=")[0].split(".")[-1]
      value = line.split("=")[1].strip()
      output.append((key, value))
    return output
  
def write_indi(device, propname, keys, values):
  # If passed a single key and value, convert them to lists.
  if not isinstance(keys, list):
    keys = [keys]
  if not isinstance(values, list):
    values = [values]
  if len(keys) != len(values):
    raise ValueError("Keys and values must have the same length.")
  values_str = ""
  for key, value in zip(keys, values):
    if len(values_str) > 0:
      values_str += ";"
    values_str += "%s=%s" % (key, value)

  command = "indi_setprop \"%s.%s.%s\"" % (device, propname, values_str)
  returncode = subprocess.call(command, shell=True)
  if returncode != 0:
    print("Error: command '%s' returned %d" % (command, returncode))
    sys.exit(1)

def goto(device, ra, dec):
  write_indi(device, "EQUATORIAL_EOD_COORD", ["RA", "DEC"], [ra, dec])

def get_focus(device):
  return int(read_indi(device, "ABS_FOCUS_POSITION.FOCUS_ABSOLUTE_POSITION"))

def set_focus(device, value):
  write_indi(device, "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", value)
  MAX_FOCUS_ERROR = 5
  current_value = get_focus(device)
  while abs(current_value - value) > MAX_FOCUS_ERROR:
    current_value = get_focus(device)
    time.sleep(0.25)

def adjust_focus(device, steps):
  focus_value = get_focus(device)
  if focus_value + steps < 0:
      print('ERROR: Focus value cannot be negative. Current:%d steps:%d ' % (focus_value, steps))
      return
  set_focus(device, focus_value + steps)
  print(f'New focus value: {focus_value + steps}')