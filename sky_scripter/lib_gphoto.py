import subprocess
import sys
import time
import logging
import shutil

from sky_scripter.util import exec_or_fail

class GphotoClient:
  def __init__(self, simulate=False):
    if not simulate:
      exec_or_fail(['gphoto2', '--auto-detect'])
    self.simulate = simulate

  def set_config(self, settings):
    if self.simulate:
      return
    command = ['gphoto2']
    for key, value in settings.items():
      command.append('--set-config')
      command.append(f'{key}={value}')
    exec_or_fail(command)

  '''
  Initialize the camera with the provided image format and mode.
  image_format: 'RAW' or 'JPEG'
  mode: 'Manual' or 'Bulb'
  '''
  def initialize(self, image_format, mode, iso=None, shutter_speed=None):
    if self.simulate:
      return
    self.set_config({'/main/imgsettings/imageformat', image_format})
    self.set_config({'/main/capturesettings/autoexposuremodedial', mode})
    self.image_format = image_format
    self.mode = mode
    if iso is not None:
      self.set_config({'iso', iso})
    if shutter_speed is not None:
      self.set_config({'shutterspeed', shutter_speed})

  def capture_image(self, 
                    filename, 
                    iso=None, 
                    shutter_speed=None, 
                    image_format=None):
    if self.simulate:
      shutil.copy('sample_data/NGC2244.jpg', filename)
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
    command += ['--capture-image-and-download', 
                '--filename', 
                filename, 
                '--force-overwrite']
    exec_or_fail(command)

    