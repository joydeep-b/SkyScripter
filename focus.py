import subprocess
import cv2
import os


def capture_image():
    # Replace 'filename.jpg' with your desired file path
    subprocess.run(['gphoto2', '--capture-image-and-download', '--filename', 'tmp.jpg', '--force-overwrite'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def display_image(window_name, image_path):
    image = cv2.imread(image_path)
    cv2.imshow(window_name, image)
    return image

def update_images():
    global main_image, zoom_location, main_window_name
    # capture_image()
    main_image = display_image(main_window_name, 'tmp.jpg')
    update_zoomed_image(zoom_location[0], zoom_location[1])

def update_zoomed_image(x, y):
    global main_image, zoom_window_name
    zoomed_image = zoom_image(main_image, (x, y))
    cv2.imshow(zoom_window_name, zoomed_image)

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
    print(f'Laplacian: {laplacian:9.3f}')
    zoomed_img = cv2.resize(zoomed_img, window_size, interpolation=cv2.INTER_NEAREST)

    # Overlay the laplacian value on the image, and a black box behind it.
    cv2.rectangle(zoomed_img, (0, 0), (300, 25), (0, 0, 0), -1)
    cv2.putText(zoomed_img, f'Laplacian: {laplacian:9.3f}', (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 255), 2, cv2.LINE_AA)

    return zoomed_img

def click_event(event, x, y, flags, param):
    global main_image, zoom_window_name, zoom_location
    if event == cv2.EVENT_LBUTTONDOWN:
        update_zoomed_image(x, y)
        zoom_location = (x, y)
    elif event == cv2.EVENT_MOUSEMOVE:
        if flags == cv2.EVENT_FLAG_LBUTTON:
            update_zoomed_image(x, y)
            zoom_location = (x, y)

def main():
    # Capture in Large Fine JPEG mode.
    subprocess.run(['gphoto2', '--set-config', '/main/imgsettings/imageformat=0'])

    global main_image, main_window_name, zoom_window_name, zoom_location
    zoom_location = (0, 0)
    main_image = None
    main_window_name = "DSLR Viewer"
    zoom_window_name = "Zoomed View"
    cv2.namedWindow(main_window_name)
    cv2.setMouseCallback(main_window_name, click_event)


    print('Press Spacebar to capture an image, ESC to exit.')
    print('Focus controls: |   Closer   |   Farther   |')
    print('          Fine  | Left arrow | Right arrow |')
    print('        Medium  |    , key   |    . key    |')
    print('        Coarse  |    [ key   |    ] key    |')
    
    update_images()

    # Move the zoom window to the top left corner of the screen.
    cv2.moveWindow(zoom_window_name, 0, 0)
    
    while True:
        key = cv2.waitKey(1)
        # if key > 0:
        #     print(key)
        if key == 32:  # Spacebar key code
            update_images()

        if key == 27:  # ESC key to exit
            break

        # Left key: focus closer +1
        if key == 2:
            subprocess.run(['gphoto2', '--set-config', '/main/actions/manualfocusdrive=0'])
            update_images()
        
        # Right key: focus further -1
        if key == 3:
            subprocess.run(['gphoto2', '--set-config', '/main/actions/manualfocusdrive=4'])
            update_images()

        # "[" key: focus closer +3
        if key == 91:
            subprocess.run(['gphoto2', '--set-config', '/main/actions/manualfocusdrive=2'])
            update_images()

        # "]" key: focus further -3
        if key == 93:
            subprocess.run(['gphoto2', '--set-config', '/main/actions/manualfocusdrive=6'])
            update_images()

        # "." key: focus closer +2
        if key == 46:
            subprocess.run(['gphoto2', '--set-config', '/main/actions/manualfocusdrive=5'])
            update_images()

        # "," key: focus further -2
        if key == 44:
            subprocess.run(['gphoto2', '--set-config', '/main/actions/manualfocusdrive=1'])
            update_images()

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
