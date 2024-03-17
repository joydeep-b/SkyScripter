#!/usr/bin/env python3

import socket
import logging
import json
import time
import sys

VERBOSE = False

class Phd2Client:
  def __init__(self, ip_address='localhost', port=4400):
    self.ip_address = ip_address
    self.port = port
    self.message_id = 0
    
  def connect(self):
    # Open connection to PHD2
    logging.info(f"Connecting to PHD2 at {self.ip_address}:{self.port}")
    try: 
      self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      self.sock.connect((self.ip_address, self.port))
      version_message = self.wait_for_response('Version')
      if version_message:
        logging.info(f"Connected to PHD2 version {version_message['PHDVersion']}")
        self.version = version_message['PHDVersion']
      else:
        logging.error("Unable to connect to PHD2")
        return False
    except Exception as e:
      logging.error(f"Unable to connect to PHD2: {e}")
    self.send_command('set_connected', {'connect': True})
    self.send_command('get_connected', {})
    # connected_message = self.wait_for_response('Connected')
    return True

  def wait_for_response(self, events, timeout=5.0):
    if not isinstance(events, list):
      events = [events]
    # Wait for a response from PHD2
    start_time = time.time()
    while time.time() - start_time < timeout or timeout < 0:
      try:
        chunk = self.sock.recv(4096)
        if chunk:
          response = json.loads(chunk.decode())
          if VERBOSE:
            print(f"Response received: {json.dumps(response, indent=2)}")
          if response['Event'] in events:
            return response
      except Exception as e:
        pass
      time.sleep(0.05)
    return None

  def send_command(self, method, params):
    self.message_id += 1
    json_message = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": self.message_id
    }
    message = json.dumps(json_message)
    if VERBOSE:
      print(f"Request sent: {json.dumps(json_message, indent=2)}")
    self.sock.sendall(message.encode() + b'\r\n')
    logging.info(f"Request sent: {message}")
  
  def start_guiding(self, timeout=360):
    guide_params = {
      "settle": {"pixels": 1.5, "time": 10, "timeout": 60},
      "recalibrate": True
    }
    self.send_command("guide", guide_params)
    success_events = ['SettleDone']
    progress_events = ['StartCalibration', 'Calibrating', 'GuideStep']
    error_events = ['CalibrationFailed']
    wait_events = success_events + progress_events + error_events
    t_start = time.time()
    while time.time() - t_start < timeout:
      response = self.wait_for_response(wait_events, timeout=timeout)
      if not response:
        logging.error("Guiding timeout")
        return False
      if response['Event'] == 'SettleDone':
        settling_status = response['Status']
        if settling_status != 0:
          logging.error(f"Settling failed. Status: {settling_status} Error: {response['Error']}")
          return False
        return True
      elif response['Event'] == 'GuideStep':
        # Don't know why a settling message was not received...
        logging.info(f"Guiding started, no settling message received")
        return True
      elif response['Event'] in error_events:
        logging.error(f"Guiding failed. Event: {response['Event']}")
        return False
      else:
        if response['Event'] == 'Calibrating':
          calibration_status = response['State']
        else:
          calibration_status = ''
        
        logging.info(f"Guiding progress: {response['Event']} {calibration_status}")
    logging.error("Timed out waiting for guiding to start")

  def dither(self, pixels=1.5, timeout=60, settle_pixels=0.5, settle_time=8, settle_timeout=40):
    params = {
      "amount": 10, 
      "raOnly": False, 
      "settle": {
        "pixels": settle_pixels, 
        "time": settle_time,
        "timeout": settle_timeout
      }
    }
    self.send_command("dither", params)
    success_events = ['SettleDone']
    progress_events = ['Settling']
    t_start = time.time()
    while time.time() - t_start < timeout:
      response = self.wait_for_response(success_events + progress_events,
                                        timeout=timeout)
      if not response:
        logging.error("Dither timeout")
        return False
      if response['Event'] == 'SettleDone':
        settling_status = response['Status']
        if settling_status != 0:
          logging.error(f"Settling failed. Status: {settling_status} Error: {response['Error']}")
          return False
        return True
      else:
        settling_distance = response['Distance']
        settling_time = response['Time']
        logging.info(f"Dither settling progress: {settling_distance} pixels, {settling_time} seconds")
    logging.error("Timed out waiting for dither to complete")
    return False
  
  def stop_guiding(self):
    logging.info("Stopping guiding, start looping")
    self.send_command("loop", {})
    success_events = ['LoopingExposures']
    response = self.wait_for_response(success_events)
    if response:
      logging.info("Guiding stopped, looping started")
      return True
    else:
      logging.error("Guiding stop failed")
      return False

def main():
  import os
  sys.path.append(os.getcwd())
  from util import init_logging
  init_logging('lib_phd2', also_to_console=True)
  # logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
  phd2client = Phd2Client()
  if phd2client.connect():
    print("Connected to PHD2 version ", phd2client.version)
  else:
    print("Unable to connect to PHD2")
    sys.exit(1)
  
  if phd2client.start_guiding():
    print("Guiding started")
  else:
    print("Guiding failed to start")
    sys.exit(1)
  
  import signal
  def signal_handler(signum, frame):
    phd2client.stop_guiding()
    sys.exit(0)
  signal.signal(signal.SIGINT, signal_handler)

  for i in range(3):
    time.sleep(60)
    logging.info("Dithering")
    if phd2client.dither():
      logging.info("Dither complete")
    else:
      logging.error("Dither failed")
      sys.exit(1)
  phd2client.stop_guiding()

if __name__ == '__main__':
  main()