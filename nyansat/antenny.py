import sys
import machine
from machine import Pin
import logging
import time
import utime
import uasyncio
import ssd1306
import _thread
import socket

from telemetry_sender_udp import TelemetrySenderUDP
from ant_gps import AntGPS
from imu.bno055_controller import Bno055Controller
from motor.pca9685_controller import Pca9685Controller # from servtor import ServTor
import config as cfg

EL_SERVO_INDEX = cfg.get("elevation_servo_index")
AZ_SERVO_INDEX = cfg.get("azimuth_servo_index")


class AntKontrol:
    """Controller for Nyansat setup: integrated servo controls, IMU usage

    Default components:
    - BNO055 Absolute orientation sensor
    - 16-channel pwm breakout servo controller
    """

    def __init__(self):
        self.telem = TelemetrySenderUDP()
        self._az_direction = -1
        self._el_direction = 1

        # Initialize lock on the IMU
        self.imu_lock = _thread.allocate_lock()
        self._loop = uasyncio.get_event_loop()
        self._gps = AntGPS()
        self._gps_thread = _thread.start_new_thread(self._gps.start, ())

        # Set up I2C connections
        self._i2c_servo_mux = machine.I2C(0, scl=Pin(cfg.get("i2c_servo_scl"),
            Pin.OUT, Pin.PULL_DOWN), sda=Pin(cfg.get("i2c_servo_sda"), Pin.OUT,
                Pin.PULL_DOWN))
        self._i2c_imu = machine.I2C(1,
                scl=machine.Pin(cfg.get("i2c_bno_scl"), Pin.OUT,
                    Pin.PULL_DOWN), sda=machine.Pin(cfg.get("i2c_bno_sda"),
                        Pin.OUT, Pin.PULL_DOWN))

        self._i2c_screen = machine.I2C(-1,
                scl=machine.Pin(cfg.get("i2c_screen_scl"), Pin.OUT,
                    Pin.PULL_DOWN), sda=machine.Pin(cfg.get("i2c_screen_sda"),
                        Pin.OUT, Pin.PULL_DOWN))         # on [60] ssd1306
        self._pinmode = False

        self._servo_mux = Pca9685Controller(self._i2c_servo_mux, min_us=500, max_us=2500, degrees=180)
        self._screen = ssd1306.SSD1306_I2C(128, 32, self._i2c_screen)

        self._cur_azimuth_degree = None
        self._cur_elevation_degree = None
        self._target_azimuth_degree = None
        self._target_elevation_degree = None

        self.imu = Bno055Controller(self._i2c_imu, sign=(0, 0, 0))

        self._euler = None
        self._pinned_euler = None
        self._pinned_servo_pos = None
        time.sleep(6)

        self._servo_mux.position(EL_SERVO_INDEX, 90)
        time.sleep(0.1)
        self._servo_mux.position(AZ_SERVO_INDEX, 90)
        time.sleep(0.1)

        cur_orientation = self.imu.euler()
        self._el_target = self._el_last = cur_orientation[EL_SERVO_INDEX]
        self._az_target = self._az_last = cur_orientation[AZ_SERVO_INDEX]
        self._el_last_raw = 90.0
        self._az_last_raw = 90.0
        self.do_euler_calib()

        self._el_moving = False
        self._az_moving = False

        self._el_max_rate = cfg.get("elevation_max_rate")
        self._az_max_rate = cfg.get("azimuth_max_rate")

        self._orientation_thread = _thread.start_new_thread(self.update_orientation, ())
        logging.info("starting screen thread")
        self._run_telem_thread = True
        self._telem_thread = _thread.start_new_thread(self.send_telem, ())
        self._screen_thread = _thread.start_new_thread(self.display_status, ())
        self._move_thread = _thread.start_new_thread(self.move_loop, ())

    def _measure_az(self, min_angle, max_angle):
        with self.imu_lock:
            self._servo_mux.position(AZ_SERVO_INDEX, min_angle)
            time.sleep(0.3)
            self._euler = self.imu.euler()
            a1 = self._euler[1]
            time.sleep(1)
            self._servo_mux.position(AZ_SERVO_INDEX, max_angle)
            time.sleep(0.3)
            self._euler = self.imu.euler()
            a2 = self._euler[1]
            time.sleep(1)
            return (a1, a2)

    def get_euler(self):
        with self.imu_lock:
            self._euler = self.imu.euler()

    def test_az_axis(self):
        #measure servo pwm parameters
        self.c_az=90
        time.sleep(1)
        self.get_euler()
        self.c_az=80
        time.sleep(2)
        self.get_euler()
        a1 = self._euler[1]
        self.c_az=100
        time.sleep(2)
        self.get_euler()
        a2 = self._euler[1]

        #should be 20 degrees. what did we get
        observed_angle = abs(a1) + a2
        angle_factor = observed_angle/20.0
        self._servo_mux._set_degrees(1, self._servo_mux.degrees(1) * angle_factor)
        print("Observed angle: {} factor: {}".format(observed_angle, angle_factor))

    def test_el_axis(self):
        #measure servo pwm parameters
        self.c_az=90.0
        time.sleep(1)
        self._servo_mux.position(0, 90)
        time.sleep(1)
        self.get_euler()
        self._servo_mux.position(0, 70)
        time.sleep(2)
        self.get_euler()
        a1 = self._euler[0]
        self._servo_mux.position(0, 110)
        time.sleep(2)
        self.get_euler()
        a2 = self._euler[0]

        #should be 20 degrees. what did we get
        observed_angle = a1 - a2
        angle_factor = observed_angle/4.0
        self._servo_mux._set_degrees(0, self._servo_mux.degrees(0) * angle_factor)
        print("Observed angle: {} factor: {}".format(observed_angle, angle_factor))

    #I got az and el backwards. use for now, change all later
    def auto_zero_az(self):
        #automatically find az offset
        self._servo_mux.position(AZ_SERVO_INDEX, 90)
        self._servo_mux.position(EL_SERVO_INDEX, 90)
        time.sleep(1)
        a1 = 60
        a2 = 120
        p_center = 100
        while abs(p_center) > 0.1:
            p1, p2 = self._measure_az(a1, a2)
            p_center = (p1+p2)/2
            print("a1: {},{} a2: {},{} a-center: {}".format(a1, p1, a2, p2, p_center))
            if p_center > 0:
                a2 = a2 - abs(p_center)
            else:
                a1 = a1 + abs(p_center)

        min_y = 100
        min_angle = None
        cur_angle = avg_angle = (a1+a2)/2-1.5
        while cur_angle < avg_angle+1.5:
            self._servo_mux.position(AZ_SERVO_INDEX, cur_angle)
            time.sleep(0.2)
            self._euler = self.imu.euler()
            cur_y = abs(self._euler[1])
            if cur_y < min_y:
                min_y = cur_y
                min_angle = cur_angle
            cur_angle += 0.1

        time.sleep(1)
        a_center = min_angle
        self._servo_mux.position(AZ_SERVO_INDEX, a_center)
        print ("a-center: {}".format(a_center))
        self._euler = self.imu.euler()
        self._az_offset = a_center-90.0

    def auto_calibration(self):
        # read from BNO055 sensor, move antenna
        # soft home, etc
        self._servo_mux.position(AZ_SERVO_INDEX, 90)
        self._servo_mux.position(EL_SERVO_INDEX, 90)
        time.sleep(1)

        self._servo_mux.position(EL_SERVO_INDEX, 180)
        time.sleep(1)
        self._servo_mux.position(EL_SERVO_INDEX, 0)
        time.sleep(1)
        self._servo_mux.position(EL_SERVO_INDEX, 180)
        time.sleep(1)
        self._servo_mux.position(EL_SERVO_INDEX, 0)
        time.sleep(1)

        self._servo_mux.position(AZ_SERVO_INDEX, 180)
        time.sleep(1)
        self._servo_mux.position(AZ_SERVO_INDEX, 0)
        time.sleep(1)
        self._servo_mux.position(AZ_SERVO_INDEX, 180)
        time.sleep(1)
        self._servo_mux.position(AZ_SERVO_INDEX, 0)
        time.sleep(1)

        self._servo_mux.position(AZ_SERVO_INDEX, 90)
        self._servo_mux.position(EL_SERVO_INDEX, 90)
        time.sleep(1)


        self._servo_mux.position(EL_SERVO_INDEX, 0)
        self._euler = self.imu.euler()
        x1 = self._euler[0]
        time.sleep(1)
        self._servo_mux.position(EL_SERVO_INDEX, 180)
        self._euler = self.imu.euler()
        x2 = self._euler[0]
        time.sleep(1)
        self._servo_mux.position(AZ_SERVO_INDEX, 0)
        self._euler = self.imu.euler()
        y1 = self._euler[1]
        time.sleep(1)
        self._servo_mux.position(AZ_SERVO_INDEX, 180)
        self._euler = self.imu.euler()
        y2 = self._euler[1]

        return ("[{}] - [{}] [{}] - [{}]".format(x1,x2,y1,y2))


    def touch(self):
        self._status_gps = self._gps.valid
        self._gps_position = [self._gps.latitude, self._gps.longitude]
        self._elevation_servo_position = self._servo_mux.position(EL_SERVO_INDEX)
        self._azimuth_servo_position = self._servo_mux.position(AZ_SERVO_INDEX)

    def update_telem(self):
        self.telem.update_telem({'euler': self._euler})
        self.telem.update_telem({'last_time': utime.ticks_ms()})
        self.telem.update_telem({'gps_long': self._gps.longitude})
        self.telem.update_telem({'gps_lat': self._gps.latitude})
        self.telem.update_telem({'gps_valid': self._gps.valid})
        self.telem.update_telem({'gps_altitude': self._gps.altitude})
        self.telem.update_telem({'gps_speed': self._gps.speed})
        self.telem.update_telem({'gps_course': self._gps.course})

    def display_status(self):
        while True:
            try:
                self.touch()
                self._screen.fill(0)

                self._screen.text("{:08.3f}".format(self._euler[0]), 0, 0)
                self._screen.text("{:08.3f}".format(self._euler[1]), 0, 8)
                self._screen.text("{:08.3f}".format(self._euler[2]), 0, 16)
                self._screen.show()
            except Exception as e:
                logging.info("here{}".format(str(e)))
            time.sleep(.2)

    def send_telem(self):
        while self._run_telem_thread:
            try:
                self.touch()
                self.updateTelem()
                self.telem.sendTelemTick()
            except Exception as e:
                logging.info("here{}".format(str(e)))
            time.sleep(.2)

    def pin(self):
        self._pinned_euler = self._euler
        self._pinned_servo_pos = [self._el_last, self._az_last]
        self._pinmode = True

    def unpin(self):
        self._pinned_euler = None
        self._pinned_servo_pos = None
        self._pinmode = False

    def do_euler_calib(self):
        cur_imu = self.imu.euler()
        self._el_target = cur_imu[EL_SERVO_INDEX]
        self._az_target = cur_imu[AZ_SERVO_INDEX]

        self._el_offset = cur_imu[EL_SERVO_INDEX] - self._el_last_raw
        self._az_offset = cur_imu[AZ_SERVO_INDEX] - self._az_last_raw


    def do_move_mode(self):
        el_delta_deg = self._el_target - ((self._el_last_raw + self._el_offset) % 360)
        az_delta_deg = self._az_target - (self._az_last_raw - self._az_offset)

        print("delta {} = {} - {} - {}".format(az_delta_deg, self._az_target, \
                                               self._az_last_raw, self._az_offset))

        if self._el_moving or self._pinmode:
            # goes from 0 - 180, or whaterver max is
            if abs(el_delta_deg) < self._el_max_rate:
                self._el_last_raw = self._el_last_raw + el_delta_deg
                self._servo_mux.position(EL_SERVO_INDEX, self._el_last_raw)
                self._servo_mux.release(EL_SERVO_INDEX)
                self._el_moving = False
            else:
                if el_delta_deg > 0:
                    self._el_last_raw = self._el_last_raw + self._el_max_rate * self._el_direction
                else:
                    self._el_last_raw = self._el_last_raw - self._el_max_rate * self._el_direction
                self._servo_mux.position(EL_SERVO_INDEX, self._el_last_raw)
                self._el_moving = True

        if self._az_moving or self._pinmode:
            # -90 to +90, but antenny can only move from 0 - 90
            print(az_delta_deg)
            if abs(az_delta_deg) < self._az_max_rate:
                self._az_last_raw = self._az_last_raw + az_delta_deg
                self._servo_mux.position(AZ_SERVO_INDEX, self._az_last_raw)
                self._servo_mux.release(AZ_SERVO_INDEX)
                self._az_moving = False
            else:
                if az_delta_deg > 0:
                    self._az_last_raw = self._az_last_raw + self._az_max_rate * self._az_direction
                else:
                    self._az_last_raw = self._az_last_raw - self._az_max_rate * self._az_direction
                self._servo_mux.position(AZ_SERVO_INDEX, self._az_last_raw)
                self._az_moving = True

    def do_pin_mode(self):
        delta_x = self._pinned_euler[0] - self._euler[0]
        delta_y = self._pinned_euler[1] - self._euler[1]
        logging.info("d-x {}, d-y {}".format(delta_x, delta_y))
        self._el_target = self._el_last + delta_x * -1
        self._az_target = self._az_last + delta_y
        self.do_move_mode()

    def update_orientation(self):
        while True:
            try:
                with self.imu_lock:
                    self._euler = self.imu.euler()
            except:
                logging.info("Error in orientation update")

    def move_loop(self):
        while True:
            while self._az_moving or self._el_moving or self._pinmode:
                try:

                    if self._pinned_euler:
                        self.do_pin_mode()
                    else:
                        self.do_move_mode()
                    time.sleep(0.1)
                except Exception as e:
                    logging.info(e)
            time.sleep(0.1)

    def set_el_deg(self, deg):
        self._el_moving = True
        self._el_target = deg

    def set_az_deg(self, deg):
        self._az_moving = True
        self._az_target = deg


    @property
    def az(self):
        return self._az_last_raw

    @az.setter
    def az(self, deg):
        self.set_az_deg(deg)

    @property
    def c_az(self):
        return self._az_last + self._az_offset

    @az.setter
    def c_az(self, deg):
        self.set_az_deg(deg+ self._az_offset)

    @property
    def c_el(self):
        return self._el_last + self._el_offset

    @az.setter
    def c_el(self, deg):
        self.set_el_deg(deg + self._el_offset)

    @property
    def el(self):
        return self._el_last

    @el.setter
    def el(self, deg):
        self.set_el_deg(deg)

    def imu_status(self):
        output = ""
        output += 'Temperature {}°C'.format(self.imu.temperature()) + "\n"
        output += 'Mag       x {:5.0f}    y {:5.0f}     z {:5.0f}'.format(*self.imu.mag()) + "\n"
        output += 'Gyro      x {:5.0f}    y {:5.0f}     z {:5.0f}'.format(*self.imu.gyro()) + "\n"
        output += 'Accel     x {:5.1f}    y {:5.1f}     z {:5.1f}'.format(*self.imu.accel()) + "\n"
        output += 'Lin acc.  x {:5.1f}    y {:5.1f}     z {:5.1f}'.format(*self.imu.lin_acc()) + "\n"
        output += 'Gravity   x {:5.1f}    y {:5.1f}     z {:5.1f}'.format(*self.imu.gravity()) + "\n"
        output += 'Heading     {:4.0f} roll {:4.0f} pitch {:4.0f}'.format(*self.imu.euler()) + "\n"
        return output

    def motor_status(self):
        # TODO
        pass

    def motor_test(self, index, position):
        pos = self._servo_mux.smooth_move(index, position, 10)
        x_angle, y_angle, z_angle = self.imu.euler()
        return (pos, x_angle, y_angle, z_angle)