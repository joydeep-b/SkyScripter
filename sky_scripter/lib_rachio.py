#!/usr/bin/env python3

import os
import sys
import json
import time
import datetime
from dateutil.parser import parse
from pytz import timezone
import logging
import argparse

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.util import init_logging, print_and_log, exec_or_fail

def get_rachio_key():
  api_key_file = os.path.join(os.getcwd(), '.rachio_api_key')
  with open(api_key_file, 'r') as f:
    api_key = f.read().strip()
  return api_key


class RachioClient:
  def get_person_id(self):
    command = f'curl -X GET -H "Authorization: Bearer {self.api_key}" "https://api.rach.io/1/public/person/info"'
    result = exec_or_fail(command)
    return json.loads(result)['id']

  def get_person_info(self):
    person_id = self.get_person_id()
    command = f'curl -X GET -H "Content-Type: application/json" -H "Authorization: Bearer {self.api_key}" https://api.rach.io/1/public/person/{person_id}/'
    result = exec_or_fail(command)
    return json.loads(result)

  def get_first_device(self):
    person_info = self.get_person_info()
    return person_info['devices'][0]

  def get_upcoming_schedule(self, hours_ahead=24):
    json_data = {
      "device_id": self.device['id'],
      "hours_ahead": hours_ahead,
    }
    command = 'curl -X POST -H "Content-Type: application/json" -H ' + \
      f'"Authorization: Bearer {self.api_key}" ' + \
      f'https://cloud-rest.rach.io/events/upcoming -d "{json.dumps(json_data)}"'
    result = exec_or_fail(command)
    return json.loads(result)

  def __init__(self, api_key):
    self.api_key = api_key
    self.person_id = self.get_person_id()
    self.device = self.get_first_device()
    logging.info(f"Rachio API Key: {api_key} Person ID: {self.person_id}, Device ID: {self.device['id']}")

def main():
  print('Checking for upcoming Rachio events...')
  init_logging('rachio')
  parser = argparse.ArgumentParser(description='Rachio client')
  parser.add_argument('-a', '--hours-ahead', type=int, default=24,
                      help='Hours ahead to fetch the schedule')
  parser.add_argument('-j', '--json', action='store_true',
                      help='Print the JSON response')
  args = parser.parse_args()

  api_key = get_rachio_key()
  rachio_client = RachioClient(api_key)

  upcoming_schedule = rachio_client.get_upcoming_schedule(hours_ahead=args.hours_ahead)
  for event in upcoming_schedule['entries']:
    event_name = event['summary']
    event_start = parse(event['startTime'], tzinfos={'Z': 'UTC'})
    event_end = parse(event['endTime'], tzinfos={'Z': 'UTC'})
    time_until_event = event_start - datetime.datetime.now(datetime.timezone.utc)
    print(f'Event "{event_name}":')
    print(f'  Start: {event_start.astimezone(timezone("US/Central"))}')
    print(f'  End: {event_end.astimezone(timezone("US/Central"))}')
    print(f'  Time until start: {time_until_event}')
    print(f'  Duration: {event_end - event_start}')
    if args.json:
      print(json.dumps(event, indent=2))
  # If there are upcoming events, print a warning in blinking red text.
  BLINK = "\033[5m"
  BOLD = "\033[1m"
  RED = "\033[31m"
  GREEN = "\033[32m"
  RESET = "\033[0m"
  if len(upcoming_schedule['entries']) > 0:
    print(f'{BLINK}{BOLD}{RED}WARNING: Sprinkler events found!{RESET}')
  else:
    print(f'{GREEN}No upcoming sprinkler events found.{RESET}')


if __name__ == '__main__':
  main()
