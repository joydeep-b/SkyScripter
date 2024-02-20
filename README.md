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
* A Python3 environment (tested with 3.9.12 on Mac OS and 3.10.12 on Ubuntu 22.04)
* The following Python packages: 
    * `astroquery`
    * `argparse`
    * `astropy`
    * `matplotlib`
    * `opencv-python`
    * `numpy` 
  
You can install these with `pip3 install -r requirements.txt`


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
* `auto_meridian_flip.py` - Monitors the mount, and automatically performs a meridian flip when the telescope crosses the meridian.


## Analysis Scripts

* `python batch_solve.py` - Batch plate-solving and star analysis of images in a directory, useful for extracting metrics of image quality (number of stars, FWHM) during a session. Sample graph:
![Sample graph](sample_data/star_data_example.png)

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
