#!/usr/bin/env python3
'''
    File name: play.py
    Author: Maxime Bergeron
    Date last modified: 15/11/2018
    Python Version: 3.7

    A python websocket server/client to control various cheap IoT RGB BLE lightbulbs and
    HDMI-CEC-to-TV RPi3
'''
import os
import os.path
import sys
import argparse
import sched
import time
import datetime
import socket
import threading
import functools
import configparser
import traceback
import json
import signal
import queue
from argparse import RawTextHelpFormatter, Namespace
from multiprocessing.pool import ThreadPool
import bluepy.btle as ble
from __main__ import *

JOURNALING = False #do not edit - use the --journal option

###

def connect_ble(_f):
    """ Wrapper for functions which requires an active BLE connection using bluepy """
    @functools.wraps(_f)
    def _conn_wrap(self, *args):
        if self._connection is None:
            try:
                LightManager.debugger("CONnecting to device ({}) {}".format(self.device_type,
                                                                            self.device), 0)
                connection = ble.Peripheral(self.device)
                self._connection = connection.withDelegate(self)
            except Exception as ex:
                LightManager.debugger("Device ({}) {} connection failed. Exception: {}" \
                                      .format(self.device_type, self.device, ex), 1)
                self._connection = None
        return _f(self, *args)
    return _conn_wrap

###
# CONSTANTS
LIGHT_SKIP = "-1"
LIGHT_OFF = "0"
LIGHT_ON = "1"
###

class LightServer(object):
    """ Handles server-side request reception and handling """
    def __init__(self, lm, host, port):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sched_disconnect = sched.scheduler(time.time, time.sleep)
        self.scheduled_disconnect = None
        signal.signal(signal.SIGTERM, self.remove_server)
        lm.set_colors([LIGHT_ON] * len(lm.devices))
        lm.run()

    def listen(self):
        """ Starts the server """
        LightManager.debugger('Server started', 0)
        # Cleanup connection to allow new sock.accepts faster as sched is blocking
        self.disconnect_devices()
        self.sock.listen(5)
        while True:
            client, address = self.sock.accept()
            LightManager.debugger("Connected with {}:{}".format(address[0], address[1]), 0)
            client.settimeout(30)
            threading.Thread(target=self.listen_client, args=(client, address)).start()

    def listen_client(self, client, address):
        """ Listens for new requests and handle them properly """
        streamingdev = False
        streaminggrp = False
        streaming_id = None
        try:
            while True:
                msize = int(client.recv(4).decode('utf-8'))
                if self.scheduled_disconnect is not None:
                    self.sched_disconnect.cancel(self.scheduled_disconnect)
                    self.scheduled_disconnect = None
                #LightManager.debugger("Set message size {}".format(msize), 0)
                data = client.recv(msize)
                if data:
                    if data.decode('utf-8') == "getstate":
                        LightManager.debugger('Sending lightserver status', 0)
                        client.send(str.encode(str(lm.get_state())))
                        break
                    if data.decode('utf-8') == "stream":
                        LightManager.debugger('Starting streaming mode', 0)
                        streamingdev = True
                        continue
                    if data.decode('utf-8') == "streamgroup":
                        LightManager.debugger('Starting group streaming mode', 0)
                        streaminggrp = True
                        continue
                    if data.decode('utf-8') == "nostream":
                        LightManager.debugger('Ending streaming mode', 0)
                        streamingdev = False
                        streaminggrp = False
                        streaming_id = None
                        break
                    if streamingdev:
                        if streaming_id is None:
                            streaming_id = int(data.decode('utf-8'))
                            LightManager.debugger('Set streaming devid to {}' \
                                                  .format(streaming_id), 0)
                            continue
                        LightManager.debugger("Sending request to devid {} for color: {}" \
                                              .format(streaming_id, data.decode('utf-8')), 0)
                        lm.set_light_stream(streaming_id, data.decode('utf-8'), False)
                        continue
                    if streaminggrp:
                        if streaming_id is None:
                            streaming_id = data.decode('utf-8')
                            LightManager.debugger('Set streaming group to {}' \
                                                  .format(streaming_id), 0)
                            continue
                        LightManager.debugger("Sending request to group '{}' for color: {}" \
                                              .format(streaming_id, data.decode('utf-8')), 0)
                        lm.set_light_stream(streaming_id, data.decode('utf-8'), True)
                        continue
                    try:
                        args = self._sanitize(json.loads(data.decode('utf-8')))
                    except: #fallback - data is not formatted
                        LightManager.debugger("Error - improperly formatted JSON", 2)
                        break
                    LightManager.debugger('Change of lights requested with args: ' + str(args), 0)
                    self._validate_and_execute_req(args)
                    break

        except socket.timeout:
            pass

        except Exception as ex:
            LightManager.debugger('Unhandled exception of type {}: {}, {}' \
                                  .format(type(ex), ex, 
                                          ''.join(traceback.format_tb(ex.__traceback__))
                                         ), 2)

        finally:
            LightManager.debugger('Closing connection.', 0)
            lm.set_lock(0)
            lm.reinit()
            client.close()
            self.scheduled_disconnect = self.sched_disconnect.enter(60, 1, 
                                                                    self.disconnect_devices, ())
            self.sched_disconnect.run()

    def disconnect_devices(self):
        """ Disconnects all configured devices """
        self.scheduled_disconnect = None
        LightManager.debugger("Server unused. Disconnecting devices.", 0)
        for _dev in lm.devices:
            _dev.disconnect()

    def remove_server(self, signal, frame):
        """ Shuts down server and cleans resources """
        LightManager.debugger("Closing down server and lights.", 0)
        lm.skip_time(0)
        lm.set_colors([LIGHT_OFF] * len(lm.devices))
        lm.run()
        time.sleep(3)
        self.sock.close()

    def _validate_and_execute_req(self, args):
        LightManager.debugger("Validating arguments", 0)
        if args["hexvalues"] and (args["playbulb"] or args["milight"]):
            LightManager.debugger("Got color hexvalues for milights and/or playbulbs \
                                   and/or both devices in the same request, which is not \
                                   supported. Use '{} -h' for help. Quitting".format(sys.argv[0]),
                                 2)
            return
        if args["tvon"] and args["tvoff"]:
            LightManager.debugger("Cannot ON and OFF the TV in the same request. Quitting.", 2)
            return         
        if len(args["hexvalues"]) != len(lm.devices) and not any([args["notime"], args["off"], args["on"], 
                                                                  args["playbulb"], args["milight"], 
                                                                  args["toggle"], args["tvon"], 
                                                                  args["tvoff"], args["tvrestart"]]):
            LightManager.debugger("Got {} color hexvalues, {} expected. Use '{} -h' for help. Quitting" \
                                  .format(len(args["hexvalues"]), len(lm.devices), sys.argv[0]), 2)
            return
        if args["tvon"]:
            LightManager.debugger("Setting TV on", 0)
            self._set_tv(1)
            return #Do not accept any more requests for now.
        if args["tvoff"]:
            LightManager.debugger("Setting TV off", 0)
            self._set_tv(0)
        if args["tvrestart"]:
            LightManager.debugger("Rebooting KODI", 0)
            self._set_tv(2)
            return
        if args["priority"]:
            lm.priority = args["priority"]
        if args["hexvalues"]:
            LightManager.debugger("Received color hexvalues length {} for {} devices" \
                                  .format(len(args["hexvalues"]), len(lm.devices)), 0)
            lm.set_colors(args["hexvalues"])
        else:
            if args["playbulb"] is not None:
                LightManager.debugger("Received playbulb change request", 0)
                lm.set_typed_colors(args["playbulb"], "Playbulb")
            if args["milight"] is not None:
                LightManager.debugger("Received milight change request", 0)
                lm.set_typed_colors(args["milight"], "Milight")
            if args["off"]:
                LightManager.debugger("Received OFF change request", 0)
                lm.set_colors([LIGHT_OFF] * len(lm.devices))
            if args["on"]:
                LightManager.debugger("Received ON change request", 0)
                lm.set_colors([LIGHT_ON] * len(lm.devices))
            if args["toggle"]:
                LightManager.debugger("Received TOGGLE change request", 0)
                lm.set_colors(lm.get_toggle())
        if args["notime"] or args["off"]:
            lm.skip_time(0)
        if args["group"] is not None:
            lm.get_group(args["group"], args["subgroup"])
        LightManager.debugger("Arguments are OK", 0)
        lm.run()
        return

    def _sanitize(self, args):
        if "hexvalues" not in args:
            args["hexvalues"] = []
        if "off" not in args:
            args["off"] = False
        if "tvrestart" not in args:
            args["tvrestart"] = False
        if "on" not in args:
            args["on"] = False
        if "tvon" not in args:
            args["tvon"] = False
        if "toggle" not in args:
            args["toggle"] = False
        if "playbulb" not in args:
            args["playbulb"] = None
        if "milight" not in args:
            args["milight"] = None
        if "server" not in args:
            args["server"] = False
        if "journal" not in args:
            args["journal"] = False
        if "notime" not in args:
            args["notime"] = False
        if "tvoff" not in args:
            args["tvoff"] = False
        if "priority" in args and args["priority"] is None:
            args["priority"] = 1
        if "priority" not in args:
            args["priority"] = 1
        if "group" not in args:
            args["group"] = None
        if "subgroup" not in args:
            args["subgroup"] = None
        if type(args["playbulb"]).__name__ == "str":
            LightManager.debugger('Converting values to lists for playbulb', 0)
            args["playbulb"] = args["playbulb"].replace("'", "").split(',')
        if type(args["milight"]).__name__ == "str":
            LightManager.debugger('Converting values to lists for milight', 0)
            args["milight"] = args["milight"].replace("'", "").split(',')
        return args

    def _set_tv(self, value):
        if value == 0:
            ## TV OFF
            os.system("echo 'standby 0' | cec-client -s")
            os.system("ssh kodi@192.168.1.200 'sudo shutdown now'")
            LightManager.debugger('Set the TV and KODI to OFF', 0)
        elif value == 1:
            ## TV ON
            os.system("echo 'on 0' | cec-client -s")
            LightManager.debugger('Set the TV ON', 0)
        elif value == 2:
            ## TV RESTART        
            os.system("ssh kodi@192.168.1.200 'sudo reboot'")
            LightManager.debugger('Restarted KODI', 0)

class LightManager(object):
    """ Methods for instanciating and managing BLE lightbulbs """
    def __init__(self, config=None):
        self.config = config
        ## TWEAKABLES ##
        self.devices = []
        i = 0
        while True:
            try:
                if self.config["DEVICE"+str(i)]["TYPE"] == "Playbulb":
                    self.devices.append(Playbulb(i, self.config["DEVICE"+str(i)]["ADDRESS"],
                                                 self.config["DEVICE"+str(i)]["DESCRIPTION"],
                                                 self.config["DEVICE"+str(i)]["GROUP"],
                                                 self.config["DEVICE"+str(i)]["SUBGROUP"],
                                                 self.config["DEVICE"+str(i)]["DEFAULT_INTENSITY"],
                                                 self))
                    LightManager.debugger("Created device Playbulb {}. Description: {}" \
                                          .format(self.config["DEVICE"+str(i)]["ADDRESS"],
                                                  self.config["DEVICE"+str(i)]["DESCRIPTION"]),
                                          0)
                elif self.config["DEVICE"+str(i)]["TYPE"] == "Milight":
                    self.devices.append(Milight(i, self.config["DEVICE"+str(i)]["ADDRESS"],
                                                self.config["DEVICE"+str(i)]["ID1"],
                                                self.config["DEVICE"+str(i)]["ID2"],
                                                self.config["DEVICE"+str(i)]["DESCRIPTION"],
                                                self.config["DEVICE"+str(i)]["GROUP"],
                                                self.config["DEVICE"+str(i)]["SUBGROUP"],
                                                self))
                    LightManager.debugger("Created device Milight {}. Description: {}" \
                                          .format(self.config["DEVICE"+str(i)]["ADDRESS"],
                                                  self.config["DEVICE"+str(i)]["DESCRIPTION"]),
                                          0)
                else:
                    LightManager.debugger('Unsupported device type {}' \
                                          .format(self.config["DEVICE"+str(i)]["TYPE"]), 1)
            except KeyError:
                break
            i = i + 1

        #TODO allow reporting of device state to the lightserver
        self.starttime = datetime.time(18, 00) #Light change minimal time
        self.skiptime = 0
        self.queue = queue.Queue()
        self.colors = [LIGHT_OFF] * len(self.devices)
        self.set_lock(0)
        self.lockcount = 0
        self.journaling = False
        self.priority = 0
        self.threaded = False
        self.light_threads = [None] * len(self.devices)
        self.light_pool = None

    def start_threaded(self):
        """ Enables multithreaded light change requests """
        self.threaded = True
        self.light_pool = ThreadPool(processes=4)

    def skip_time(self, serverwide=0):
        """ Enables skipping time check """
        if serverwide:
            LightManager.debugger("Skipping time check for all requests", 0)
            self.starttime = None
        else:
            LightManager.debugger("Skipping time check", 0)
            self.skiptime = 1

    def set_colors(self, color):
        """ Setter function for color request. Required. """
        self.colors = color

    def get_group(self, group, subgroup):
        """ Gets devices from a specific group/subgroup for the light change """
        for _cnt, device in enumerate(self.devices):
            if device.group != group:
                LightManager.debugger("Skipping device {} as it does not belong in the '{}' group" \
                                      .format(device.device, group), 0)
                self.colors[_cnt] = LIGHT_SKIP
            else:
                if subgroup is not None and device.subgroup != subgroup:
                    LightManager.debugger("Skipping device {} as it does not belong in the '{}' subgroup" \
                                          .format(device.device, subgroup), 0)
                    self.colors[_cnt] = LIGHT_SKIP

    def get_toggle(self):
        """ Toggles the devices on/off """
        colors = [LIGHT_ON] * len(lm.devices)
        i = 0
        for color in self.get_state():
            if color != LIGHT_OFF:
                colors = [LIGHT_OFF] * len(lm.devices)
            i = i+1
        return colors

    def set_typed_colors(self, colorargs, atype):
        """ Gets devices of a specific  type for the light change """
        cvals = self._get_type_index(atype)
        if cvals[0] != len(colorargs):
            LightManager.debugger("Received color hexvalues length {} for {} devices. Quitting" \
                                  .format(len(colorargs), cvals[0]), 2)
            return
        self.colors[cvals[1]:cvals[1]+cvals[0]] = colorargs

    def run(self):
        """ Validates the request and runs the light change """
        if self._check_time():
            self.queue.put(self.colors)
            #TODO Manage locking out when the run thread hangs
            LightManager.debugger("Locked status: {}".format(self.locked), 0)
            if not self.locked or self.lockcount == 2:
                self._set_lights()
            else:
                self.lockcount = self.lockcount + 1

    def descriptions(self):
        """ Getter for configured devices descriptions """
        desctext = ""
        i = 1
        for obj in self.devices:
            if isinstance(obj, (Playbulb, Milight)):
                desctext += str(i) + " - " + obj.descriptions() + "\n"
            else:
                desctext += str(i) + " - " + "Unknown bulb type\n"
            i += 1
        return desctext

    def enable_journaling(self):
        """ Enables journaling to file """
        global JOURNALING
        JOURNALING = True
        #TODO Dynamic history limits
        if os.path.isfile(self.config['SERVER']['JOURNAL_DIR'] + "/play.0.log"):
            if os.path.isfile(self.config['SERVER']['JOURNAL_DIR'] + "/play.1.log"):
                if os.path.isfile(self.config['SERVER']['JOURNAL_DIR'] + "/play.2.log"):
                    os.remove(self.config['SERVER']['JOURNAL_DIR'] + "/play.2.log")
                os.rename(self.config['SERVER']['JOURNAL_DIR'] + "/play.1.log",
                          self.config['SERVER']['JOURNAL_DIR'] + "/play.2.log")
            os.rename(self.config['SERVER']['JOURNAL_DIR'] + "/play.0.log",
                      self.config['SERVER']['JOURNAL_DIR'] + "/play.1.log")

    def set_lock(self, is_locked):
        """ Locks the light change request """
        self.locked = is_locked

    def get_state(self, devid=None):
        """ Getter for configured devices actual colors """
        states = [None] * len(self.devices)
        for _cnt, dev in enumerate(self.devices):
            states[_cnt] = dev.get_state()
        if devid is not None:
            return states[devid]
        return states

    def set_light_stream(self, devid, color, is_group):
        """ Simplified function for quick, streamed light change requests """
        if is_group:
            for device in self.devices:
                if device.group == devid:
                    cnt = 0
                    _color = device.convert(color)
                    while True:
                        if cnt == 4:
                            break
                        if device.color(_color, 3):
                            break
                        time.sleep(0.3)
                        cnt = cnt + 1
        else:
            cnt = 0
            _color = device.convert(color)
            while True:
                if cnt == 4:
                    break
                if self.devices[devid].color(_color, 3):
                    break
                time.sleep(0.3)
                cnt = cnt + 1
        self.reinit()

    def reinit(self):
        """ Resets the Success bool to False """
        i = 0
        while i < len(self.devices):
            self.devices[i].reinit()
            i += 1

    def _set_lights(self):
        LightManager.debugger("Running a change of lights (priority level: {})..." \
                              .format(self.priority), 0)
        try:
            self.lockcount = 0
            firstran = False
            try:
                while not self.queue.empty():
                    colors = None
                    if firstran:
                        LightManager.debugger("Getting remainder of queue", 0)
                        self.reinit()
                    colors = self.queue.get() #TODO Check performance
                    LightManager.debugger("Changing colors to {} from state {}" \
                                          .format(colors, self.get_state()), 0)
                    self.set_lock(1)
                    i = 0
                    tries = 0
                    firstran = True

                    while i < len(self.devices):
                        _state = self.get_state(i)
                        _color = self.devices[i].convert(colors[i])

                        if not self.devices[i].success:
                            LightManager.debugger("DEVICE: {}, REQUESTED COLOR: {}, \
                                                  FROM STATE: {}, PRIORITY: {}"
                                                  .format(self.devices[i].device,
                                                          _color, _state,
                                                          self.devices[i].priority),
                                                  0)
                            if self.threaded:
                                if not self.queue.empty():
                                    break
                                self.light_threads[i] = self.light_pool.apply_async(self.devices[i].color, 
                                                                                    args=(_color, self.priority, ))
                            else:
                                self.devices[i].color(_color, self.priority)
                        i += 1

                        if i == len(self.devices):
                            if self.threaded:
                                LightManager.debugger("Awaiting results", 0)
                                for _cnt, _thread in enumerate(self.light_threads):
                                    if _thread is not None:
                                        try:
                                            if not _thread.get(5):
                                                i = 0
                                        except:
                                            i = 0
                                tries = tries + 1
                                if tries == 5:
                                    break
                            else:
                                for _cnt, _dev in enumerate(self.devices):
                                    _state = self.get_state(_cnt)
                                    if colors[_cnt] != _state \
                                       or (colors[_cnt] == self.devices[_cnt].convert(LIGHT_OFF) \
                                       and _state == self.devices[_cnt].convert(LIGHT_OFF)):
                                        i = 0
                                tries = tries + 1
                                if tries == 5:
                                    break

            except queue.Empty:
                LightManager.debugger("Nothing in queue", 0)
                pass

            finally:
                LightManager.debugger("Clearing up light change queues.", 0)
                if colors:
                    self.queue.task_done()

        except Exception as ex:
            LightManager.debugger('Unhandled exception of type {}: {}, {}'
                                  .format(type(ex), ex, 
                                          ''.join(traceback.format_tb(ex.__traceback__))), 2)

        finally:
            self.reinit()
            self.set_lock(0)

        LightManager.debugger("Change of lights completed.", 0)

    def _check_time(self):
        #TODO Check if we keep this...
        if self.skiptime or self.starttime is None:
            self.skiptime = 0
            return 1
        if datetime.time(6, 00) < datetime.datetime.now().time() < self.starttime:
            LightManager.debugger("Too soon, no change of light required", 0)
            return 0
        return 1

    def _get_type_index(self, atype):
        # TODO This should not depend on an ordered set of devices
        i = 0
        count = 0
        firstindex = 0
        for obj in self.devices:
            if isinstance(obj, eval(atype)):
                if count == 0: 
                    firstindex = i
                count += 1
            i += 1
        if count == 0:
            raise Exception('Invalid bulb type given. Quitting')
        return [count, firstindex]

    @staticmethod
    def debugger(msg, level):
        """ Handles debug logging """
        playconfig = configparser.ConfigParser()
        playconfig.read('play.ini')
        levels = {0: "DEBUG", 1: "ERROR", 2: "FATAL"}
        debugtext = "({}) - [{}] {}".format(datetime.datetime.now().time(), levels[level], msg)
        print(debugtext)
        if JOURNALING:
            with open(playconfig['SERVER']['JOURNAL_DIR'] + "/play.0.log", "a") as jfile:
                jfile.write(debugtext + "\n")


class Bulb(object):
    """ Global bulb functions and variables """
    def __init__(self, devid, device, description, group, subgroup, server):
        self.devid = devid
        self.device = device
        self.description = description
        self.success = False
        self._connection = None
        self.group = group
        self.subgroup = subgroup
        self.server = server
        self.priority = 0
        self.state = None
        self.device_type = None

    def reinit(self):
        """ Prepares the device for a future request """
        self.success = False

    def get_state(self):
        """ Getter for the actual color """
        return self.state

    def disconnect(self):
        """ Disconnects the device """
        try:
            if self._connection is not None:
                LightManager.debugger("DISconnecting from device {}".format(self.device), 0)
                self._connection.disconnect()
        except ble.BTLEException:
            LightManager.debugger("Device ({}) {} disconnection failed. Already disconnected?"
                                  .format(self.device_type, self.device), 1)
            pass
        except:
            pass

        self._connection = None


class Playbulb(Bulb):
    """ Methods for driving a rainbow BLE lightbulb """
    def __init__(self, devid, device, description, group, subgroup, intensity, server):
        super().__init__(devid, device, description, group, subgroup, server)
        self.device_type = "Playbulb"
        #TODO get actual color at instanciation
        self.state = "00000000"
        self.intensity = intensity

    def convert(self, color):
        """ Conversion to a color code acceptable by the device """
        if color == LIGHT_OFF:
            color = "00000000"
        elif color == LIGHT_ON:
            color = self.intensity
        return color

    def color(self, color, priority):
        """ Checks the request and trigger a light change if needed """
        if len(color) not in (1, 8) and color != self.convert(LIGHT_SKIP):
            LightManager.debugger("Unhandled color format {}".format(color), 1)
            return True
        if self.success:
            return True
        if color == self.convert(LIGHT_SKIP):
            self.success = True
            return True
        if self.priority > priority:
            LightManager.debugger("Playbulb bulb {} is set with higher priority ({}), skipping."
                                  .format(self.device, self.priority), 0)
            self.success = True
            return True
        if priority == 3:
            self.priority = 1
        else:
            self.priority = priority
        if self.state == color and color != self.convert(LIGHT_OFF):
            self.success = True
            LightManager.debugger("Bulb {} is already of the requested color, skipping."
                                  .format(self.device), 0)
            return True
        LightManager.debugger("Changing playbulb {} color to {}".format(self.device, color), 0)
        if not self._write(color): return False
        return True

    def descriptions(self):
        """ Getter for the device description """
        desctext = "[Playbulb MAC: " + self.device + "] " + self.description
        return desctext

    @connect_ble
    def _write(self, color):
        _oldcolor = self.state
        try:
            if self._connection is not None:
                    #NOT YET STABLE
#                   state = self.server.get_state(self.devid)
#                   if (state == "0"):
#                       state = "00000000"
#                   elif (state == "1"):
#                       state = self.intensity
#                   LightManager.debugger("Got color: {} and state: {}".format(color, state), 0)
#                   delta_w = (int(color[0:2]) - int(state[0:2]))/20
#                   delta_r = (int(color[2:4]) - int(state[2:4]))/20
#                   delta_g = (int(color[4:6]) - int(state[4:6]))/20
#                   delta_b = (int(color[6:8]) - int(state[6:8]))/20
#                   LightManager.debugger("deltaw: {}, deltar: {}, deltag: {}, deltab: {}".format(delta_w, delta_r, delta_g, delta_b), 0)
#                   for _iter in range(20):
#                       if (int(_iter*delta_w) != 0 and int(_iter*delta_r) != 0 and int(_iter*delta_g) != 0 and int(_iter*delta_b) != 0):
#                           deltacolor = str(int(color[0:2]) + int(_iter*delta_w)) + str(int(color[2:4]) + int(_iter*delta_r)) + str(int(color[4:6]) + int(_iter*delta_g)) + str(int(color[6:8]) + int(_iter*delta_b))
#                           self._connection.getCharacteristics(uuid="0000fffc-0000-1000-8000-00805f9b34fb")[0].write(bytearray.fromhex(deltacolor))
#                           time.sleep(0.5)


                self.state = color
                LightManager.debugger("Setting playbulb {} color to {}".format(self.device, color), 0)
                self._connection.getCharacteristics(uuid="0000fffc-0000-1000-8000-00805f9b34fb")[0] \
                                .write(bytearray.fromhex(color))

                #Prebuilt animations: blink=00, pulse=01, hard rainbow=02, smooth rainbow=03, candle=04
                #self._connection.getCharacteristics(uuid="0000fffb-0000-1000-8000-00805f9b34fb")[0].write(bytearray.fromhex(color+"02ffffff"))
                self.success = True
                LightManager.debugger("Playbulb {} color changed to {}".format(self.device, color), 0)
                return True
            self.state = _oldcolor
            LightManager.debugger("Connection error to device (playbulb) {}. Retrying" \
                                  .format(self.device), 1)
            time.sleep(0.2)
            return False

        except Exception as ex:
            #TODO manage "overwritten" thread by queued requests
            self.state = _oldcolor
            LightManager.debugger("Unhandled response. Thread died?\n{}".format(ex), 0)
            self.disconnect()
            return False


class Milight(Bulb):
    """ Methods for driving a milight BLE lightbulb """
    def __init__(self, devid, device, id1, id2, description, group, subgroup, server):
        super().__init__(devid, device, description, group, subgroup, server)
        self.device_type = "Milight"
        self.id1 = id1
        self.id2 = id2
        self.state = "0"

    def turn_on(self):
        """ Helper function to turn on device """
        return self._write(self.get_query(32, 161, 1, self.id1, self.id2), "1")

    def turn_off(self):
        """ Helper function to turn off device """
        return self._write(self.get_query(32, 161, 2, self.id1, self.id2), "0")

    def turn_on_and_set_color(self, color):
        """ Helper function to change color """
        if not self.turn_on(): return False
        return self._write(self.get_query(45, 161, 4, self.id1, self.id2, color, 2, 50), color)

    def turn_on_and_dim_on(self, color):
        """ Helper function to turn on device to default intensity """
        if not self.turn_on(): return False
        return self.dim_on(color)

    def dim_on(self, color):
        """ Helper function to set default intensity """
        return self._write(self.get_query(20, 161, 5, self.id1, self.id2, 200, 4, 50), color)

    def convert(self, color):
        """ Conversion to a color code acceptable by the device """
        #TODO rrggbb to ...this format...
        return color

    def color(self, color, priority):
        """ Checks the request and trigger a light change if needed """
        if len(color) > 3:
            LightManager.debugger("Unhandled color format {}".format(color), 1)
            return True
        if self.success:
            return True
        if color == self.convert(LIGHT_SKIP):
            self.success = True
            return True
        if self.priority > priority:
            LightManager.debugger("Milight bulb {} is set with higher priority ({}), skipping."
                                  .format(self.device, self.priority), 0)
            self.success = True
            return True
        if priority == 3:
            self.priority = 1
        else:
            self.priority = priority
        if color == self.convert(LIGHT_OFF):
            if not self.turn_off(): return False
            return True
        elif self.state == color:
            self.success = True
            LightManager.debugger("Device (milight) {} is already of the requested color, skipping."
                                  .format(self.device), 0)
            return True
        elif color == LIGHT_ON:
            if not self.turn_on_and_dim_on(color):
                return False
            return True
        else:
            if not self.turn_on_and_set_color(color): return False
            return True

    def get_query(self, value1, value2, value3, id1, id2, value4=0, value5=2, value6=0):
        """
        Generate encrypted request string.
        ON (value3 = 1)/OFF (value3 = 2): value1 = 32, value2 = 161
        CHANGE COLOR: value1 = 45, value2 = 161, value3 = 4, value4 = colorid
        """
        packet = self._create_command("[" + str(value1) + ", " + str(value2) + ", " + str(id1)
                                      + ", " + str(id2) + ", " + str(value5) + ", " + str(value3)
                                      + ", " + str(value4) + ", " + str(value6) + ", 0, 0, 0]")
        return packet

    def descriptions(self):
        """ Getter for the device description """
        desctext = "[Milight MAC: {}, ID1: {}, ID2: {}] {}" \
                    .format(self.device, self.id1, self.id2, self.description)
        return desctext

    @connect_ble
    def _write(self, command, color):
        _oldcolor = self.state
        try:
            if self._connection is not None:
                self.state = color
                LightManager.debugger("Setting milight {} color to {}".format(self.device, color), 0)
                self._connection.getCharacteristics(uuid="00001001-0000-1000-8000-00805f9b34fb")[0] \
                                                    .write(bytearray.fromhex(command \
                                                                             .replace('\n', '') \
                                                                             .replace('\r', '')))
                self.success = True
                LightManager.debugger("Milight {} color changed to {}".format(self.device, color), 0)
                return True
            self.state = _oldcolor
            LightManager.debugger("Connection error to device (milight)  {}. Retrying" \
                                  .format(self.device), 1)
            return False
        except:
            self.state = _oldcolor
            LightManager.debugger("Error sending data to device (milight) {}. Retrying" \
                                   .format(self.device), 1)
            self._connection = None
            return False

    def _create_command(self, bledata):
        _input = eval(bledata)
        k = _input[0]
        j = 0
        i = 0
        while i <= 10:
            j += _input[i] & 0xff
            i += 1
        checksum = ((((k ^ j) & 0xff) + 131) & 0xff)
        xored = [(s&0xff)^k for s in _input]
        offs = [0, 16, 24, 1, 129, 55, 169, 87, 35, 70, 23, 0]
        adds = [x+y&0xff for(x, y) in zip(xored, offs)]
        adds[0] = k
        adds.append(checksum)
        hexs = [hex(x) for x in adds]
        hexs = [x[2:] for x in hexs]
        hexs = [x.zfill(2) for x in hexs]

        return ''.join(hexs)


""" Script executed directly """
if __name__ == "__main__":
    #TODO externalize?
    PLAYCONFIG = configparser.ConfigParser()
    PLAYCONFIG.read('play.ini')
    lm = LightManager(PLAYCONFIG)

    parser = argparse.ArgumentParser(description='BLE light bulbs manager script', epilog=lm.descriptions(),
                                     formatter_class=RawTextHelpFormatter)
    parser.add_argument('hexvalues', metavar='N', type=str, nargs="*",
                        help='color hex values for the lightbulbs (see list below)')
    parser.add_argument('--playbulb', metavar='P', type=str, nargs="*", help='Change playbulbs colors only')
    parser.add_argument('--milight', metavar='M', type=str, nargs="*", help='Change milights colors only')
    parser.add_argument('--priority', metavar='prio', type=int, nargs="?", default=1,
                        help='Request priority from 1 to 3')
    parser.add_argument('--group', metavar='group', type=str, nargs="?", default=None,
                        help='Apply light actions on specified light group')
    parser.add_argument('--subgroup', metavar='group', type=str, nargs="?", default=None,
                        help='Apply light actions on specified light subgroup')
    parser.add_argument('--notime', action='store_true', default=False,
                        help='Skip the time check and run the script anyways')
    parser.add_argument('--on', action='store_true', default=False, help='Turn everything on')
    parser.add_argument('--off', action='store_true', default=False, help='Turn everything off')
    parser.add_argument('--toggle', action='store_true', default=False, help='Toggle all lights on/off')
    parser.add_argument('--server', action='store_true', default=False,
                        help='Start as a socket server daemon')
    parser.add_argument('--threaded', action='store_true', default=False,
                        help='Starts the server daemon with threaded light change requests')
    parser.add_argument('--journal', action='store_true', default=False, help='Enables file journaling')
    parser.add_argument('--tvon', action='store_true', default=False, help='Turns TV on')
    parser.add_argument('--tvoff', action='store_true', default=False, help='Turns TV off')
    parser.add_argument('--tvrestart', action='store_true', default=False, help='Reboots KODI')
    parser.add_argument('--stream-dev', metavar='str-dev', type=int, nargs="?", default=None,
                        help='Stream colors directly to device id')
    parser.add_argument('--stream-group', metavar='str-grp', type=str, nargs="?", default=None,
                        help='Stream colors directly to device group')

    args = parser.parse_args()

    if args.server and (args.playbulb or args.milight or args.on
                        or args.off or args.toggle or args.stream_dev
                        or args.stream_group):
        LightManager.debugger("You cannot start the daemon and send arguments at the same time. \
                              Quitting.", 2)
        sys.exit()

    if args.stream_dev and args.stream_group:
        LightManager.debugger("You cannot stream data to both devices and groups. Quitting.", 2)
        sys.exit()

    if args.journal:
        lm.enable_journaling()

    if args.server:
        if args.notime:
            lm.skip_time(1)
        if args.threaded:
            lm.start_threaded()
        LightServer(lm, PLAYCONFIG['SERVER']['HOST'], int(PLAYCONFIG['SERVER']['PORT'])) \
                    .listen()

    elif args.stream_dev or args.stream_group:
        colorval = ""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((PLAYCONFIG['SERVER']['HOST'], int(PLAYCONFIG['SERVER']['PORT'])))
        if args.stream_dev:
            s.sendall("0006".encode('utf-8'))
            s.sendall("stream".encode('utf-8'))
            s.sendall(('%04d' % args.stream_dev).encode('utf-8'))
            s.sendall(str(args.stream_dev).encode('utf-8'))
        else:
            s.sendall("0011".encode('utf-8'))
            s.sendall("streamgroup".encode('utf-8'))
            s.sendall(('%04d' % len(args.stream_group)).encode('utf-8'))
            s.sendall(args.stream_group.encode('utf-8'))
        while colorval != "quit":
            if args.stream_dev:
                colorval = input("Set device {} to colorvalue ('quit' to exit): " \
                                  .format(args.stream_dev))
            else:
                colorval = input("Set group '{}' to colorvalue ('quit' to exit): " \
                                  .format(args.stream_group))
            try:
                if colorval == "quit":
                    s.sendall("0008".encode('utf-8'))
                    s.sendall("nostream".encode('utf-8'))
                    break
                s.sendall(('%04d' % len(colorval)).encode('utf-8'))
                s.sendall(colorval.encode('utf-8'))
            except BrokenPipeError:
                if colorval != "quit":
                    s.close()
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.connect((PLAYCONFIG['SERVER']['HOST'], int(PLAYCONFIG['SERVER']['PORT'])))
                    if args.stream_dev:
                        s.sendall("0006".encode('utf-8'))
                        s.sendall("stream".encode('utf-8'))
                        s.sendall(('%04d' % args.stream_dev).encode('utf-8'))
                        s.sendall(str(args.stream_dev).encode('utf-8'))
                    else:
                        s.sendall("0011".encode('utf-8'))
                        s.sendall("streamgroup".encode('utf-8'))
                        s.sendall(('%04d' % len(args.stream_group)).encode('utf-8'))
                        s.sendall(args.stream_group.encode('utf-8'))
                    s.sendall(('%04d' % len(colorval)).encode('utf-8'))
                    s.sendall(colorval.encode('utf-8'))
                    continue
        s.close()

    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((PLAYCONFIG['SERVER']['HOST'], int(PLAYCONFIG['SERVER']['PORT'])))
        #TODO report connection errors or allow feedback response
        LightManager.debugger('Connecting with lightmanager daemon', 0)
        LightManager.debugger('Sending request: ' + json.dumps(vars(args)), 0)
        s.sendall("1024".encode('utf-8'))
        s.sendall(json.dumps(vars(args)).encode('utf-8'))
        s.close()

    sys.exit()
