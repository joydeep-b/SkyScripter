import subprocess

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
  if len(keys) != len(values):
    raise ValueError("Keys and values must have the same length.")
  values_str = ""
  for key, value in zip(keys, values):
    if len(values_str) > 0:
      values_str += ";"
    values_str += "%s=%s" % (key, value)

  command = "indi_setprop \"%s.%s.%s\"" % (device, propname, values_str)
  return subprocess.call(command, shell=True)
  