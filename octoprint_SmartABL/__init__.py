# coding=utf-8
from __future__ import absolute_import
from datetime import date, datetime

import octoprint.plugin
import logging
import json


class SmartABLPlugin(octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.AssetPlugin,
                     octoprint.plugin.TemplatePlugin,
                     octoprint.plugin.EventHandlerPlugin):
    def __init__(self):
        self.smart_logger = None
        self.state = None
        self.valid_mesh = False

    # Plugin: Parent class
    def initialize(self):
        console_logging_handler = logging.handlers.RotatingFileHandler(
            self._settings.get_plugin_logfile_path(), maxBytes=2*1024*1024)
        console_logging_handler.setFormatter(
            logging.Formatter('%(asctime)s %(message)s'))
        console_logging_handler.setLevel(logging.DEBUG)
        self._smartabl_logger = logging.getLogger(
            f'octoprint.plugins.{self._plugin_name}')
        self._smartabl_logger.addHandler(console_logging_handler)
        self._smartabl_logger.propagate = False

        try:
            with open(f'{self.get_plugin_data_folder()}/state.json') as f:
                self.state = json.load(f)
            self._smartabl_logger.debug(
                f"@initialize > {self._debug()}")
        except FileNotFoundError:
            self.state = dict(
                first_time=True,
                prints=0,
                last_mesh=self._today()
            )
            self._save()
            self._smartabl_logger.debug(
                f"@initialize:state_new > {self._debug()}")

    # SettingsPlugin
    def get_settings_defaults(self):
        return dict(
            force_days=False,
            days=1,
            force_prints=False,
            prints=5,
            failed=False
        )

    # TemplatePlugin
    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=False)
        ]

    def get_template_vars(self):
        return dict(
            version=self._plugin_version
        )

    # Hook: octoprint.plugin.softwareupdate.check_config
    def get_update_information(self):
        return {
            "SmartABL": {
                "displayName": self._plugin_name,
                "displayVersion": self._plugin_version,

                # version check: github repository
                "type": "github_release",
                "user": "scmanjarrez",
                "repo": "OctoPrint-SmartABL",
                "current": self._plugin_version,

                # update method: pip
                "pip": ("https://github.com/scmanjarrez/OctoPrint-SmartABL/"
                        "archive/{target_version}.zip",)
            }
        }

    # Hook: octoprint.comm.protocol.gcode.queuing
    def queuing_gcode(self, comm_instance, phase, cmd, cmd_type,
                      gcode, *args, **kwargs):
        if 'source:file' in kwargs['tags'] and gcode in ('G29', 'M420'):
            self._smartabl_logger.debug(
                f"@queuing_gcode > {self._debug()}")
            if (self.state['first_time'] or not self.valid_mesh
                    or (self._get('force_days', 'b')
                        and self._diff_days() >= self._get('days'))
                    or (self._get('force_prints', 'b')
                        and self.state['prints'] >= self._get('prints'))):
                self._smartabl_logger.debug(
                    "@queuing_gcode:abl_trigger")
                return ['G29', 'M500']
            else:
                self._smartabl_logger.debug(
                    "@queuing_gcode:abl_skip")
                return 'M420 S1'
        return cmd,

    # Hook: octoprint.comm.protocol.gcode.sent
    def sent_gcode(self, comm_instance, phase, cmd, cmd_type,
                   gcode, *args, **kwargs):
        if (f'plugin:{self._plugin_name}' in kwargs['tags']
                and 'source:file' in kwargs['tags']
                and gcode == 'M500'):
            self._smartabl_logger.debug(
                f"@sent_gcode > {self._debug()} || Trigger(gcode={gcode})")
            _tmp = ('first_time=False, ' if self.state['first_time']
                    else '')
            if self.state['first_time']:
                self.state['first_time'] = False
            self.state['prints'] = 0
            self.state['last_mesh'] = self._today()
            self._save()
            self._smartabl_logger.debug(
                f"@sent_gcode:mesh_update >> {_tmp}prints=0, "
                f"last_mesh={self._today()}")

    # Hook: octoprint.comm.protocol.gcode.received
    def process_line(self, comm_instance, line, *args, **kwargs):
        if 'Invalid mesh' in line:
            self.valid_mesh = False
            self._smartabl_logger.debug(
                "@process_line:mesh_invalid >> valid_mesh=False")
        elif 'Bilinear Leveling Grid:' in line:
            self.valid_mesh = True
            self._smartabl_logger.debug(
                "@process_line:mesh_valid >> valid_mesh=True")
        return line

    # EventHandlerPlugin
    def on_event(self, event, payload):
        if event in ('PrintStarted', 'PrintDone', 'PrintFailed'):
            self._smartabl_logger.debug(
                f"@on_event > {self._debug()} || Trigger(event={event})")
            if event == 'PrintStarted':
                self._printer.commands('M420 V1')
                self._smartabl_logger.debug(
                    "@on_event:print_start >> Mesh query")
            else:
                if event in self._events():
                    self.state['prints'] += 1
                self._smartabl_logger.debug(
                    f"@on_event:print_stop >> prints={self.state['prints']}")
                self._save()

    def _today(self):
        return date.today().strftime('%d/%m/%Y')

    def _diff_days(self):
        return (date.today() -
                datetime.strptime(
                    self.state['last_mesh'], '%d/%m/%Y').date()).days

    def _get(self, key, ktype='i'):
        if ktype == 'b':
            return self._settings.get_boolean([key])
        return self._settings.get_int([key])

    def _debug(self):
        return (f"Settings(force_days={self._get('force_days', 'b')}, "
                f"days={self._get('days')}, "
                f"force_prints={self._get('force_prints', 'b')}, "
                f"prints={self._get('prints')}), "
                f"failed={self._get('failed', 'b')}) || "
                f"State(first_time={self.state['first_time']}, "
                f"prints={self.state['prints']}, "
                f"last_mesh={self.state['last_mesh']}) || "
                f"Internal(valid_mesh={self.valid_mesh})")

    def _events(self):
        return (('PrintDone',) if not self._get('failed', 'b')
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
