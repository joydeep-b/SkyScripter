# SkyScripter

SkyScripter is a collection of scripts to automate setup, capture, and analysis for 
astrophotography. It builds upon several existing excellent astrophotography tools:
* [GPhoto](http://www.gphoto.org/) for image capture and camera control
* [Siril](https://siril.org/) for astrometry (plate-solving, star analysis) and stacking
* [PhD2](https://openphdguiding.org/) for auto-guiding
* [ASTAP](https://www.hnsky.org/astap.htm) for plate-solving
* [OpenCV](https://opencv.org/) for image analysis
* [LibINDI](https://indilib.org/) for controlling and querying astrophotography equipment

## Requirements
In addition to the above, you will need the following:
* A Python3 environment
* The following Python packages: `numpy`, `astropy`, `requests`, `opencv-python`, `pyindi`, `pyserial`
  You can install these with `pip install -r requirements.txt`


## Setup Scripts

* `startup.py` - Initializes the INDI telescope and camera, and sets the site information.
* `read_site.py` - Reads the current site information from the INDI telescope and prints it to the console.
* `goto.py` - Moves the telescope to a specified target.
* `capture_and_sync.py` - Captures an image and syncs the telescope to the target using plate-solving. 
* `align.py` - Aligns the telescope to a target using iterative capture, plate-solving, and adjustment.
* `focus_manual.py` - Assists with manual focusing of a telescope by capturing images, and displaying the number of stars and the mean FWHM.
* `set_tracking.py` - Sets the tracking mode of the telescope to sidereal, lunar, or solar, and turns tracking on or off.

## Capture Scripts

* `batch_capture.py` - Captures a series of images with the camera, and saves them to a directory.
## Other Utilities

* `python focus.py` - Focus assist tool for DSLR lenses. Requires OpenCV.
* `python batch_solve.py` - Batch plate-solving of images in a directory. Requires OpenCV and Siril.
   Usage: `batch_platesolve.py [-d DIRECTORY] [-o OBJECT] [-w WCS] [-f FOCAL] [-c CSV]`
  `-d DIRECTORY`: Directory containing images to platesolve
  `-o OBJECT`: Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")
  `-w WCS`: WCS coordinates
  `-f FOCAL`: Override focal length
  `-c CSV`: CSV file to write results to

### Notes

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
