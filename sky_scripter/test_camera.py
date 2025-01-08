import time
from sky_scripter.lib_indi import IndiClient, IndiCamera
import logging
import argparse
from sky_scripter.util import init_logging, print_and_log, exec_or_fail

# Arguments: -f for filter, -t for temperature

def main():
  parser = argparse.ArgumentParser(description='Test camera functions')
  parser.add_argument('-f', '--filter', type=str, help='Filter name', default='L')
  parser.add_argument('-t', '--temperature', type=float, help='Temperature', default=None)
  parser.add_argument('--camera', type=str, help='Camera name', default='QHY CCD QHY268M-b93fd94')
  args = parser.parse_args()

  init_logging('camera_test', True)
  camera = IndiCamera(args.camera)

  camera.set_mode(5)
  camera.set_gain(56)
  camera.set_offset(20)
  if args.temperature is not None:
    camera.set_temperature(args.temperature)
  else:
    camera.cooler_off()

  print(f"Camera mode: {camera.get_mode()}")
  print(f"Camera gain: {camera.get_gain()}")
  print(f"Camera offset: {camera.get_offset()}")
  print(f"Camera humidity: {camera.get_humidity()}")
  print(f"Camera temperature: {camera.get_temperature()}")
  print(f"Filters: {camera.get_filter_names()}")

  print(f"Changing to {args.filter} filter")
  camera.change_filter(args.filter)

  while True:
    print(f"Temp. | Humidity: {camera.get_temperature():5.2f} | {camera.get_humidity():5.2f}")
    time.sleep(1)

if __name__ == '__main__':
  main()