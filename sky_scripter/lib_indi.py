import os
import subprocess
import sys
import time
import logging
from astropy.coordinates import SkyCoord, EarthLocation, AltAz
from astropy.time import Time
import astropy.units as u

from typing import Tuple, Literal

from sky_scripter.util import exec_or_fail

class IndiClient:
  def __init__(self, device: str, simulate: bool = False):
    self.device = device
    self.simulate = simulate

  def read(self, propname: str, timeout: float = 2) -> str | list:
    if self.simulate:
      return 0
    # Call indi_getprop to get the property value
    command = "indi_getprop -t %d \"%s.%s\"" % (timeout, self.device, propname)
    # Execute the command and get the output.
    output = exec_or_fail(command)
    # Check for multiple lines of output.
    lines = output.splitlines()
    if len(lines) == 1:
      # Parse the output to get the property value.
      output = output.split("=")[1].strip()
      return output
    else:
      # Parse the output from each line to get all property values.
      output = []
      for line in lines:
        # Get key-value pair.
        # Example:"SkyAdventurer GTi.RASTATUS.RAInitialized=Ok"
        # Key="RAInitialized" Value="Ok"
        key = line.split("=")[0].split(".")[-1]
        value = line.split("=")[1].strip()
        output.append((key, value))
      return output

  def write(self, propname: str, keys: list | str, values: list | str):
    if self.simulate:
      return
    # If passed a single key and value, convert them to lists.
    if not isinstance(keys, list):
      keys = [keys]
    if not isinstance(values, list):
      values = [values]
    if len(keys) != len(values):
      raise ValueError("Keys and values must have the same length.")
    values_str = ""
    for key, value in zip(keys, values):
      if len(values_str) > 0:
        values_str += ";"
      values_str += "%s=%s" % (key, value)

    command = "indi_setprop \"%s.%s.%s\"" % (self.device, propname, values_str)
    exec_or_fail(command)

class IndiFocuser(IndiClient):
  def get_focus(self) -> int:
    return int(self.read("ABS_FOCUS_POSITION.FOCUS_ABSOLUTE_POSITION"))

  def set_focus(self, value, max_error=5, timeout=30):
    self.write("ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", value)
    current_value = self.get_focus()
    t_start = time.time()
    while abs(current_value - value) > max_error and \
        time.time() - t_start < timeout:
      time.sleep(0.25)
      current_value = self.get_focus()
    if abs(current_value - value) > max_error:
      logging.error(f"Focus value not reached in the desired time. Requested: {value} Current: {current_value}, time elapsed: {time.time() - t_start}, timeout: {timeout}")
    else:
      logging.info(f'New focus value: {current_value}')

  def adjust_focus(self, steps: int):
    focus_value = self.get_focus()
    if focus_value + steps < 0:
      logging.error('Focus value cannot be negative. Current:%d steps:%d ' % (focus_value, steps))
      print('ERROR: Focus value cannot be negative. Current:%d steps:%d ' % (focus_value, steps))
      return
    self.set_focus(focus_value + steps)
    logging.info(f'New focus value: {focus_value + steps}')

class IndiCamera(IndiClient):
  def __init__(self, device, simulate = False):
    super().__init__(device, simulate)
    # Get the location of this file.
    current_path = os.path.dirname(os.path.realpath(__file__))
    # Get the path to the indi_cam_client executable: it is in the same directory as this script.
    self.indi_cam_client = os.path.join(current_path, "..", "indi_cam_client", "indi_cam_client")
    self.mode = 5
    self.gain = 56
    self.offset = 20
    self.exposure = 120

  def set_capture_settings(self, mode, gain, offset, exposure):
    self.mode = mode
    self.gain = gain
    self.offset = offset
    self.exposure = exposure

  def get_filter_names(self):
    # There should be 7 filter slots, numbered 1-7, with names FILTER_NAME.FILTER_SLOT_NAME_1, etc.
    filter_names = []
    for i in range(1, 8):
      filter_name = self.read(f"FILTER_NAME.FILTER_SLOT_NAME_{i}")
      if filter_name:
        filter_names.append(filter_name)
    return filter_names

  def change_filter(self, filter_name):
    filter_names = self.get_filter_names()
    if filter_name not in filter_names:
      logging.error(f"Filter {filter_name} not found in filter wheel")
      return
    filter_index = filter_names.index(filter_name) + 1
    logging.info(f"Changing filter to {filter_name} (index {filter_index})")
    self.write("FILTER_SLOT", "FILTER_SLOT_VALUE", filter_index)
    timeout = 60
    t_start = time.time()
    while time.time() - t_start < timeout:
      current_filter = int(self.read("FILTER_SLOT.FILTER_SLOT_VALUE"))
      logging.info(f"Current filter: {current_filter}")
      if current_filter == filter_index:
        logging.info(f"Filter change successful, completed in {time.time() - t_start:.3f} seconds")
        return
      time.sleep(1)
    logging.error(f"Filter change timed out after {timeout} seconds")

  def get_humidity(self):
    return float(self.read("CCD_HUMIDITY.HUMIDITY"))

  def get_temperature(self):
    return float(self.read("CCD_TEMPERATURE.CCD_TEMPERATURE_VALUE"))

  def set_temperature(self, temperature):
    logging.info(f"Setting temperature to {temperature}")
    self.write("CCD_TEMPERATURE", "CCD_TEMPERATURE_VALUE", temperature)

  def cooler_off(self):
    logging.info("Turning cooler off")
    self.write("CCD_COOLER", "COOLER_OFF", "On")

  def capture_image(self, filename, gain=None, exposure=None):
    if gain is None:
      gain = self.gain
    if exposure is None:
      exposure = self.exposure
    logging.info(f"Capturing image to {filename}")
    command = f"{self.indi_cam_client} --output {filename} --mode {self.mode} --gain {gain} --offset {self.offset} --exposure {exposure}"
    exec_or_fail(command)

class IndiMount(IndiClient):
  def park(self):
    self.write("TELESCOPE_PARK", "PARK", "On")
    while self.read("TELESCOPE_PARK.PARK") != "On":
      time.sleep(1)

  def unpark(self):
    self.write("TELESCOPE_PARK", "UNPARK", "On")
    while self.read("TELESCOPE_PARK.UNPARK") != "On":
      time.sleep(1)

  def goto(self, ra, dec):
    self.write("ON_COORD_SET", "TRACK", "On")
    time.sleep(1)
    self.write("EQUATORIAL_EOD_COORD", ["RA", "DEC"], [ra, dec])
    t_start = time.time()
    def slew_finished():
      current_ra, current_dec = self.get_ra_dec()
      max_error = 10 / 3600 # Arcseconds
      if False:
        print(f"RA Error: {abs(current_ra - ra) * 3600:.3f} arcsec, DEC Error: {abs(current_dec - dec) * 3600:.3f} arcsec")
      return abs(current_ra - ra) < max_error and \
              abs(current_dec - dec) < max_error

    while not slew_finished():
      t_now = time.time()
      if t_now - t_start > 30:
        current_ra, current_dec = self.get_ra_dec()
        logging.error(f"Slew timeout. Requested: {ra} {dec} Read: {current_ra} {current_dec} Error: {abs(current_ra - ra) * 3600:.3f} {abs(current_dec - dec) * 3600:.3f}")
        return
      time.sleep(1)



  def sync(self, ra: float, dec: float):
    self.write("TELESCOPE_TRACK_STATE", "TRACK_ON", "On")
    self.write("ON_COORD_SET", "SYNC", "On")
    self.write("EQUATORIAL_EOD_COORD", ["RA", "DEC"], [ra, dec])
    time.sleep(1)
    ra_read, dec_read = self.get_ra_dec()
    if abs(ra - ra_read) > 0.001 or abs(dec - dec_read) > 0.001:
      logging.error(f"Sync failed. Requested: {ra} {dec} Read: {ra_read} {dec_read}")

  def get_tracking_state(self) -> Literal["TRACK_ON", "TRACK_OFF", "Unknown"]:
    track_state_on = self.read("TELESCOPE_TRACK_STATE.TRACK_ON")
    track_state_off = self.read("TELESCOPE_TRACK_STATE.TRACK_OFF")
    if track_state_on == "On":
      return "TRACK_ON"
    elif track_state_off == "On":
      return "TRACK_OFF"
    else:
      logging.error("Get tracking state: unknown state")
      return "Unknown"

  def start_tracking(self):
    self.write("TELESCOPE_TRACK_STATE", "TRACK_ON", "On")

  def stop_tracking(self):
    self.write("TELESCOPE_TRACK_STATE", "TRACK_OFF", "On")

  def get_tracking_mode(self) -> Literal["TRACK_SIDEREAL",
                                         "TRACK_LUNAR",
                                         "TRACK_SOLAR",
                                         "TRACK_CUSTOM",
                                         "Unknown"]:
    sidereal_tracking = self.read("TELESCOPE_TRACK_MODE.TRACK_SIDEREAL")
    lunar_tracking = self.read("TELESCOPE_TRACK_MODE.TRACK_LUNAR")
    solar_tracking = self.read("TELESCOPE_TRACK_MODE.TRACK_SOLAR")
    custom_tracking = self.read("TELESCOPE_TRACK_MODE.TRACK_CUSTOM")

    if sidereal_tracking == "On":
      return "TRACK_SIDEREAL"
    elif lunar_tracking == "On":
      return "TRACK_LUNAR"
    elif solar_tracking == "On":
      return "TRACK_SOLAR"
    elif custom_tracking == "On":
      return "TRACK_CUSTOM"
    else:
      logging.error("Get tracking mode: unknown mode")
      return "Unknown"

  def set_tracking_mode(self,
                        mode: Literal["TRACK_SIDEREAL",
                                      "TRACK_LUNAR",
                                      "TRACK_SOLAR",
                                      "TRACK_CUSTOM"]):
    if mode not in ["TRACK_SIDEREAL",
                    "TRACK_LUNAR",
                    "TRACK_SOLAR",
                    "TRACK_CUSTOM"]:
      logging.error("Set tracking mode: unknown mode")
      return
    self.write("TELESCOPE_TRACK_MODE", mode, "On")

  def get_ra_dec(self) -> Tuple[float, float]:
    ra = float(self.read("EQUATORIAL_EOD_COORD.RA"))
    dec = float(self.read("EQUATORIAL_EOD_COORD.DEC"))
    return ra, dec

  def get_alt_az(self) -> Tuple[float, float]:
    ra, dec = self.get_ra_dec()
    obs_time = Time.now()
    lat = float(self.read("GEOGRAPHIC_COORD.LAT"))
    lon = float(self.read("GEOGRAPHIC_COORD.LONG"))
    elevation = float(self.read("GEOGRAPHIC_COORD.ELEV"))

    # Create EarthLocation object for the observer
    location = EarthLocation(lat=lat*u.deg, lon=lon*u.deg, height=elevation*u.m)

    # Create a SkyCoord object for the RA/Dec
    # Assumes RA is in hours, dec is in degrees
    sky_coord = SkyCoord(ra=ra*u.hourangle, dec=dec*u.deg, frame='icrs')

    # Create AltAz frame for the given time and location
    altaz_frame = AltAz(obstime=obs_time, location=location)

    # Transform the SkyCoord (RA/Dec) into AltAz
    altaz_coord = sky_coord.transform_to(altaz_frame)

    # Extract altitude and azimuth in degrees
    alt = altaz_coord.alt.degree
    az  = altaz_coord.az.degree

    return alt, az

  def get_pier_side(self) -> Literal["West", "East", "Unknown"]:
    if self.read("TELESCOPE_PIER_SIDE.PIER_WEST") == "On":
      return "West"
    elif self.read("TELESCOPE_PIER_SIDE.PIER_EAST") == "On":
      return "East"
    else:
      logging.error("Could not determine pier side")
      return "Unknown"

  def get_lst(self):
    """
    Compute the Local Sidereal Time (LST) for the current time at a given location.

    Parameters
    ----------
    longitude_deg : float
        Longitude in degrees (East is positive, West is negative).
    latitude_deg : float, optional
        Latitude in degrees (North is positive, South is negative).
        Default is 0.0 (equator).

    Returns
    -------
    lst_hours : float
        Local sidereal time in hours (0 to 24).
    """
    longitude_deg = float(self.read("GEOGRAPHIC_COORD.LONG"))
    latitude_deg = float(self.read("GEOGRAPHIC_COORD.LAT"))
    altitude_m = float(self.read("GEOGRAPHIC_COORD.ELEV"))
    from astropy.time import Time
    from astropy.coordinates import EarthLocation
    import astropy.units as u
    # 1. Get the current time in UTC as an Astropy Time object
    now = Time.now()  # default timescale = 'utc'

    # 2. Create an EarthLocation object with your longitude/latitude
    #    (height=0 by default; set your altitude if you want).
    location = EarthLocation(lon=longitude_deg * u.deg,
                              lat=latitude_deg * u.deg,
                              height=altitude_m * u.m)

    # 3. Use Astropy's sidereal_time() method to get LST.
    #    'mean' sidereal time is usually adequate; you can also choose 'apparent'.
    lst = now.sidereal_time('mean', longitude=location.lon)

    # Convert LST (an Angle object) to decimal hours
    lst_hours = lst.hour

    # print(f"Local Sidereal Time: {lst_hours:.2f} hours")
    return lst_hours
