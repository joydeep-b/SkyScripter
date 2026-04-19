import time

from sky_scripter.lib_indi import IndiCamera
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.util import print_and_log


class CoolerManager:
  def __init__(self, camera: IndiCamera, logger: StructuredLogger):
    self.camera = camera
    self.logger = logger
    self.target_temp = None

  def start_cooling(self, target_temp: float, timeout: float = 300,
                    tolerance: float = 1.0) -> bool:
    self.target_temp = target_temp
    self.camera.set_temperature(target_temp)
    self.logger.log("cooler", "cooler_start", target_temp=target_temp)
    print_and_log(f"Cooling to {target_temp}°C (timeout {timeout}s)")
    t_start = time.time()
    last_print = 0
    while time.time() - t_start < timeout:
      temp = self.camera.get_temperature()
      elapsed = time.time() - t_start
      if elapsed - last_print >= 30:
        print_and_log(f"Sensor: {temp:.1f}°C, target: {target_temp}°C, elapsed: {elapsed:.0f}s")
        last_print = elapsed
      if abs(temp - target_temp) <= tolerance:
        self.logger.log("cooler", "cooler_reached", temp=temp, elapsed=elapsed)
        print_and_log(f"Target temperature reached: {temp:.1f}°C")
        return True
      time.sleep(5)
    temp = self.camera.get_temperature()
    self.logger.log("cooler", "cooler_timeout", temp=temp, target=target_temp)
    print_and_log(f"Cooling timeout: sensor at {temp:.1f}°C, target was {target_temp}°C")
    return False

  def warm_up(self, rate: float = 2.0, interval: float = 30):
    current_temp = self.camera.get_temperature()
    final_temp = 5.0
    self.logger.log("cooler", "warmup_start", start_temp=current_temp, final_temp=final_temp)
    print_and_log(f"Warming up from {current_temp:.1f}°C to {final_temp}°C")
    temp = current_temp
    while temp < final_temp:
      temp = min(temp + rate, final_temp)
      self.camera.set_temperature(temp)
      self.logger.log("cooler", "warmup_step", temp=temp)
      print_and_log(f"Warm-up step: set to {temp:.1f}°C")
      time.sleep(interval)
    self.camera.cooler_off()
    self.target_temp = None
    self.logger.log("cooler", "warmup_complete")
    print_and_log("Warm-up complete, cooler off")

  def get_status(self) -> dict:
    status = {"sensor_temp": self.camera.get_temperature()}
    if self.target_temp is not None:
      status["target_temp"] = self.target_temp
    return status
