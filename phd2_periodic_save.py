import socket
import json
import time
import sys
import os
import argparse

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# Connect the socket to the server
message_id = 1
ip_address = "localhost"
port = 4400
save_directory = "./phd2_images"
save_interval = 300

def connect_to_phd2():
  global sock, server_address
  # Connect the socket to the server
  server_address = (ip_address, port)
  print(f"Connecting to PHD2 at {server_address}:{port}")
  try:
    sock.connect(server_address)
  except Exception as e:
    print(f"Error connecting to PHD2: {e}")
    exit(1)

def send_save_command():
  global message_id, sock
  # Create a TCP/IP socket
  try:
    # Send JSON RPC 2.0 command
    message = json.dumps({
        "jsonrpc": "2.0",
        "method": "save_image",
        "params": {},
        "id": message_id
    })
    sock.sendall(message.encode() + b'\r\n')
    print(f"Request sent: {message_id}")
    message_id += 1
  except Exception as e:
    print(f"Unable to send command: {e}")
    sys.exit(1)

def save_image_loop():
  # Send the command every 5 minutes
  t_last_command = 0
  while True:
    t_now = time.time()
    # print(f"Time since last command: {t_now - t_last_command}")
    t_since_last_command = t_now - t_last_command
    if t_since_last_command >= save_interval:
      send_save_command()
      t_last_command = t_now
    try:
      chunk = sock.recv(4096)
    except Exception as e:
      print(f"Error receiving data: {e}")
      sys.exit(1)
    if chunk:
      try:
        data = json.loads(chunk)
        filename = data['result']['filename']
        # Output filename is ./phd2_images/{epoch time}.fits where epoch 
        # time is the time up to three decimal places (milliseconds).
        epoch_time = str(time.time()).replace(".", "")[:13]
        output_filename = save_directory + f"/{epoch_time}.fits"
        os.rename(filename, output_filename)
        print(f"Saved {output_filename}")
      except Exception as e:
        pass
    time.sleep(0.1)


if __name__ == "__main__":
  # Parse command-line arguments: -d = save directory, -p = port, -i = IP address, -t = time between saves
  parser = argparse.ArgumentParser()
  parser.add_argument("-d", "--directory", help="Directory to save images to", default="./phd2_images")
  parser.add_argument("-p", "--port", help="Port to connect to", default=4400)
  parser.add_argument("-i", "--ip", help="IP address to connect to", default="localhost")
  parser.add_argument("-t", "--time", help="Time between saves", default=300)
  args = parser.parse_args()
  # Set global variables
  port = args.port
  ip_address = args.ip
  save_directory = args.directory
  save_interval = int(args.time)
  print(f"Saving images to {save_directory} every {save_interval} seconds")

  # Create save directory if it doesn't exist
  if not os.path.exists(args.directory):
    os.makedirs(args.directory)
  # Connect to PHD2
  connect_to_phd2()
  save_image_loop()