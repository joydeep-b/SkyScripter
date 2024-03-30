import subprocess
import sys
import time
import logging
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

class IndiMount(IndiClient):
  def goto(self, ra, dec):
    self.write("ON_COORD_SET", "TRACK", "On")
    time.sleep(1)
    self.write("EQUATORIAL_EOD_COORD", ["RA", "DEC"], [ra, dec])
    tracking = False
    while not tracking:
      time.sleep(1)
      _, _, tracking = self.get_mount_state()

  def sync(self, ra: float, dec: float):
    self.write("TELESCOPE_TRACK_STATE", "TRACK_ON", "On")
    self.write("ON_COORD_SET", "SYNC", "On")
    self.write("EQUATORIAL_EOD_COORD", ["RA", "DEC"], [ra, dec])
    time.sleep(1)
    ra_read = float(self.read("EQUATORIAL_EOD_COORD.RA"))
    dec_read = float(self.read("EQUATORIAL_EOD_COORD.DEC"))
    if abs(ra - ra_read) > 0.001 or abs(dec - dec_read) > 0.001:
      logging.error(f"Sync failed. Requested: {ra} {dec} Read: {ra_read} {dec_read}")

  def get_mount_state(self) -> Tuple[bool, bool, bool]:
    if self.simulate:
      return False, False, True
    ra_status = self.read("RASTATUS.*", 1)
    de_status = self.read("DESTATUS.*", 1)

    # If ra_status has ("RAGoto", "Ok"), or de_status has ("DEGoto", "Ok"), then the mount is running a goto slew.
    goto_slew = False
    for key, value in ra_status:
      if key == "RAGoto" and value == "Ok":
        goto_slew = True
        break
    for key, value in de_status:
      if key == "DEGoto" and value == "Ok":
        goto_slew = True
        break
    
    # If not goto_slew, and ra_status has ('RARunning', 'Ok'), ('RAGoto', 'Busy'), and ('RAHighspeed', 'Busy'), then the mount is tracking.
    tracking = False
    if (not goto_slew) and \
        ("RARunning", "Ok") in ra_status and \
        ("RAGoto", "Busy") in ra_status and \
        ("RAHighspeed", "Busy") in ra_status:
      tracking = True

    # If not goto_slew, not tracking, and ra_status has ('RARunning', 'Ok') or 
    # de_status has ('DERunning', 'Ok'), then the mount is running a manual slew.
    manual_slew = False
    if (not goto_slew) and (not tracking) and \
        (("RARunning", "Ok") in ra_status or \
        ("DERunning", "Ok") in de_status):
      manual_slew = True

    return manual_slew, goto_slew, tracking

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

  def get_coordinates(self) -> Tuple[float, float, float, float, float]:
    ra = float(self.read("EQUATORIAL_EOD_COORD.RA"))
    dec = float(self.read("EQUATORIAL_EOD_COORD.DEC"))
    alt = float(self.read("HORIZONTAL_COORD.ALT"))
    az = float(self.read("HORIZONTAL_COORD.AZ"))
    lst = float(self.read("TIME_LST.LST"))
    return ra, dec, alt, az, lst

  def get_pier_side(self) -> Literal["West", "East", "Unknown"]:
    if self.read("TELESCOPE_PIER_SIDE.PIER_WEST") == "On":
      return "West"
    elif self.read("TELESCOPE_PIER_SIDE.PIER_EAST") == "On":
      return "East"
    else:
      logging.error("Could not determine pier side")
      return "Unknown"
