#!/usr/bin/env python2
# -*- coding: utf-8 -*-


import os
from datetime import datetime, timedelta
import time
import subprocess
import shelve
import re

import httplib2

# package python2-google-api-python-client
from apiclient.discovery import build
from oauth2client.file import Storage
from oauth2client.client import OAuth2WebServerFlow
from oauth2client.tools import run_flow
from oauth2client import tools


class AtError(Exception):
    pass

class AtApi:
    def __init__(self,
                 verbose_level=0):
        self.verbose_level = verbose_level

    def run_at(self, dt, cmd):
        "Program the AT daemon to run the cmd at the given datetime"
        dt_now = datetime.now()
        delta_minutes = max(0,
                            (dt - dt_now).total_seconds() / 60 +1)
        at_cmd_l = ["at", "now + %d minutes" % delta_minutes]

        p = subprocess.Popen(at_cmd_l,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT
            )
        p.stdin.write(cmd + "\n")
        p.stdin.close()
        ret = p.wait()
        if ret != 0:
            raise AtError(p.stdout.readlines())


def get_RFC3339(dt_utc):
    "Returns a string of the correct format given a datetime object"
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

def from_RFC3339(s):
    "Returns a datetime object from the given string"
    dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    return dt

def utc_from_RFC3339(s):
    "Returns a datetime object from the given string"
    dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    assert s[22] == ":"
    utcoffset = timedelta(hours=int(s[20:22]), minutes=int(s[23:25]))
    sig = s[19]
    assert sig in "+-"
    if sig == "+":
        return dt - utcoffset
    else:
        return dt + utcoffset

class CronCalendar:
    """
    Should be called periodicaly, query the google calendar, and
    program AT daemon to execute commands from the 'description' field

    """
    tokenMatchRegExp=r"""^ \s* {} (?P<cmd_match>.+)"""

    def __init__(self,
                 conf,
                 logger,
                 verbose_level=0,
                 dryrun=False):
        self.conf = conf
        self.logger = logger
        self.verbose_level = verbose_level
        self.dryrun = dryrun

    def get_calendar_service(self):
        " Connect to google calendar"
        storage = Storage(os.path.expanduser(self.conf.get("storage", "credential_file")))
        credentials = storage.get()


        flow = OAuth2WebServerFlow(
            client_id=self.conf.get("google_api", "client_id"),
            client_secret=self.conf.get("google_api", "client_secret"),
            scope='https://www.googleapis.com/auth/calendar',
            user_agent='CronCalendar/1.0')

        if credentials is None or credentials.invalid == True:
            flags = tools.argparser.parse_args(args=["--noauth_local_webserver"])
            credentials = run_flow(flow, storage, flags)

        # Create an httplib2.Http object to handle our HTTP requests and authorize it
        # with our good Credentials.
        http = httplib2.Http()
        http = credentials.authorize(http)

        service = build("calendar", "v3", http=http)

        return service

    def __get_query_utc_dt(self):
        self.shelve_dict = shelve.open(os.path.expanduser(self.conf.get("storage", "shelve_file")))

        dt_utcnow = datetime.utcnow()
        dt_utcnow -= timedelta(seconds=dt_utcnow.second, # Make it aligned to minutes starts
                               microseconds=dt_utcnow.microsecond)

        # Make sure we don't query twice the same time range.
        if "last_utctime_max" in self.shelve_dict:
            dt_utctime_min = max(dt_utcnow,
                              self.shelve_dict["last_utctime_max"])
        else:
            dt_utctime_min = dt_utcnow

        dt_utctime_max = dt_utcnow + timedelta(minutes=self.conf.getint("general", "advance_minute"))

        return dt_utctime_min, dt_utctime_max

    def __match_cmd(self,
                    at,
                    event,
                    dt_utctime_min,
                    dt_utctime_max):
        # Start Event is in the range
        cmd_start_str = event['start']["dateTime"]
        cmd_start_dt_utc = utc_from_RFC3339(cmd_start_str)

        # end Event is in the range
        cmd_end_str = event['end']["dateTime"]
        cmd_end_dt_utc = utc_from_RFC3339(cmd_end_str)

        if not(dt_utctime_min <= cmd_start_dt_utc < dt_utctime_max) and not(dt_utctime_min < cmd_end_dt_utc <= dt_utctime_max):
            self.logger.debug("Start_time and End_time are not inside boundaries(min<=dt<max)")
            return

        description_str = event.get("description")
        summary_str = event.get("summary")
        if not description_str:
            self.logger.debug("No command for [{}]".format(summary_str))
        else:
            startToken = self.conf.get("general", "start_token")
            if_startToken = self.conf.get("general", "if_start_token")
            else_startToken = self.conf.get("general", "else_start_token")

            stopToken = self.conf.get("general", "stop_token")
            if_stopToken = self.conf.get("general", "if_stop_token")
            else_stopToken = self.conf.get("general", "else_stop_token")

            cmd_dicts = {'start':      {'reg_exp': re.compile(self.tokenMatchRegExp.format(startToken),      re.VERBOSE|re.IGNORECASE), 'value':[]},
                         'if_start':   {'reg_exp': re.compile(self.tokenMatchRegExp.format(if_startToken),   re.VERBOSE|re.IGNORECASE), 'value':[]},
                         'else_start': {'reg_exp': re.compile(self.tokenMatchRegExp.format(else_startToken), re.VERBOSE|re.IGNORECASE), 'value':[]},
                         'end':        {'reg_exp': re.compile(self.tokenMatchRegExp.format(stopToken),       re.VERBOSE|re.IGNORECASE), 'value':[]},
                         'if_end':     {'reg_exp': re.compile(self.tokenMatchRegExp.format(if_stopToken),    re.VERBOSE|re.IGNORECASE), 'value':[]},
                         'else_end':   {'reg_exp': re.compile(self.tokenMatchRegExp.format(else_stopToken),  re.VERBOSE|re.IGNORECASE), 'value':[]},
                         'coms':       {'reg_exp': re.compile(self.tokenMatchRegExp.format("\#"),             re.VERBOSE|re.IGNORECASE), 'value':[]},
                         'none':       {'reg_exp': re.compile(self.tokenMatchRegExp.format(''),              re.VERBOSE|re.IGNORECASE), 'value':[]}}
            keys_tuple=['start', 'if_start', 'else_start', 'end', 'if_end', 'else_end', 'coms', 'none'] # important to check none in the last position

            for line in description_str.split("\n"):
                for cmd_key in keys_tuple:
                    match = cmd_dicts[cmd_key]['reg_exp'].match(line)
                    if match:
                        self.logger.debug("Line: '{}' - OK match '{}'".format(line, cmd_key))

                        # Append command to the right list
                        cmd_dicts[cmd_key]['value'].append(match.group('cmd_match'))
                        break
                    else:
                        self.logger.debug("Line: '{}' - NO match '{}'".format(line, cmd_key))

            if not self.dryrun:
                # prepare test commands
                if_cmd = {'start':{'if':"",'else':""},
                          'end':{'if':"",'else':""}}
                for cmd_key in ['start', 'end']:
                    if cmd_dicts['if_'+cmd_key]['value']: # Check if if_start/if_end exist
                        if_cmd[cmd_key]['if']   = cmd_dicts['if_'+cmd_key]['value'][0] + " && "
                        if_cmd[cmd_key]['else'] = cmd_dicts['if_'+cmd_key]['value'][0] + " || "

                for cmd_key in ['start', 'none', 'end', 'else_start', 'else_end']:
                    if ((cmd_key == "start") or (cmd_key == "none") or ((cmd_key == "else_start") and (if_cmd['start']['else']!=""))) and (dt_utctime_min <= cmd_start_dt_utc < dt_utctime_max) and cmd_dicts[cmd_key]['value']:
                        self.logger.debug("Token '{}': time inside boundaries(min<=dt<max)".format(cmd_key))
                        cmd_start_dt = from_RFC3339(cmd_start_str )
                        for cmd_value in cmd_dicts[cmd_key]['value']:
                            command = if_cmd['start']['else' if (cmd_key.find('else') != -1) else 'if'] + cmd_value
                            self.logger.info("Token '{}': Programming from [{}] at {} command: [{}]".format(cmd_key,summary_str, cmd_start_dt, command))
                            at.run_at(cmd_start_dt, command)

                    elif ((cmd_key == "end") or ((cmd_key == "else_end") and (if_cmd['end']['else']!=""))) and (dt_utctime_min < cmd_end_dt_utc <= dt_utctime_max) and cmd_dicts[cmd_key]['value']:
                        self.logger.debug("Token '{}': time inside boundaries(min<dt<=max)".format(cmd_key))
                        cmd_end_dt = from_RFC3339(cmd_end_str )
                        for cmd_value in cmd_dicts[cmd_key]['value']:
                            command = if_cmd['end']['else' if (cmd_key.find('else') != -1) else 'if'] + cmd_value
                            self.logger.info("Token '{}': Programming from [{}] at {} command: [{}]".format(cmd_key,summary_str, cmd_end_dt, command))
                            at.run_at(cmd_end_dt, command)
        return


    def __program_at(self,
                     res,
                     dt_utctime_min,
                     dt_utctime_max):
        " From the raw results from the google api, program AT"
        at = AtApi()

        if "items" in res:
            for event in res["items"]:
                self.__match_cmd(at, event, dt_utctime_min, dt_utctime_max)

    def run(self):

        # Connect to google API
        self.service = self.get_calendar_service()

        dt_utctime_min, dt_utctime_max = self.__get_query_utc_dt()

        if dt_utctime_min == dt_utctime_max:
            self.logger.info("Nothing to query")
        else:

            time_min = get_RFC3339(dt_utctime_min)
            time_max = get_RFC3339(dt_utctime_max)

            self.logger.debug("Querying calendar from " + str(time_min) + " to " + str(time_max))
            req = self.service.events().list(calendarId=self.conf.get("general", "calendar_id"),
                                             singleEvents=True,  # Make sure regular events are expanded
                                             orderBy="startTime",
                                             timeMin=time_min,
                                             timeMax=time_max
                                             )

            res = req.execute()

            if self.verbose_level >= 2:
                from pprint import pprint
                pprint(res)

            self.__program_at(res,
                              dt_utctime_min,
                              dt_utctime_max)

            # If we are here, programmation went well, and we can update the range
            self.shelve_dict["last_utctime_min"] = dt_utctime_min
            self.shelve_dict["last_utctime_max"] = dt_utctime_max
