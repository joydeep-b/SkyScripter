# Astro GPhoto

Astro GPhoto is a collection of shell scripts to automate astrophotography with
a DSLR camera based on the [GPhoto](http://www.gphoto.org/) command line
tool. Additional utilities for batch plate-solving and focusing are also
included.

## Requirements

* A DSLR camera supported by GPhoto
* A computer with GPhoto installed -- tested with gphoto2 2.5.28.1 (compiled
  from source)
* Optional, required for focus tool and batch plate-solving: [OpenCV](https://opencv.org/) `pip install opencv-python`
* Optional, required for batch plate-solving: [Siril](https://siril.org/)
* Optional, required for PhD2 drift analysis: [PhD2](https://openphdguiding.org/)

### Mac OS

Mac OS users can install GPhoto with [Homebrew](https://brew.sh/):

```bash
brew install gphoto2
```
An annoying issue with Mac OS is that it automatically starts a daemon to
connect with the camera, and claims the camera USB device as soon as it is
connected, preventing GPhoto from accessing it. To prevent this, you can
disable the daemon with the following command:

```bash
sudo launchctl unload /System/Library/LaunchAgents/com.apple.ptpcamerad.plist
sudo launchctl disable gui/501/com.apple.ptpcamerad
```

Reference: https://discussions.apple.com/thread/254703577

## Capture Scripts

* `./batch_capture.sh` - Capture a batch sequence of images with the same settings.  
    Usage: `batch_capture.sh [-i ISO] [-a APERTURE] [-s SHUTTER] [-f] [-d IMAGE_DIR] [-n NUM] [-v] [-k]`  
    `-i ISO`: The ISO of the image (default: 100)  
    `-a APERTURE`: The aperture of the image (default: 5.6)  
    `-s SHUTTER`: The shutter speed of the image (default: 1/100)  
    `-f`: Force overwrite of existing files  
    `-d`: The directory to save the images (default: images)  
    `-n`: The number of images to capture (default: 1)  
    `-v`: View the image after capture  
    `-k`: Keep the image on the camera after capture  
    `-h`: Print this help message  
* `./remote_batch.sh` - Run the batch capture on a remote computer (e.g., an astrophotography computer connected to the rig), and copy over the results to the local compute once complete.  
    Usage: `remote_batch.sh [-i ISO] [-s SHUTTER] [-a APERTURE] [-c HOST] [-r REMOTE_DIR] [-l LOCAL_DIR]`  
    `-i ISO`: The ISO of the image (default: 800)  
    `-s SHUTTER`: The shutter speed of the image (default: 60)  
    `-a APERTURE`: The aperture of the image (default: 8)  
    `-c HOST`: The hostname of the remote machine (default: astropc)  
    `-r REMOTE_DIR`: The directory to save the images on the remote machine (default: images)  
    `-l LOCAL_DIR`: The directory to save the images on the local machine (default: ~/Astrophotography/images)  
    `-g REMOTE_ASTRO_GPHOTO_DIR`: The directory of the astro_gphoto repository on the remote machine (default: ~/astro_gphoto)  
    `-h`: Print this help message  


## Other Utilities

* `python focus.py` - Focus assist tool for DSLR lenses. Requires OpenCV.
* `python batch_solve.py` - Batch plate-solving of images in a directory. Requires OpenCV and Siril.
   Usage: `batch_platesolve.py [-d DIRECTORY] [-o OBJECT] [-w WCS] [-f FOCAL] [-c CSV]`
  `-d DIRECTORY`: Directory containing images to platesolve
  `-o OBJECT`: Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")
  `-w WCS`: WCS coordinates
  `-f FOCAL`: Override focal length
  `-c CSV`: CSV file to write results to
