#!/usr/bin/env python3
import subprocess
import cv2
import os
import numpy as np
import platform
import sys

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import adjust_focus, set_focus, get_focus

KEY_MAP = {
    'left': 2,
    'right': 3
}

def find_star(image):
    # Convert to grayscale and normalize
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)

    # Apply a blur to reduce noise
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Find the brightest point
    (minVal, maxVal, minLoc, maxLoc) = cv2.minMaxLoc(blurred)
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

def capture_image():
    global iso, shutter_speed
    # print(f'Capturing image with iso={iso}, shutter_speed={shutter_speed}')
    # Capture in desired iso, aperture, and shutter speed, pipe output to /dev/null.
    result = subprocess.run(['gphoto2',
                              '--set-config', f'iso={iso}',
                              '--set-config', f'shutterspeed={shutter_speed}',
                              '--capture-image-and-download',
                              '--filename', 'tmp.jpg',
                              '--force-overwrite'], stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        print("Error capturing image.")
        exit(1)

def display_image(window_name, image_path):
    image = cv2.imread(image_path)
    # image_scaled = cv2.resize(image, (0, 0), fx=0.15, fy=0.15)
    cv2.imshow(window_name, image)
    # Resize the window to fit the screen.
    cv2.resizeWindow(window_name, 1500, 1000)
    return image

def update_images():
    global main_image, zoom_location, main_window_name
    capture_image()
    main_image = display_image(main_window_name, 'tmp.jpg')
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

def setup_camera():
    # Set the camera to JPEG mode
    subprocess.run(['gphoto2', '--set-config', '/main/imgsettings/imageformat=0'])
    # Set the camera to manual mode
    subprocess.run(['gphoto2', '--set-config', '/main/capturesettings/autoexposuremodedial=Manual'])

def update_keymap():
    global KEY_MAP
    if platform.system() == 'Darwin':
        KEY_MAP['left'] = 2
        KEY_MAP['right'] = 3
    elif platform.system() == 'Linux':
        KEY_MAP['left'] = 81
        KEY_MAP['right'] = 83

def auto_focus(device, initial_focus, step_size):
    global main_image, zoom_location, main_window_name
    abs_min_focus = initial_focus - 4 * step_size
    set_focus(device, abs_min_focus)
    max_laplacian = 0
    min_step_size = 10
    best_focus = initial_focus
    while step_size > min_step_size:
        set_focus(device, abs_min_focus)
        min_focus = best_focus - 2 * step_size
        max_focus = best_focus + 2 * step_size
        print(f'Focusing from {min_focus} to {max_focus} in steps of {step_size}')
        for focus in range(min_focus, max_focus + 1, step_size):
            set_focus(device, focus)
            capture_image()
            main_image = display_image(main_window_name, 'tmp.jpg')
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
    set_focus(device, abs_min_focus)
    set_focus(device, best_focus)
    capture_image()
    main_image = display_image(main_window_name, 'tmp.jpg')
    laplacian, fwhm = update_zoomed_image(*zoom_location)
    if fwhm is None:
        fwhm = 0
    print(f'Final focus: {best_focus} | Laplacian: {laplacian:9.3f} | FWHM: {fwhm:9.3f}')
    return best_focus
    
def main():
    global iso, aperture, shutter_speed, zoom_factor
    iso = 100
    shutter_speed = '1/20'
    zoom_factor = 8
    device = 'ASI EAF'
    # create_settings_window()

    global main_image, main_window_name, zoom_window_name, zoom_location
    zoom_location = (0, 0)
    main_image = None
    main_window_name = "DSLR Viewer"
    zoom_window_name = "Zoomed View"
    setup_camera()
    update_keymap()
    cv2.namedWindow(main_window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(main_window_name, click_event)
    # Move the main window to the right of the left window.
    cv2.moveWindow(main_window_name, 400, 0)
    print('Current ISO: %d' % iso)


    print('Press Spacebar to capture an image, ESC to exit.')
    print('-' * 60)
    print('Focus controls: |   Closer   |   Farther   |')
    print('          Fine  | Left arrow | Right arrow |')
    print('        Medium  |    , key   |    . key    |')
    print('        Coarse  |    [ key   |    ] key    |')
    print('-' * 60)
    print('Pan controls:   |     Up     |    Down     |   Left   |   Right  |')
    print('                |   w key    |   s key     |  a key   |  d key   |')
    print('-' * 60)
    print('Zoom controls:  |   Zoom in  |  Zoom out   |')
    print('                |   q key    |   e key     |')
    print('-' * 60)
    print('ISO controls:   |  Decrease  |  Increase   |')
    print('                |   z key    |    x key    |')
    print('-' * 60)
    
    update_images()

    # Move the zoom window to the top left corner of the screen.
    cv2.moveWindow(zoom_window_name, 0, 0)
    
    print('Current focus: %d' % get_focus(device))
    while True:
        key = cv2.waitKey(1)
        # if key > 0:
        #     print(key)
        if key == 32:  # Spacebar key code
            update_images()

        if key == 27:  # ESC key to exit
            break

        # Left key: focus closer +1
        if key == KEY_MAP['left']:
            adjust_focus(device, 10)
            update_images()
        
        # Right key: focus further -1
        if key == KEY_MAP['right']:
            adjust_focus(device, -10)
            update_images()

        # "[" key: focus closer +3
        if key == 91:
            adjust_focus(device, 100)
            update_images()

        # "]" key: focus further -3
        if key == 93:
            adjust_focus(device, -100)
            update_images()

        # "." key: focus closer +2
        if key == 46:
            adjust_focus(device, 50)
            update_images()

        # "," key: focus further -2
        if key == 44:
            adjust_focus(device, -50)
            update_images()

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
            update_images()

        # "x" key: Increase ISO
        if key == 120:
            if iso < 51200:
                iso = iso * 2
            print('Current ISO: %d' % iso)
            update_images()

        # "f" key: Auto focus
        if key == 102:
            initial_focus = get_focus(device)
            step_size = 100
            auto_focus(device, initial_focus, step_size)
        
        if key > 0:
            print('Current focus: %d' % get_focus(device))

    # Delete the temporary image file
    os.remove('tmp.jpg')
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
