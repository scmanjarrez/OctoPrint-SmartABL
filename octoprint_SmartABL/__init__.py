# coding=utf-8
from __future__ import absolute_import

import json
import logging
import threading
from datetime import date, datetime

import octoprint.plugin


class SmartABLPlugin(
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.TemplatePlugin,
):
    fw_metadata = {
        "marlin": {
            "abl": "G29",
            "load": "M420 S1",
            "info": (
                "M420 V1",
                ["Invalid mesh"],
                ["Bilinear Leveling Grid", "Bed Topography Report"],
            ),
            "save": "M500",
        },
        "prusa": {
            "abl": "G80",
            "info": (
                "G81",
                ["Mesh bed leveling not active"],
                ["Measured points"],
            ),
        },
        "klipper": {
            "abl": "BED_MESH_CALIBRATE",
            "info": (
                "BED_MESH_OUTPUT",
                ["Bed has not been probed"],
                ["Mesh Leveling Probed Z positions"],
            ),
        },
    }
    temp = {"he": "M109", "bed": "M190"}

    def __init__(self):
        self.smart_logger = None
        self.state = None
        self.valid_mesh = False
        self.cache = set()
        self.force_temp = False
        self.firmware = None
        self.probe_required = False
        self.save_allowed = True
        self.last_cmd = None
        self.querying = False
        self.thread = None
        self.event = None

    # Plugin: Parent class
    def initialize(self):
        console_logging_handler = logging.handlers.RotatingFileHandler(
            self._settings.get_plugin_logfile_path(), maxBytes=2 * 1024 * 1024
        )
        console_logging_handler.setFormatter(
            logging.Formatter(
                f"%(asctime)s ({self._plugin_version}): %(message)s"
            )
        )
        console_logging_handler.setLevel(logging.DEBUG)
        self._smartabl_logger = logging.getLogger(
            f"octoprint.plugins.{self._identifier}"
        )
        self._smartabl_logger.addHandler(console_logging_handler)
        # some users don't enable it, so it's better to enable it by default...
        self._smartabl_logger.setLevel(logging.DEBUG)
        self._smartabl_logger.propagate = False

        try:
            with open(f"{self.get_plugin_data_folder()}/state.json") as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = dict(
                first_time=True, prints=0, last_mesh=self._today()
            )
        if "abl_always" not in self.state:
            self.state["abl_always"] = False
        if "last_bedtemp" not in self.state:
            self.state["last_bedtemp"] = 0
        if "last_hetemp" not in self.state:
            self.state["last_hetemp"] = 0
        self._save()
        self._smartabl_logger.debug(f"@initialize > {self._dbg()}")

    # AssetPlugin
    def get_assets(self):
        return dict(css=["css/SmartABL.css"], js=["js/SmartABL.js"])

    # SettingsPlugin
    def get_settings_defaults(self):
        return dict(
            trigger_custom=False,
            trigger_gcode="G29",
            abl_custom=False,
            abl_gcode="G29",
            cmd_ignore=False,
            ignore_gcode="",
            force_days=True,
            days=1,
            force_prints=True,
            prints=5,
            failed=False,
            bedtemp=False,
            hetemp=False,
        )

    # SimpleApiPlugin
    def get_api_commands(self):
        return dict(abl_always=["value"])

    def on_api_command(self, command, data):
        self.state["abl_always"] = data["value"]
        self._save()
        self._smartabl_logger.debug(
            f"@on_api_command:update_button > {self._dbgstate()}"
        )

    # TemplatePlugin
    def get_template_configs(self):
        return [dict(type="settings", custom_bindings=False)]

    def get_template_vars(self):
        return dict(version=self._plugin_version)

    # EventHandlerPlugin
    def on_event(self, event, payload):
        if event == "ClientOpened":
            self._plugin_manager.send_plugin_message(
                self._identifier, {"abl_always": self.state["abl_always"]}
            )
            self._update_frontend()
        elif event == "Disconnected":
            self.firmware = None
        if self.firmware is not None and event in (
            "PrintDone",
            "PrintFailed",
        ):
            self._smartabl_logger.debug(
                f"@on_event > Trigger(event={event}) || {self._dbg()}"
            )
            if event in self._events():
                self.state["prints"] += 1
            self._smartabl_logger.debug(
                f"@on_event:print_stop > {self._dbgstate()}"
            )
            self._update_frontend()
            self._save()

    # Hook: octoprint.comm.protocol.gcode.queuing
    def gcode_queuing(
        self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs
    ):
        if (
            self.firmware is not None
            and "tags" in kwargs
            and kwargs["tags"] is not None
            and "source:file" in kwargs["tags"]
        ):
            if self._get("cmd_ignore") and (
                gcode in self._gcodes_ignore() or cmd in self._gcodes_ignore()
            ):
                self._smartabl_logger.debug(
                    f"@gcode_queuing:ignore > "
                    f"Trigger(cmd={cmd}, gcode={gcode}) || "
                    f"{self._dbg()}"
                )
                return [None]
            elif gcode == "G28":
                self.cache = set()
            elif gcode in self._gcodes_abl() or cmd in self._gcodes_abl():
                self._smartabl_logger.debug(
                    f"@gcode_queuing:abl > "
                    f"Trigger(cmd={cmd}, gcode={gcode}) || "
                    f"{self._dbg()}"
                )
                self._printer.set_job_on_hold(True)
                if self.thread is None:
                    self.thread = threading.Thread(
                        target=self._unlock_queue, daemon=True
                    )
                    self.event = threading.Event()
                    self.thread.start()
                self.last_cmd = cmd
                cmd = ["@SMARTABLQUERY"]
                self._smartabl_logger.debug(
                    f"@gcode_queuing:abl_send >> Sending {cmd}"
                )
                return cmd
        return [cmd]

    # Hook: octoprint.comm.protocol.atcommand.sending
    def at_command(
        self, comm_instance, phase, cmd, parameters, tags=None, *args, **kwargs
    ):
        if cmd == "SMARTABLSAVE":
            if self.state["first_time"]:
                self.state["first_time"] = False
            self.state["prints"] = 0
            self.state["last_mesh"] = self._today()
            self._save()
            self._update_frontend()
            if self.save_allowed:
                cmds = self.fw_metadata[self.firmware]["save"]
                self._smartabl_logger.debug(
                    f"@at_command:save >> Sending {cmds} > "
                    f"{self._dbgstate()} || "
                    f"{self._dbginternal()}"
                )
                self._printer.commands(cmds)
            else:
                self._smartabl_logger.debug(
                    f"@at_command:save > {self._dbgstate()} || "
                    f"{self._dbginternal()}"
                )
        elif cmd == "SMARTABLQUERY":
            self.querying = True
            cmds = self.fw_metadata[self.firmware]["info"][0]
            self._smartabl_logger.debug(
                f"@at_command:query >> Mesh query(cmd={cmds}) > "
                f"{self._dbginternal()}"
            )
            self._printer.commands(cmds)
        elif cmd == "SMARTABLDECIDE":
            cmds = None
            if (
                self.state["abl_always"]
                or self.probe_required
                or self.force_temp
                or self.state["first_time"]
                or not self.valid_mesh
                or (
                    self._get("force_days")
                    and self._diff_days() >= self._get("days", "i")
                )
                or (
                    self._get("force_prints")
                    and self.state["prints"] >= self._get("prints", "i")
                )
            ):
                self.force_temp = False
                if "M420" in self.last_cmd:
                    cmds = [self.fw_metadata[self.firmware]["abl"]]
                else:
                    cmds = [self.last_cmd]
                if self._get("abl_custom"):
                    cmds = self._gcodes_custom()
                if self.save_allowed:
                    self.cache.add(self.fw_metadata[self.firmware]["save"])
                cmds.append("@SMARTABLSAVE")
                self._smartabl_logger.debug(
                    f"@at_command:decide >> ABL trigger >> Sending {cmds} > "
                    f"{self._dbg()}"
                )
                self.probe_required = False
            else:
                if self.save_allowed:
                    if self.last_cmd.startswith("M420 S1 Z"):
                        cmds = [self.last_cmd]
                    else:
                        cmds = [self.fw_metadata[self.firmware]["load"]]
                self._smartabl_logger.debug(
                    f"@at_command:decide >> ABL skip >> Sending {cmds} > "
                    f"{self._dbg()}"
                )
            if cmds is not None:
                self._printer.commands(cmds)
            self._printer.set_job_on_hold(False)
            self.querying = False
            if self.event is not None:
                self.event.set()
            self.thread = None
            self.event = None

    # Hook: octoprint.comm.protocol.gcode.received
    def process_line(self, comm_instance, line, *args, **kwargs):
        if self.firmware is None:
            if "FIRMWARE_NAME" in line:
                self._smartabl_logger.debug(
                    f"@process_line:firmware >> {line}"
                )
                idx = line.find("FIRMWARE_NAME")
                for fw_n in self.fw_metadata:
                    pattern = line[idx : idx + 40].lower()
                    if fw_n in pattern:
                        buddy = False
                        if "buddy" in pattern:
                            self.firmware = "marlin"
                            buddy = True
                        else:
                            self.firmware = fw_n
                        if self.firmware != "marlin" or buddy:
                            self.save_allowed = False
                            self.probe_required = True
                        if buddy:
                            self._smartabl_logger.debug(
                                f"@process_line:detected_firmware >> "
                                f"prusa-buddy > {self._dbginternal()}"
                            )
                        else:
                            self._smartabl_logger.debug(
                                f"@process_line:detected_firmware >> "
                                f"{fw_n} > {self._dbginternal()}"
                            )
                        break
                else:
                    self._smartabl_logger.debug(
                        "@process_line:detected_firmware >> Unknown"
                    )
                    self._plugin_manager.send_plugin_message(
                        self._identifier,
                        {
                            "abl_notify": (
                                "SmartABL: disabled",
                                "Unknown firmware. Open an Issue "
                                "on GitHub indicating your "
                                "firmware and logs.",
                            )
                        },
                    )
        else:
            if "EEPROM disabled" in line:  # marlin eeprom disabled
                self.save_allowed = False
            elif self._line_mesh(line) and self.querying:
                cmds = "@SMARTABLDECIDE"
                self.valid_mesh = self._valid_mesh(line)
                self._smartabl_logger.debug(
                    f"@process_line:"
                    f"{'' if self.valid_mesh else 'in'}valid_mesh >> "
                    f"Sending {cmds} > {self._dbginternal()}"
                )
                self._printer.commands(cmds)
            # elif "M420 S1.0 Z0.0" in line:
            #     self.valid_mesh = True
            #     self._smartabl_logger.debug(
            #         f"@process_line:VIRTUALPRINTER > {self._dbginternal()}"
            #     )
            #     self._printer.commands("@SMARTABLDECIDE")
        return line

    # Hook: octoprint.comm.protocol.gcode.sent
    def gcode_sent(
        self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs
    ):
        if (
            self.firmware is not None
            and "tags" in kwargs
            and kwargs["tags"] is not None
            and f"plugin:{self._identifier}" in kwargs["tags"]
            and "source:file" in kwargs["tags"]
            and gcode in self._gcodes_temp()
        ):
            self._smartabl_logger.debug(
                f"@gcode_sent:temp > Trigger(cmd={cmd}) || {self._dbg()}"
            )
            if gcode not in self.cache:
                self.cache.add(gcode)
                setting = "bedtemp"
                state = "last_bedtemp"
                if gcode == self.temp["he"]:
                    setting = "hetemp"
                    state = "last_hetemp"
                try:
                    temp = int(self.temp_regx.match(cmd).group(1))
                except AttributeError:
                    self._smartabl_logger.debug(
                        f"@gcode_sent:temp_error > Trigger(cmd={cmd}) || "
                        f"{self._dbg()}"
                    )
                else:
                    if self._get(setting) and temp != self.state[state]:
                        self.force_temp = True
                    self.state[state] = temp
                    self._save()
                    self._smartabl_logger.debug(
                        f"@gcode_sent:{setting} > "
                        f"{self._dbgstate()} || {self._dbginternal()}"
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
                "pip": (
                    "https://github.com/scmanjarrez/OctoPrint-SmartABL/"
                    "archive/{target_version}.zip"
                ),
            }
        }

    def _today(self):
        return date.today().strftime("%d/%m/%Y")

    def _diff_days(self):
        return (
            date.today()
            - datetime.strptime(self.state["last_mesh"], "%d/%m/%Y").date()
        ).days

    def _gcodes_abl(self):
        if self._get("trigger_custom"):
            return [
                gc.strip() for gc in self._get("trigger_gcode", "s").split(",")
            ]
        else:
            if self.firmware != "marlin":
                return [self.fw_metadata[self.firmware]["abl"]]
            else:
                return [
                    self.fw_metadata[self.firmware]["abl"],
                    self.fw_metadata[self.firmware]["load"].split()[0],
                ]

    def _gcodes_temp(self):
        return [self.temp[tmp] for tmp in self.temp]

    def _gcodes_custom(self):
        return [gc.strip() for gc in self._get("abl_gcode", "s").split(",")]

    def _gcodes_ignore(self):
        return [gc.strip() for gc in self._get("ignore_gcode", "s").split(",")]

    def _get(self, key, ktype="b"):
        if ktype == "i":
            return self._settings.get_int([key])
        elif ktype == "s":
            return self._settings.get([key])
        return self._settings.get_boolean([key])

    def _dbg(self):
        return (
            f"{self._dbgsettings()} || "
            f"{self._dbgstate()} || "
            f"{self._dbginternal()}"
        )

    def _dbgsettings(self):
        return (
            f"Settings("
            f"trigger_custom={self._get('trigger_custom')}, "
            f"trigger_gcode={self._get('trigger_gcode', 's')}, "
            f"abl_custom={self._get('abl_custom')}, "
            f"abl_gcode={self._get('abl_gcode', 's')}, "
            f"cmd_ignore={self._get('abl_custom')}, "
            f"ignore_gcode={self._get('ignore_gcode', 's')}, "
            f"force_days={self._get('force_days')}, "
            f"days={self._get('days', 'i')}, "
            f"force_prints={self._get('force_prints')}, "
            f"prints={self._get('prints', 'i')}, "
            f"failed={self._get('failed')}, "
            f"bedtemp={self._get('bedtemp')}, "
            f"hetemp={self._get('hetemp')}"
            f")"
        )

    def _dbgstate(self):
        return (
            f"State("
            f"first_time={self.state['first_time']}, "
            f"prints={self.state['prints']}, "
            f"last_mesh={self.state['last_mesh']}, "
            f"abl_always={self.state['abl_always']}, "
            f"last_bedtemp={self.state['last_bedtemp']}, "
            f"last_hetemp={self.state['last_hetemp']}"
            f")"
        )

    def _dbginternal(self):
        return (
            f"Internal("
            f"valid_mesh={self.valid_mesh}, "
            f"cache={self.cache}, "
            f"force_temp={self.force_temp}, "
            f"firmware={self.firmware}, "
            f"probe_required={self.probe_required}, "
            f"save_allowed={self.save_allowed}, "
            f"last_cmd={self.last_cmd}, "
            f"querying={self.querying}"
            f")"
        )

    def _events(self):
        return (
            ("PrintDone",)
            if not self._get("failed")
            else ("PrintDone", "PrintFailed")
        )

    def _save(self):
        with open(f"{self.get_plugin_data_folder()}/state.json", "w") as f:
            json.dump(self.state, f)

    def _update_frontend(self):
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"abl_counter": (self.state["prints"], self._get("prints", "i"))},
        )

    def _unlock_queue(self):
        if not self.event.wait(5):
            self._smartabl_logger.debug(
                "@unlock_queue >> Sending @SMARTABLDECIDE"
            )
            self._printer.commands("@SMARTABLDECIDE")

    def _line_mesh(self, line):
        for text in self.fw_metadata[self.firmware]["info"][1]:
            if text in line:
                return True
        for text in self.fw_metadata[self.firmware]["info"][2]:
            if text in line:
                return True
        return False

    def _valid_mesh(self, line):
        for text in self.fw_metadata[self.firmware]["info"][2]:
            if text in line:
                return True
        return False


__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_name__ = "SmartABL"


def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__

    __plugin_implementation__ = SmartABLPlugin()
    __plugin_hooks__ = {
        "octoprint.comm.protocol.atcommand.sending": (
            __plugin_implementation__.at_command
        ),
        "octoprint.comm.protocol.gcode.queuing": (
            __plugin_implementation__.gcode_queuing
        ),
        "octoprint.comm.protocol.gcode.received": (
            __plugin_implementation__.process_line
        ),
        "octoprint.comm.protocol.gcode.sent": (
            __plugin_implementation__.gcode_sent
        ),
        "octoprint.plugin.softwareupdate.check_config": (
            __plugin_implementation__.get_update_information
        ),
    }
