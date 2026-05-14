from capture.gpio_controller import GPIOController
import time

gpio = GPIOController()

print("Testing buzzer patterns...")

# Single beep
gpio.beep_1x()
time.sleep(1)

# Double beep
gpio.beep_2x()
time.sleep(2)

# Continuous FOV alarm
print("Continuous alarm...")
gpio.start_fov_alarm("continuous")
time.sleep(5)
gpio.stop_fov_alarm()

time.sleep(1)

# Intermittent alarm
print("Intermittent alarm...")
gpio.start_fov_alarm("intermittent")
time.sleep(5)
gpio.stop_fov_alarm()

gpio.shutdown()

print("Done")