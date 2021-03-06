#!/usr/bin/env python3
"""
Simple playserver IFTTT server
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
import os
import time
import datetime
import hashlib
from __main__ import *

SALT = "mazout360"

class LightManager(object):
    """ Methods for instanciating and managing BLE lightbulbs """
    @staticmethod
    def debugger(msg, level):
        levels = {0: "DEBUG", 1: "ERROR", 2: "FATAL"}
        debugtext = "({}) - [{}] {}".format(datetime.datetime.now().time(), levels[level], msg)
        print(debugtext)
        with open("./server.0.log", "a") as jfile:
            jfile.write(debugtext + "\n")

if os.path.isfile("./server.0.log"):
    if os.path.isfile("./server.1.log"):
        if os.path.isfile("./server.2.log"):
            os.remove("./server.2.log")
        os.rename("./server.1.log", "./server.2.log")
    os.rename("./server.0.log", "./server.1.log")

class S(BaseHTTPRequestHandler):
    def _set_response(self):
        self.send_response(200)
        self.send_header('Content-type', 'x-www-form-urlencoded')
        self.end_headers()

    def do_POST(self):
        """ Receives and handles POST request """
        LightManager.debugger('Getting request', 0)
        content_length = int(self.headers['Content-Length']) # <--- Gets the size of data
        postvars = urllib.parse.parse_qs(self.rfile.read(content_length), keep_blank_values=1)
        action = postvars[b'action'][0].decode('utf-8')
        _hash = postvars[b'hash'][0].decode('utf-8')

        if _hash == hashlib.sha512(bytes(SALT.encode('utf-8') + action.encode('utf-8'))).hexdigest():
            LightManager.debugger('Running action : {}\n'.format(action), 0)
            if action == "lumieres_salon_off":
                os.system('./playclient.py --off --notime --priority 3 --group salon')
            elif action == "lumieres_salon_on":
                os.system('./playclient.py --on --notime --priority 2 --group salon')
            elif action == "luminaire_passage_off":
                os.system('./playclient.py --off --notime --priority 3 --group passage')
            elif action == "luminaire_passage_on":
                os.system('./playclient.py --on --notime --priority 2 --group passage')
            elif action == "television_salon_on":
                os.system('./playclient.py --tvon --priority 3')
                time.sleep(2)
                os.system('/usr/sbin/ether-wake 4C:CC:6A:F4:79:EC -i br0')
            elif action == "television_salon_off":
                os.system('./playclient.py --tvoff --priority 3')
            elif action == "television_salon_restart":
                os.system('./playclient.py --tvrestart')
            elif action == "salon_close":
                os.system('./playclient.py --tvoff --off --notime --priority 3 --group salon')
            elif action == "luminaire_salon_off":
                os.system('./playclient.py --off --notime --priority 3 --group salon --subgroup luminaire')
            elif action == "luminaire_salon_on":
                os.system('./playclient.py --on --notime --priority 2 --group salon --subgroup luminaire')
            elif action == "lumieres_on":
                os.system('./playclient.py --on --notime --priority 2')
            elif action == "lumieres_off":
                os.system('./playclient.py --off --notime --priority 3')
        else:
            LightManager.debugger('Unwanted request with action : {}\n'.format(action), 1)

        self._set_response()
        self.wfile.write("POST request for {}".format(self.path).encode('utf-8'))

def run(server_class=HTTPServer, handler_class=S, port=1234):
    """ Runs the IFTTT server """
    server_address = ('', port)
    httpd = server_class(server_address, handler_class)
    LightManager.debugger('Starting http webserver for getting lightserver POST requests on port {}\n' \
                          .format(port), 0)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    LightManager.debugger('Stopping webserver', 0)

if __name__ == '__main__':
    run()
