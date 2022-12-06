# coding=utf-8
from __future__ import absolute_import
from datetime import date, datetime

import octoprint.plugin
import logging
import json


__plugin_pythoncompat__ = ">=3.7,<4"


class SmartABLPlugin(octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.AssetPlugin,
                     octoprint.plugin.TemplatePlugin,
                     octoprint.plugin.EventHandlerPlugin):
    def __init__(self):
        self.smart_logger = None
        self.state = None

    def initialize(self):
        console_logging_handler = logging.handlers.RotatingFileHandler(
            self._settings.get_plugin_logfile_path(), maxBytes=2*1024*1024)
        console_logging_handler.setFormatter(
            logging.Formatter('%(asctime)s %(message)s'))
        console_logging_handler.setLevel(logging.DEBUG)
        self._smartabl_logger = logging.getLogger(
            f'octoprint.plugins.{__name__}')
        self._smartabl_logger.addHandler(console_logging_handler)
        self._smartabl_logger.setLevel(logging.DEBUG)
        self._smartabl_logger.propagate = False
        try:
            with open(f'{self.get_plugin_data_folder()}/state.json') as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = dict(
                first_time=True,
                prints=0,
                last_print=self._today()
            )

    def get_settings_defaults(self):
        return dict(
            force_days=False,
            days=1,
            force_prints=True,
            prints=5,
            failed=False
        )

    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=False)
        ]

    def get_template_vars(self):
        return dict(
            version=self._plugin_version
        )

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

    def _today(self):
        return date.today().strftime('%d/%m/%Y')

    def _diff_days(self):
        return (date.today() -
                datetime.strptime(
                    self.state['last_print'], '%d/%m/%Y').date()).days

    def _get_int(self, key):
        return self._settings.get_int([key])

    def _get_bool(self, key):
        return self._settings.get_boolean([key])

    def handle_gcode(self, comm_instance, phase, cmd, cmd_type,
                     gcode, *args, **kwargs):
        if gcode in ('G29', 'M420'):
            self._smartabl_logger.info(
                f"Settings(force_days={self._get_bool('force_days')}, "
                f"days={self._get_int('days')}, "
                f"force_prints={self._get_bool('force_prints')}, "
                f"prints={self._get_int('prints')}) || "
                f"State(first_time={self.state['first_time']}, "
                f"last_print={self.state['last_print']}, "
                f"prints={self.state['prints']})")
            if (self.state['first_time'] or
                (self._get_bool('force_days') and
                 self._diff_days() >= self._get_int('days')) or
                (self._get_bool('force_prints') and
                 self.state['prints'] >= self._get_int('prints'))):
                self._smartabl_logger.info("Forcing mesh update")
                if self.state['first_time']:
                    self.state['first_time'] = False
                self.state['prints'] = 0
                return ['G29', 'M500']
            else:
                self._smartabl_logger.info("Using saved mesh")
                return 'M420 S1'
        return cmd,

    def save_state(self):
        with open(f'{self.get_plugin_data_folder()}/state.json', 'w') as f:
            json.dump(self.state, f)

    def on_event(self, event, payload):
        events = (('PrintDone',) if not self._get_bool('failed')
                  else ('PrintDone', 'PrintFailed'))
        if event in events:
            self.state['prints'] += 1
            self.state['last_print'] = self._today()
            self.save_state()


def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__

    __plugin_implementation__ = SmartABLPlugin()
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": (
            __plugin_implementation__.get_update_information),
        "octoprint.comm.protocol.gcode.queuing": (
            __plugin_implementation__.handle_gcode)
    }
