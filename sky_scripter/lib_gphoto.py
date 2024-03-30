import subprocess
import sys
import time
import logging
import shutil
from typing import Tuple, Literal

from sky_scripter.util import exec_or_fail

class GphotoClient:
  def __init__(self, 
               image_format: Literal['RAW', 'JPEG'],
               mode: Literal['Manual', 'Bulb'],
               iso: int | None = None, 
               shutter_speed: str | None = None, 
               simulate: bool = False):
    if image_format not in ['RAW', 'JPEG']:
      logging.warning(f"Unknown image format: {image_format}")
    if mode not in ['Manual', 'Bulb']:
      logging.warning(f"Unknown mode: {mode}")
    if not simulate:
      exec_or_fail(['gphoto2', '--auto-detect'])
    self.simulate = simulate
    self.image_format = image_format
    self.mode = mode
    self.iso = iso
    self.shutter_speed = shutter_speed

  def set_config(self, settings: dict):
    if self.simulate:
      return
    command = ['gphoto2']
    for key, value in settings.items():
      command.append('--set-config')
      command.append(f'{key}="{value}"')
    exec_or_fail(command)

  '''
  Initialize the camera with the provided image format and mode.
  image_format: 'RAW' or 'JPEG'
  mode: 'Manual' or 'Bulb'
  '''
  def initialize(self):
    if self.simulate:
      return
    self.set_config({'/main/capturesettings/autoexposuremodedial': self.mode})
    self.set_config({'/main/imgsettings/imageformat': self.image_format})
    if self.iso is not None:
      self.set_config({'iso': self.iso})
    if self.shutter_speed is not None and self.mode == 'Manual':
      self.set_config({'shutterspeed': self.shutter_speed})

  def capture_image(self, 
                    filename: str, 
                    iso: int | None = None,
                    shutter_speed: str | float | None = None,
                    image_format: Literal['RAW', 'JPEG'] | None = None):
    if self.simulate:
      file_extension = filename.split('.')[-1].lower()
      jpg_extensions = ['jpg', 'jpeg']
      raw_extensions = ['cr2', 'cr3', 'raw']
      if shutter_speed is not None and type(shutter_speed) == str:
        shutter_speed = eval(shutter_speed)
      elif self.shutter_speed is not None and type(self.shutter_speed) == str:
        shutter_speed = eval(self.shutter_speed)
      # Simulate the capture by waiting for the shutter speed and then copying the sample image.
      time.sleep(shutter_speed + 2)
      if file_extension in jpg_extensions:
        shutil.copy('sample_data/NGC2244.jpg', filename)
      elif file_extension in raw_extensions:
        shutil.copy('sample_data/NGC2244.cr3', filename)
      else:
        logging.warning(f"Unknown file extension: {file_extension}, assuming CR3 format.")
        shutil.copy('sample_data/NGC2244.cr3', filename)
      return

    command = ['gphoto2']
    if iso is not None:
      command.append('--set-config')
      command.append(f'iso={iso}')
    if shutter_speed is not None:
      command.append('--set-config')
      command.append(f'shutterspeed={shutter_speed}')
    if image_format is not None:
      command.append('--set-config')
      command.append(f'/main/imgsettings/imageformat={image_format}')
    if self.mode == 'Manual':
      command += ['--capture-image-and-download', 
                  '--filename', 
                  filename, 
                  '--force-overwrite']
      exec_or_fail(command)
    elif self.mode == 'Bulb':
      # First, write any specified configuration to the camera.
      if len(command) > 1:
        exec_or_fail(command)
      if type(shutter_speed) == str:
        shutter_decimal = eval(shutter_speed)
      elif type(shutter_speed) == int or type(shutter_speed) == float:
        shutter_decimal = shutter_speed
      else:
        shutter_decimal = self.shutter_speed
      # Then, capture the image using bulb mode.
      command = ['gphoto2',
                 '--set-config', 'eosremoterelease=Immediate',
                 f'--wait-event={shutter_decimal}s',
                 '--set-config', 'eosremoterelease="Release Full"',
                 '--wait-event-and-download=1s',
                 '--filename', filename,
                 '--force-overwrite'] 
      # print(command)
      exec_or_fail(command)
    else:
      logging.error(f"Unknown mode '{self.mode}' for capturing image.")
    