#!/usr/bin/env python3
import subprocess
import cv2
import os
import numpy as np
import platform
import sys
import argparse
import tempfile

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiFocuser
from sky_scripter.lib_gphoto import GphotoClient

KEY_MAP = {
  'left': 2,
  'right': 3
}

def find_star(image):
  # Convert to grayscale and normalize
  gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
  gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
  # Save grayscale image for debugging
  cv2.imwrite('gray.jpg', gray)
  # Find the brightest point
  (minVal, maxVal, minLoc, maxLoc) = cv2.minMaxLoc(gray)
  # Find the centroid of the pixels that have the maximum value.
  maxValPixels = np.array(np.where(gray == maxVal)).T
  x = np.mean(maxValPixels[:, 0])
  y = np.mean(maxValPixels[:, 1])
  return (int(y), int(x)), maxVal

  return maxLoc, maxVal


def compute_fwhm(image, star_location, max_val):
  x, y = star_location
  row = image[y, :]
  max_intensity = max_val
  # Find the half max value
  half_max = max_intensity / 2
  # print(f'Max intensity: {max_intensity:9.3f}')
  # print(f'x: {x}, y: {y}')
  left_crossings = np.where(row[:x] < half_max)[0]
  right_crossings = np.where(row[x:] < half_max)[0]
  # Check if crossings are found
  if left_crossings.size > 0 and right_crossings.size > 0:
    left_idx = left_crossings[-1]
    right_idx = x + right_crossings[0]
    fwhm = right_idx - left_idx
    return fwhm
  else:
    # Handle case where FWHM cannot be computed
    return None

def display_image(window_name, image_path):
  image = cv2.imread(image_path)
  # image_scaled = cv2.resize(image, (0, 0), fx=0.15, fy=0.15)
  cv2.imshow(window_name, image)
  # Resize the window to fit the screen.
  cv2.resizeWindow(window_name, 1500, 1000)
  return image

def update_images(camera):
  global main_image, zoom_location, main_window_name, iso
  with tempfile.TemporaryDirectory() as tempdir:
    image_file = os.path.join(tempdir, 'tmp.jpg')
    camera.capture_image(image_file, iso=iso)
    main_image = display_image(main_window_name, image_file)
    update_zoomed_image(zoom_location[0], zoom_location[1])

def update_zoomed_image(x, y):
  global main_image, zoom_window_name, zoom_factor
  zoomed_image, laplacian, fwhm = zoom_image(main_image, (x, y), zoom_factor)
  cv2.imshow(zoom_window_name, zoomed_image)
  return laplacian, fwhm

def zoom_image(image, click_point, zoom_factor=8, window_size=(400, 400)):
  # Calculate the zoomed area dimensions
  x, y = click_point
  width, height = image.shape[1], image.shape[0]
  zoom_width, zoom_height = window_size[0] // zoom_factor, window_size[1] // zoom_factor
  
  # Define the ROI
  x1 = max(x - zoom_width // 2, 0)
  y1 = max(y - zoom_height // 2, 0)
  x2 = min(x1 + zoom_width, width)
  y2 = min(y1 + zoom_height, height)
  
  # Crop and resize the image
  zoomed_img = image[y1:y2, x1:x2]
  # Compute sum of laplacian to check if the image is blurry
  laplacian = cv2.Laplacian(zoomed_img, cv2.CV_64F).var()
  # Print the laplacian value in format %9.3f
  zoomed_img = cv2.resize(zoomed_img, window_size, interpolation=cv2.INTER_NEAREST)

  # Star detection
  star_location, max_val = find_star(zoomed_img)

  # Compute FWHM
  fwhm = compute_fwhm(zoomed_img, star_location, max_val)

  # Draw a crosshair and circle around the star
  cv2.drawMarker(zoomed_img, star_location, (0, 0, 255), cv2.MARKER_CROSS, 10, 2)
  if fwhm:
    cv2.circle(zoomed_img, star_location, fwhm // 2, (0, 0, 255), 2)

  # Overlay the laplacian and FWHM values on the image, and a black box behind it.
  cv2.rectangle(zoomed_img, (0, 0), (300, 25), (0, 0, 0), -1)
  cv2.putText(zoomed_img, f'Laplacian: {laplacian:9.3f}', (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 255), 2, cv2.LINE_AA)
  if fwhm:
    fwhm = fwhm / zoom_factor
    cv2.rectangle(zoomed_img, (0, 25), (300, 50), (0, 0, 0), -1)
    cv2.putText(zoomed_img, f'FWHM: {fwhm:9.3f}', (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 255), 2, cv2.LINE_AA)

  if fwhm:
    print(f'Laplacian: {laplacian:9.3f} | FWHM: {fwhm:9.3f}')
  else:    
    print(f'Laplacian: {laplacian:9.3f} | FWHM: None')
  return zoomed_img, laplacian, fwhm

def click_event(event, x, y, flags, param):
  global main_image, zoom_window_name, zoom_location
  if event == cv2.EVENT_LBUTTONDOWN:
    update_zoomed_image(x, y)
    zoom_location = (x, y)
  elif event == cv2.EVENT_MOUSEMOVE:
    if flags == cv2.EVENT_FLAG_LBUTTON:
      update_zoomed_image(x, y)
      zoom_location = (x, y)

def update_keymap():
  global KEY_MAP
  if platform.system() == 'Darwin':
    KEY_MAP['left'] = 2
    KEY_MAP['right'] = 3
  elif platform.system() == 'Linux':
    KEY_MAP['left'] = 81
    KEY_MAP['right'] = 83

def auto_focus(focuser, camera, initial_focus, step_size):
  global main_image, zoom_location, main_window_name, iso
  abs_min_focus = initial_focus - 4 * step_size
  focuser.set_focus(abs_min_focus)
  max_laplacian = 0
  min_step_size = 10
  best_focus = initial_focus
  with tempfile.TemporaryDirectory() as tempdir:
    image_file = os.path.join(tempdir, 'tmp.jpg')
    while step_size > min_step_size:
      focuser.set_focus(abs_min_focus)
      min_focus = best_focus - 2 * step_size
      max_focus = best_focus + 2 * step_size
      print(f'Focusing from {min_focus} to {max_focus} in steps of {step_size}')
      for focus in range(min_focus, max_focus + 1, step_size):
        focuser.set_focus(focus)
        camera.capture_image(image_file)
        main_image = display_image(main_window_name, image_file)
        laplacian, fwhm = update_zoomed_image(*zoom_location)
        cv2.waitKey(1)
        if fwhm is None:
          fwhm = 0
        print(f'Focus: {focus} | Laplacian: {laplacian:9.3f} | FWHM: {fwhm:9.3f}')
        if laplacian > max_laplacian:
          max_laplacian = laplacian
          best_focus = focus
      step_size = step_size // 2
    print(f'Best focus: {best_focus} | Max Laplacian: {max_laplacian:9.3f}')
    focuser.set_focus(abs_min_focus)
    focuser.set_focus(best_focus)
    camera.capture_image(image_file)
    main_image = display_image(main_window_name, image_file)
  laplacian, fwhm = update_zoomed_image(*zoom_location)
  if fwhm is None:
    fwhm = 0
  print(f'Final focus: {best_focus} | Laplacian: {laplacian:9.3f} | FWHM: {fwhm:9.3f}')
  return best_focus
  
def main():
  global zoom_factor, iso, main_image, main_window_name, zoom_window_name, \
      zoom_location
  parser = argparse.ArgumentParser(description='Manually focus a telescope using a camera and star FWHM detection')
  parser.add_argument('-d', '--device', type=str, 
                      help='INDI focuser device name', default='ASI EAF')
  parser.add_argument('-i', '--iso', type=int, 
                      help='ISO setting for camera', default=1600)
  parser.add_argument('-e', '--exposure', type=str, 
                      help='Exposure time for camera', default='2')
  parser.add_argument('-s', '--simulate', action='store_true',
                      help='Simulate camera capture')
  args = parser.parse_args()

  zoom_factor = 8
  focuser = IndiFocuser(args.device, simulate=args.simulate)
  camera = GphotoClient(simulate=args.simulate)
  iso = args.iso
  camera.initialize(image_format='Large Fine JPEG', 
                    mode='Manual', 
                    iso=args.iso, 
                    shutter_speed=args.exposure)

  zoom_location = (0, 0)
  main_image = None
  main_window_name = "DSLR Viewer"
  zoom_window_name = "Zoomed View"

  update_keymap()
  cv2.namedWindow(main_window_name, cv2.WINDOW_NORMAL)
  cv2.setMouseCallback(main_window_name, click_event)
  # Move the main window to the right of the left window.
  cv2.moveWindow(main_window_name, 400, 0)
  print('Current ISO: %d' % iso)


  print('Press Spacebar to capture an image, ESC to exit.')
  print('-' * 60)
  print('Focus controls: |   Closer   |   Farther   |')
  print('      Fine  | Left arrow | Right arrow |')
  print('    Medium  |  , key   |  . key  |')
  print('    Coarse  |  [ key   |  ] key  |')
  print('-' * 60)
  print('Pan controls:   |   Up   |  Down   |   Left   |   Right  |')
  print('        |   w key  |   s key   |  a key   |  d key   |')
  print('-' * 60)
  print('Zoom controls:  |   Zoom in  |  Zoom out   |')
  print('        |   q key  |   e key   |')
  print('-' * 60)
  print('ISO controls:   |  Decrease  |  Increase   |')
  print('        |   z key  |  x key  |')
  print('-' * 60)
  
  update_images(camera)
  
  # Move the zoom window to the top left corner of the screen.
  cv2.moveWindow(zoom_window_name, 0, 0)
  
  print('Current focus: %d' % focuser.get_focus())
  
  while True:
    key = cv2.waitKey(1)
    # if key > 0:
    #   print(key)
    if key == 32:  # Spacebar key code
      update_images(camera)

    if key == 27:  # ESC key to exit
      break

    # Left key: focus closer +1
    if key == KEY_MAP['left']:
      focuser.adjust_focus(10)
      update_images(camera)
    
    # Right key: focus further -1
    if key == KEY_MAP['right']:
      focuser.adjust_focus(-10)
      update_images(camera)

    # "[" key: focus closer +3
    if key == 91:
      focuser.adjust_focus(100)
      update_images(camera)

    # "]" key: focus further -3
    if key == 93:
      focuser.adjust_focus(-100)
      update_images(camera)

    # "." key: focus closer +2
    if key == 46:
      focuser.adjust_focus(50)
      update_images(camera)

    # "," key: focus further -2
    if key == 44:
      focuser.adjust_focus(-50)
      update_images(camera)

    # Pan left
    if key == 97: # a key
      zoom_location = (zoom_location[0] - 10, zoom_location[1])
      update_zoomed_image(*zoom_location)

    # Pan right
    if key == 100: # d key
      zoom_location = (zoom_location[0] + 10, zoom_location[1])
      update_zoomed_image(*zoom_location)

    # Pan up
    if key == 119: # w key
      zoom_location = (zoom_location[0], zoom_location[1] - 10)
      update_zoomed_image(*zoom_location)

    # Pan down
    if key == 115: # s key
      zoom_location = (zoom_location[0], zoom_location[1] + 10)
      update_zoomed_image(*zoom_location)

    # "q" key: zoom in
    if key == 113:
      if zoom_factor > 1:
        zoom_factor = zoom_factor // 2
      # print(f'Zoom factor: {zoom_factor}')
      update_zoomed_image(*zoom_location)

    # "e" key: zoom out
    if key == 101:
      if zoom_factor < 64:
        zoom_factor = zoom_factor * 2
      # print(f'Zoom factor: {zoom_factor}')
      update_zoomed_image(*zoom_location)

    # "z" key: Decrease ISO
    if key == 122:
      if iso > 100:
        iso = iso // 2
      print('Current ISO: %d' % iso)
      update_images(camera)

    # "x" key: Increase ISO
    if key == 120:
      if iso < 51200:
        iso = iso * 2
      print('Current ISO: %d' % iso)
      update_images(camera)

    # "f" key: Auto focus
    if key == 102:
      initial_focus = focuser.get_focus()
      step_size = 100
      auto_focus(focuser, camera, initial_focus, step_size)
    
    if key > 0:
      print('Current focus: %d' % focuser.get_focus())

  cv2.destroyAllWindows()

if __name__ == "__main__":
  main()
