# coding=utf-8
from __future__ import absolute_import
from datetime import date, datetime

import octoprint.plugin
import logging
import json


class SmartABLPlugin(octoprint.plugin.AssetPlugin,
                     octoprint.plugin.EventHandlerPlugin,
                     octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.SimpleApiPlugin,
                     octoprint.plugin.TemplatePlugin):
    fw_metadata = {
        'marlin': {
            'codename': 'Marlin',
            'abl': 'G29',
            'load': 'M420 S1',
            'info': ('M420 V1',
                     'Invalid mesh',
                     'Bilinear Leveling Grid:'),
            'save': 'M500'
        },
        'prusa': {
            'codename': 'Prusa-Firmware',
            'abl': 'G80',
            'info': ('G81',
                     'Mesh bed leveling not active.',
                     'Measured points:')
        },
        'klipper': {
            'codename': 'Klipper',
            'abl': 'BED_MESH_CALIBRATE',
            'info': ('BED_MESH_OUTPUT',
                     '// Bed has not been probed',
                     '// Mesh Leveling Probed Z positions:')
        }
    }
    temp = {
        'he': 'M109',
        'bed': 'M190'
    }

    def __init__(self):
        self.smart_logger = None
        self.state = None
        self.valid_mesh = False
        self.cache = set()
        self.force_temp = False
        self.firmware = None
        self.probed = False
        self.save_allowed = True

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

    # AssetPlugin
    def get_assets(self):
        return dict(
            css=['css/SmartABL.css'],
            js=['js/SmartABL.js']
        )

    # SettingsPlugin
    def get_settings_defaults(self):
        return dict(
            cmd_custom=False,
            custom_gcode='G29',
            cmd_ignore=False,
            ignore_gcode='',
            force_days=False,
            days=1,
            force_prints=False,
            prints=5,
            failed=False,
            bedtemp=False,
            hetemp=False
        )

    # SimpleApiPlugin
    def get_api_commands(self):
        return dict(
            abl_always=['value']
        )

    def on_api_command(self, command, data):
        self.state['abl_always'] = data['value']
        self._save()
        self._smartabl_logger.debug(
            f"@on_api_command:update_button > {self._dbgstate()}")

    # TemplatePlugin
    def get_template_configs(self):
        return [
            dict(type='settings', custom_bindings=False)
        ]

    def get_template_vars(self):
        return dict(
            version=self._plugin_version
        )

    # EventHandlerPlugin
    def on_event(self, event, payload):
        if event == 'ClientOpened':
            self._plugin_manager.send_plugin_message(
                self._identifier, {
                    'abl_always': self.state['abl_always']})
            self._update_frontend()
        elif event == 'Disconnected':
            self.firmware = None
        if (self.firmware is not None
                and event in ('PrintStarted', 'PrintDone', 'PrintFailed')):
            self._smartabl_logger.debug(
                f"@on_event > Trigger(event={event}) || "
                f"{self._dbgsettings()}")
            if event == 'PrintStarted':
                self.cache = set()
                cmd = self.fw_metadata[self.firmware]['info'][0]
                self._printer.commands(cmd)
                self._smartabl_logger.debug(
                    f"@on_event:print_start >> Mesh query(cmd={cmd})")
            else:
                if event in self._events():
                    self.state['prints'] += 1
                self._smartabl_logger.debug(
                    f"@on_event:print_stop > {self._dbgstate()}")
                self._update_frontend()
                self._save()

    # Hook: octoprint.comm.protocol.gcode.received
    def process_line(self, comm_instance, line, *args, **kwargs):
        if self.firmware is None:
            if 'FIRMWARE_NAME' in line:
                for fw_cn, fw_n in self._codenames():
                    if fw_cn in line:
                        self.firmware = fw_n
                        if self.firmware != 'marlin':
                            self.save_allowed = False
                            self.probed = False
                        self._smartabl_logger.debug(
                            f"@process_line:firmware >> {fw_n}")
                        break
                else:
                    self._smartabl_logger.debug(
                        "@process_line:firmware >> Unknown")
                    self._plugin_manager.send_plugin_message(
                        self._identifier, {
                            'abl_notify': ("SmartABL: disabled",
                                           "Unknown firmware. Open an Issue "
                                           "on GitHub indicating your "
                                           "firmware and logs.")})
        else:
            if 'EEPROM disabled' in line:  # marlin eeprom disabled
                self.save_allowed = False
            elif self.fw_metadata[self.firmware]['info'][1] in line:
                self.valid_mesh = False
                self._smartabl_logger.debug(
                    f"@process_line:invalid_mesh > {self._dbginternal()}")
            elif self.fw_metadata[self.firmware]['info'][2] in line:
                self.valid_mesh = True
                self._smartabl_logger.debug(
                    f"@process_line:valid_mesh > {self._dbginternal()}")
            # elif 'M420 S1.0 Z0.0' in line:
            #     self.valid_mesh = True
            #     self._smartabl_logger.debug(
            #         f"@process_line:VIRTUALPRINTER > "
            #         f"{self._dbginternal()}")
        return line

    # Hook: octoprint.comm.protocol.atcommand.sending
    def at_command(self, comm_instance, phase, cmd, parameters,
                   tags=None, *args, **kwargs):
        if cmd == 'SMARTABLSAVE':
            if self.state['first_time']:
                self.state['first_time'] = False
            self.state['prints'] = 0
            self.state['last_mesh'] = self._today()
            self._save()
            self._smartabl_logger.debug(
                f"@at_command:update_state > {self._dbgstate()} || "
                f"{self._dbginternal()}")
            self._update_frontend()

    # Hook: octoprint.comm.protocol.gcode.queuing
    def gcode_queuing(self, comm_instance, phase, cmd, cmd_type,
                      gcode, *args, **kwargs):
        if (self.firmware is not None
                and 'tags' in kwargs and kwargs['tags'] is not None
                and 'source:file' in kwargs['tags']):
            if (self._get('cmd_ignore')
                and (gcode in self._gcodes_ignore()
                     or cmd in self._gcodes_ignore())):
                self._smartabl_logger.debug(
                    f"@gcode_queuing:ignore > {self._dbg()}")
                return [None]
            elif gcode in self._gcodes_abl() or cmd in self._gcodes_abl():
                self._smartabl_logger.debug(
                    f"@gcode_queuing:abl > {self._dbg()}")
                if (self.state['abl_always']
                        or not self.probed
                        or self.force_temp
                        or self.state['first_time']
                        or not self.valid_mesh
                        or (self._get('force_days')
                            and self._diff_days()
                            >= self._get('days', 'i'))
                        or (self._get('force_prints')
                            and self.state['prints']
                            >= self._get('prints', 'i'))):
                    self.force_temp = False
                    if gcode == 'M420':
                        rewrite = [self.fw_metadata[self.firmware]['abl']]
                    else:
                        rewrite = [cmd]
                    if self._get('cmd_custom'):
                        rewrite = [self._get('custom_gcode', 's')]
                    if self.save_allowed:
                        self.cache.add(self.fw_metadata[self.firmware]['save'])
                    rewrite.append('@SMARTABLSAVE')
                    self._smartabl_logger.debug(
                        f"@gcode_queuing:abl_trigger >> Sending {rewrite} > "
                        f"{self._dbginternal()}")
                    self.probed = True
                    return rewrite
                else:
                    rewrite = [None]
                    if self.save_allowed:
                        rewrite = [self.fw_metadata[self.firmware]['load']]
                    self._smartabl_logger.debug(
                        f"@gcode_queuing:abl_skip({self.firmware}) "
                        f">> Sending {rewrite} > {self._dbginternal()}")
                    return rewrite
        return [cmd]

    # Hook: octoprint.comm.protocol.gcode.sent
    def gcode_sent(self, comm_instance, phase, cmd, cmd_type,
                   gcode, *args, **kwargs):
        if (self.firmware is not None
                and 'tags' in kwargs and kwargs['tags'] is not None
                and f'plugin:{self._identifier}' in kwargs['tags']
                and 'source:file' in kwargs['tags']
                and gcode in self._gcodes_temp()):
            self._smartabl_logger.debug(
                f"@gcode_sent:temp > Trigger(cmd={cmd}) || {self._dbg()}")
            if gcode not in self.cache:
                self.cache.add(gcode)
                setting = 'bedtemp'
                state = 'last_bedtemp'
                if gcode == self.temp['he']:
                    setting = 'hetemp'
                    state = 'last_hetemp'
                try:
                    temp = int(self.temp_regx.match(cmd).group(1))
                except AttributeError:
                    self._smartabl_logger.debug(
                        f"@gcode_sent:temp_error > Trigger(cmd={cmd}) || "
                        f"{self._dbg()}")
                else:
                    if (self._get(setting) and temp != self.state[state]):
                        self.force_temp = True
                    self.state[state] = temp
                    self._save()
                    self._smartabl_logger.debug(
                        f"@gcode_sent:{setting} > "
                        f"{self._dbgstate()} || {self._dbginternal()}")

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

    def _today(self):
        return date.today().strftime('%d/%m/%Y')

    def _diff_days(self):
        return (date.today() -
                datetime.strptime(
                    self.state['last_mesh'], '%d/%m/%Y').date()).days

    def _codenames(self):
        return [(fw_d['codename'], fw_n)
                for fw_n, fw_d in self.fw_metadata.items()]

    def _gcodes_abl(self):
        return ([fw['abl'] for fw in self.fw_metadata.values()] +
                [self.fw_metadata['marlin']['load'].split()[0]])

    def _gcodes_temp(self):
        return [self.temp[tmp] for tmp in self.temp]

    def _gcodes_ignore(self):
        return [gc for gc in self._get('ignore_gcode', 's').split(',')]

    def _get(self, key, ktype='b'):
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
                f"cmd_custom={self._get('cmd_custom')}, "
                f"custom_gcode={self._get('custom_gcode', 's')}, "
                f"cmd_ignore={self._get('cmd_custom')}, "
                f"ignore_gcode={self._get('ignore_gcode', 's')}, "
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
                f"force_temp={self.force_temp}, "
                f"firmware={self.firmware}, "
                f"probed={self.probed}, "
                f"save_allowed={self.save_allowed}"
                f")")

    def _events(self):
        return (('PrintDone',) if not self._get('failed')
                else ('PrintDone', 'PrintFailed'))

    def _save(self):
        with open(f'{self.get_plugin_data_folder()}/state.json', 'w') as f:
            json.dump(self.state, f)

    def _update_frontend(self):
        self._plugin_manager.send_plugin_message(
            self._identifier, {
                'abl_counter': (self.state['prints'],
                                self._get('prints', 'i'))})


__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_name__ = "SmartABL"


def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__

    __plugin_implementation__ = SmartABLPlugin()
    __plugin_hooks__ = {
        'octoprint.comm.protocol.atcommand.sending': (
            __plugin_implementation__.at_command),
        'octoprint.comm.protocol.gcode.queuing': (
            __plugin_implementation__.gcode_queuing),
        'octoprint.comm.protocol.gcode.received': (
            __plugin_implementation__.process_line),
        'octoprint.comm.protocol.gcode.sent': (
            __plugin_implementation__.gcode_sent),
        'octoprint.plugin.softwareupdate.check_config': (
            __plugin_implementation__.get_update_information)
    }
