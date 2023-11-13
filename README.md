# Astro GPhoto

Astro GPhoto is a collection of shell scripts to automate astrophotography with
a DSLR camera based on the [GPhoto](http://www.gphoto.org/) command line
tool.

## Requirements

* A DSLR camera supported by GPhoto
* A computer with GPhoto installed
* [OpenCV](https://opencv.org/): `pip install opencv-python`

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

## Usage

* `./single_capture.sh` - Capture a single image
* `./get_all_settings.sh` - Get all camera settings
* `./sequence_capture.sh` - Capture a sequence of images