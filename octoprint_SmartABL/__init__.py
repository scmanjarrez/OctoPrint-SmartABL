# coding=utf-8
from __future__ import absolute_import
from datetime import date, datetime

import octoprint.plugin
import logging
import json
import re


class SmartABLPlugin(octoprint.plugin.EventHandlerPlugin,
                     octoprint.plugin.SimpleApiPlugin,
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.TemplatePlugin,
                     octoprint.plugin.AssetPlugin):
    temp_regx = re.compile(r'M(?:140|104).*?S(\d+)')

    def __init__(self):
        self.smart_logger = None
        self.state = None
        self.valid_mesh = False
        self.cache = set()
        self.force_temp = False

    # Plugin: Parent class
    def initialize(self):
        console_logging_handler = logging.handlers.RotatingFileHandler(
            self._settings.get_plugin_logfile_path(), maxBytes=2*1024*1024)
        console_logging_handler.setFormatter(
            logging.Formatter('%(asctime)s %(message)s'))
        console_logging_handler.setLevel(logging.DEBUG)
        self._smartabl_logger = logging.getLogger(
            f'octoprint.plugins.{self._identifier}')
        self._smartabl_logger.addHandler(console_logging_handler)
        self._smartabl_logger.propagate = False

        try:
            with open(f'{self.get_plugin_data_folder()}/state.json') as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = dict(
                first_time=True,
                prints=0,
                last_mesh=self._today()
            )
        if 'abl_always' not in self.state:
            self.state['abl_always'] = False
        if 'last_bedtemp' not in self.state:
            self.state['last_bedtemp'] = 0
        if 'last_hetemp' not in self.state:
            self.state['last_hetemp'] = 0
        self._save()
        self._smartabl_logger.debug(
                f"@initialize > {self._dbg()}")

    # EventHandlerPlugin
    def on_event(self, event, payload):
        if event == 'ClientOpened':
            self._smartabl_logger.debug(
                f"@on_event:frontend_conn > {self._dbgstate()}")
            self._plugin_manager.send_plugin_message(
                self._identifier,
                dict(abl_always=self.state['abl_always']))
        elif event in ('PrintStarted', 'PrintDone', 'PrintFailed'):
            self._smartabl_logger.debug(
                f"@on_event > Trigger(event={event}) || "
                f"{self._dbgsettings()}")
            if event == 'PrintStarted':
                self.cache = set()
                self._printer.commands('M420 V1')
                self._smartabl_logger.debug(
                    "@on_event:print_start >> Mesh query")
            else:
                if event in self._events():
                    self.state['prints'] += 1
                self._smartabl_logger.debug(
                    f"@on_event:print_stop > {self._dbgstate()}")
                self._save()

    # SimpleApiPlugin
    def get_api_commands(self):
        return dict(
            abl_always=['value']
        )

    def on_api_command(self, command, data):
        self.state['abl_always'] = data['value']
        self._save()
        self._smartabl_logger.debug(
            f"@on_api_command:update > {self._dbgstate()}")

    # SettingsPlugin
    def get_settings_defaults(self):
        return dict(
            cmd_custom=False,
            cmd_gcode='G29',
            force_days=False,
            days=1,
            force_prints=False,
            prints=5,
            failed=False,
            bedtemp=False,
            hetemp=False
        )

    # TemplatePlugin
    def get_template_configs(self):
        return [
            dict(type='settings', custom_bindings=False)
        ]

    def get_template_vars(self):
        return dict(
            version=self._plugin_version
        )

    # AssetPlugin
    def get_assets(self):
        return dict(
            css=['css/SmartABL.css'],
            js=['js/SmartABL.js']
        )

    # Hook: octoprint.plugin.softwareupdate.check_config
    def get_update_information(self):
        return {
            'SmartABL': {
                'displayName': self._plugin_name,
                'displayVersion': self._plugin_version,

                # version check: github repository
                'type': 'github_release',
                'user': 'scmanjarrez',
                'repo': 'OctoPrint-SmartABL',
                'current': self._plugin_version,

                # update method: pip
                'pip': ('https://github.com/scmanjarrez/OctoPrint-SmartABL/'
                        'archive/{target_version}.zip')
            }
        }

    # Hook: octoprint.comm.protocol.gcode.queuing
    def queuing_gcode(self, comm_instance, phase, cmd, cmd_type,
                      gcode, *args, **kwargs):
        if ('tags' in kwargs and kwargs['tags'] is not None
                and 'source:file' in kwargs['tags']
                and gcode in ('G29', 'M420')):
            self._smartabl_logger.debug(
                f"@queuing_gcode > {self._dbg()}")
            if (self.state['abl_always']
                    or self.force_temp
                    or self.state['first_time']
                    or not self.valid_mesh
                    or (self._get('force_days')
                        and self._diff_days() >= self._get('days', 'i'))
                    or (self._get('force_prints')
                        and self.state['prints'] >= self._get('prints', 'i'))):
                self.force_temp = False
                rewrite = [self._get('cmd_gcode', 's')
                           if self._get('cmd_custom')
                           else cmd, 'M500']
                self._smartabl_logger.debug(
                    f"@queuing_gcode:abl_trigger >> Sending {rewrite} > "
                    f"{self._dbginternal()}")
                return rewrite
            else:
                self.cache.add('M500')
                self._smartabl_logger.debug(
                    "@queuing_gcode:abl_skip >> Sending M420 S1 > "
                    f"{self._dbginternal()}")
                return 'M420 S1'
        return cmd,

    # Hook: octoprint.comm.protocol.gcode.sent
    def sent_gcode(self, comm_instance, phase, cmd, cmd_type,
                   gcode, *args, **kwargs):
        if ('tags' in kwargs and kwargs['tags'] is not None
                and f'plugin:{self._identifier}' in kwargs['tags']
                and 'source:file' in kwargs['tags']
                and gcode in ('M140', 'M104', 'M500')):
            self._smartabl_logger.debug(
                f"@sent_gcode > Trigger(cmd={cmd}) || {self._dbg()}")
            if gcode in ('M140', 'M104') and gcode not in self.cache:
                self.cache.add(gcode)
                setting = 'bedtemp'
                state = 'last_bedtemp'
                if gcode == 'M104':
                    setting = 'hetemp'
                    state = 'last_hetemp'
                try:
                    temp = int(self.temp_regx.match(cmd).group(1))
                except AttributeError:
                    self._smartabl_logger.debug(
                        f"@sent_gcode:parse_error > Trigger(cmd={cmd}) || "
                        f"{self._dbg()}")
                else:
                    if (self._get(setting)
                            and temp != self.state[state]):
                        self.force_temp = True
                    self.state[state] = temp
                    self._save()
                    self._smartabl_logger.debug(
                        f"@sent_gcode:update > {self._dbgstate()} || "
                        f"{self._dbginternal()}")
            elif gcode == 'M500' and gcode not in self.cache:
                self.cache.add(gcode)
                if self.state['first_time']:
                    self.state['first_time'] = False
                self.state['prints'] = 0
                self.state['last_mesh'] = self._today()
                self._save()
                self._smartabl_logger.debug(
                    f"@sent_gcode:update > {self._dbgstate()} || "
                    f"{self._dbginternal()}")

    # Hook: octoprint.comm.protocol.gcode.received
    def process_line(self, comm_instance, line, *args, **kwargs):
        if 'Invalid mesh' in line:
            self.valid_mesh = False
            self._smartabl_logger.debug(
                f"@process_line:update > {self._dbginternal()}")
        elif 'Bilinear Leveling Grid:' in line:
            self.valid_mesh = True
            self._smartabl_logger.debug(
                f"@process_line:update > {self._dbginternal()}")
        # elif 'M420 S1.0 Z0.0' in line:
        #     self.valid_mesh = True
        #     self._smartabl_logger.debug(
        #         f"@process_line:VIRTUALPRINTER > {self._dbginternal()}")
        return line

    def _today(self):
        return date.today().strftime('%d/%m/%Y')

    def _diff_days(self):
        return (date.today() -
                datetime.strptime(
                    self.state['last_mesh'], '%d/%m/%Y').date()).days

    def _get(self, key, ktype='i'):
        if ktype == 'i':
            return self._settings.get_int([key])
        elif ktype == 's':
            return self._settings.get([key])
        return self._settings.get_boolean([key])

    def _dbg(self):
        return (f"{self._dbgsettings()} || "
                f"{self._dbgstate()} || "
                f"{self._dbginternal()}")

    def _dbgsettings(self):
        return (f"Settings("
                f"force_days={self._get('force_days')}, "
                f"days={self._get('days', 'i')}, "
                f"force_prints={self._get('force_prints')}, "
                f"prints={self._get('prints', 'i')}, "
                f"failed={self._get('failed')}, "
                f"bedtemp={self._get('bedtemp')}, "
                f"hetemp={self._get('hetemp')}"
                f")")

    def _dbgstate(self):
        return (f"State("
                f"first_time={self.state['first_time']}, "
                f"prints={self.state['prints']}, "
                f"last_mesh={self.state['last_mesh']}, "
                f"abl_always={self.state['abl_always']}, "
                f"last_bedtemp={self.state['last_bedtemp']}, "
                f"last_hetemp={self.state['last_hetemp']}"
                f")")

    def _dbginternal(self):
        return (f"Internal("
                f"valid_mesh={self.valid_mesh}, "
                f"cache={self.cache}, "
                f"force_temp={self.force_temp}"
                f")")

    def _events(self):
        return (('PrintDone',) if not self._get('failed')
                else ('PrintDone', 'PrintFailed'))

    def _save(self):
        with open(f'{self.get_plugin_data_folder()}/state.json', 'w') as f:
            json.dump(self.state, f)


__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_name__ = "SmartABL"


def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__

    __plugin_implementation__ = SmartABLPlugin()
    __plugin_hooks__ = {
        'octoprint.plugin.softwareupdate.check_config': (
            __plugin_implementation__.get_update_information),
        'octoprint.comm.protocol.gcode.queuing': (
            __plugin_implementation__.queuing_gcode),
        'octoprint.comm.protocol.gcode.sent': (
            __plugin_implementation__.sent_gcode),
        'octoprint.comm.protocol.gcode.received': (
            __plugin_implementation__.process_line)
    }
