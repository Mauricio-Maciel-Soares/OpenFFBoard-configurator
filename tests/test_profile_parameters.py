"""Profile coverage and torque confirmation, using real schema and serial UI."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QWidget, QMessageBox
from base_ui import CommunicationHandler
from command_requests import CommandError
from profile_transfer import ProfileTransfer
from profile_ui import ProfileUI
from serial_comms import SerialComms
from test_reliability import Port, app


SETUP = json.loads((Path(__file__).resolve().parents[1] / "res/profile.cfg").read_text())
ACTIVE = "Axis:axis:0:2560:3\nODrive (M0):odrv:0:133:4"
MULTI_ACTIVE = ACTIVE + "\nAxis:axis:1:2560:5\nODrive (M1):odrv:1:134:6"


def axis_item(cmd="power", value="300", instance=0):
    return dict(fullname="Axis", cls="axis", instance=instance, cmd=cmd, value=value)


def torque_item(value="3000", instance=0):
    return dict(fullname="ODrive (M{})".format(instance), cls="odrv",
                instance=instance, cmd="maxtorque", value=value)


class Device:
    def __init__(self):
        self.pending, self.reads, self.writes = [], [], []

    def get_value_async(self, cls, cmd, callback, instance=0, on_error=None, **kwargs):
        self.reads.append((cls, instance, cmd, kwargs.get("require_write", False)))
        self.pending.append((cls, instance, cmd, callback, on_error))

    get_supported_value = get_value_async

    def set_value_verified(self, cls, cmd, value, callback, instance=0, on_error=None):
        self.writes.append((cls, instance, cmd, str(value)))
        self.pending.append((cls, instance, cmd, callback, on_error))

    def reply(self, value):
        self.pending.pop(0)[3](value)

    def fail(self, code):
        cls, instance, cmd, _, callback = self.pending.pop(0)
        callback(CommandError(code, "{}.{}.{}".format(cls, instance, cmd)))


class ProfileParameterTests(unittest.TestCase):
    def setUp(self):
        self.io = Device()
        self.done, self.failed, self.prompts = [], [], []
        self.transfer = ProfileTransfer(self.io, SETUP,
            lambda *args: self.done.append(args),
            lambda *args: self.failed.append(args),
            lambda changes, respond: self.prompts.append((changes, respond)))

    def preflight(self, items, values, active=ACTIVE):
        self.transfer.start(items)
        self.io.reply(active)
        for value in values:
            self.io.reply(value)

    def test_capture_real_five_field_classes_and_both_odrives(self):
        self.transfer.start()
        self.io.reply(MULTI_ACTIVE)
        while self.io.pending:
            cls, instance, cmd, _, _ = self.io.pending[0]
            values = {"maxtorquerate": "10922", "encvel": "1",
                      "filterProfile_id": "1", "maxtorque": str(3000 - instance * 1000)}
            self.io.reply(values.get(cmd, "0"))
        data, skipped = self.done[0]
        values = {(item["cls"], item["instance"], item["cmd"]): item["value"] for item in data}
        for instance in (0, 1):
            for cmd, expected in (("maxtorquerate", "10922"), ("encvel", "1"),
                                  ("filterProfile_id", "1")):
                self.assertEqual(values[("axis", instance, cmd)], expected)
            self.assertEqual(values[("odrv", instance, "maxtorque")], str(3000 - instance * 1000))
        self.assertFalse(skipped)
        self.assertFalse(self.io.writes)
        self.assertFalse(self.prompts)

    def test_legacy_three_field_classes_and_profile_do_not_invent_settings(self):
        self.preflight([axis_item()], ["100"], active="Axis:axis:0")
        self.assertEqual(self.io.writes, [("axis", 0, "power", "300")])
        self.io.reply("300")
        self.assertEqual(self.done[0][0], [axis_item()])
        self.assertFalse(self.prompts)

    def test_optional_fields_unavailable_are_skipped_on_capture(self):
        self.transfer.start()
        self.io.reply(ACTIVE)
        absent = {"maxtorquerate", "encvel", "filterProfile_id", "maxtorque"}
        while self.io.pending:
            cmd = self.io.pending[0][2]
            if cmd in absent:
                self.io.fail("NOT_FOUND")
            else:
                self.io.reply("0")
        self.assertEqual(set(self.done[0][1]),
                         {"axis.0.maxtorquerate", "axis.0.encvel",
                          "axis.0.filterProfile_id", "odrv.0.maxtorque"})
        self.assertFalse(self.io.writes)

    def test_increase_prompts_after_all_reads_before_any_writes_and_decline_aborts(self):
        self.preflight([axis_item(), torque_item(), axis_item("encvel", "1")],
                       ["100", "2000", "0"])
        self.assertFalse(self.io.pending)
        self.assertFalse(self.io.writes)
        changes, respond = self.prompts[0]
        self.assertEqual(changes, [dict(instance=0, current=2000, target=3000)])
        respond(False)
        respond(True)  # A late/duplicate response cannot undo a refusal.
        self.assertFalse(self.io.writes)
        self.assertEqual((self.failed[0][0].code, self.failed[0][1]), ("CANCELLED", 0))
        self.assertFalse(self.done)

    def test_accept_writes_once_and_only_reports_success_after_verification(self):
        items = [axis_item(), torque_item(), axis_item("filterProfile_id", "1")]
        self.preflight(items, ["100", "2000", "0"])
        respond = self.prompts[0][1]
        respond(True)
        respond(True)
        self.assertEqual(len(self.io.writes), 1)
        self.io.reply("300")
        self.assertFalse(self.done)
        self.io.reply("3000")
        self.assertFalse(self.done)
        self.io.reply("1")
        self.assertEqual(self.done[0][0], items)
        self.assertEqual(len(self.io.writes), 3)
        self.assertFalse(self.failed)

    def test_cancel_while_prompt_is_open_ignores_late_acceptance(self):
        self.preflight([torque_item()], ["2000"])
        self.transfer.cancel()
        self.prompts[0][1](True)
        self.assertFalse(self.io.writes)
        self.assertFalse(self.done)

    def test_no_confirmation_handler_cannot_apply_increase(self):
        self.transfer.confirm_torque_increase = None
        self.preflight([axis_item(), torque_item()], ["100", "2000"])
        self.assertFalse(self.io.writes)
        self.assertEqual(self.failed[0][0].code, "CONFIRMATION_REQUIRED")

    def test_equal_or_lower_torque_requires_no_prompt(self):
        for target in ("3000", "2000", "0"):
            with self.subTest(target=target):
                self.setUp()
                self.preflight([torque_item(target)], ["3000"])
                self.assertFalse(self.prompts)
                self.assertEqual(self.io.writes, [("odrv", 0, "maxtorque", target)])
                self.io.reply(target)
                self.assertEqual(len(self.done), 1)

    def test_all_increases_are_shown_together_including_start_from_zero(self):
        self.preflight([torque_item("3000"), torque_item("1500", 1)],
                       ["2000", "0"], active=MULTI_ACTIVE)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.prompts[0][0], [
            dict(instance=0, current=2000, target=3000),
            dict(instance=1, current=0, target=1500)])
        self.assertFalse(self.io.writes)

    def test_invalid_target_is_rejected_before_reading_or_writing_parameters(self):
        for target in ("-1", "4096", "nan", "3000;sys.0.reboot?", "30.0"):
            with self.subTest(target=target):
                self.setUp()
                self.transfer.start([axis_item(), torque_item(target)])
                self.io.reply(ACTIVE)
                self.assertFalse(self.io.pending)
                self.assertFalse(self.io.writes)
                self.assertEqual(self.failed[0][0].code, "INVALID_PROFILE")

    def test_unknown_current_torque_prevents_all_profile_writes(self):
        for current in ("-1", "4096", "nan", "20.0", "OK"):
            with self.subTest(current=current):
                self.setUp()
                self.preflight([axis_item(), torque_item()], ["100", current])
                self.assertFalse(self.io.writes)
                self.assertFalse(self.prompts)
                self.assertEqual(self.failed[0][0].code, "INVALID_REPLY")

    def test_unsupported_torque_is_reported_as_partial_without_writing_it(self):
        self.transfer.start([axis_item(), torque_item()])
        self.io.reply(ACTIVE)
        self.io.reply("100")
        self.io.fail("NOT_FOUND")
        self.io.reply("300")
        self.assertEqual(self.io.writes, [("axis", 0, "power", "300")])
        self.assertEqual(self.done[0][1], ["odrv.0.maxtorque"])
        self.assertFalse(self.prompts)

    def test_malformed_active_class_is_not_used_for_writes(self):
        for reply in ("Axis:axis:0:2560", "Axis:axis:256:2560:3",
                      "Axis:axis:0:invalid:3", ":axis:0:2560:3"):
            with self.subTest(reply=reply):
                self.setUp()
                self.transfer.start([axis_item()])
                self.io.reply(reply)
                self.assertFalse(self.io.writes)
                self.assertFalse(self.io.pending)
                self.assertEqual(self.failed[0][0].code, "INVALID_PROFILE")


class ProfileConfirmationUITests(unittest.TestCase):
    def setUp(self):
        SerialComms.callbackDict.clear()
        self.port = Port()
        self.comms = SerialComms(SimpleNamespace(log=lambda _: None), self.port)
        self.port.flush_requests = self.comms._send_over_uart
        self.previous = CommunicationHandler.comms
        CommunicationHandler.comms = self.comms
        self.main = QWidget()
        self.active = []
        self.main.systray = SimpleNamespace(refresh_profile_list=lambda _: None,
                                           refresh_profile_action_status=self.active.append)
        profiles = {"release": 2, "global": {}, "profiles": [
            {"name": "None", "data": []},
            {"name": "Track", "data": [axis_item(), torque_item()]}]}
        with patch.object(ProfileUI, "load_profiles",
                          lambda obj: setattr(obj, "profiles", copy.deepcopy(profiles))):
            self.ui = ProfileUI(self.main)
            self.ui.initialize_ui()
        self.ui.setEnabled(True)
        self.comms.capabilities[("odrv", 0)] = {"maxtorque": {"flags": {"R", "W"}}}
        self.active.clear()

    def tearDown(self):
        self.ui.onclose()
        self.comms.reset()
        self.comms.request_timer.stop()
        self.comms.write_timer.stop()
        self.main.deleteLater()
        app.processEvents()
        CommunicationHandler.comms = self.previous
        SerialComms.callbackDict.clear()

    def open_confirmation(self):
        self.ui.select_profile("Track")
        self.port.receive("[sys.0.lsactive?|" + ACTIVE + "]")
        self.port.receive("[axis.0.power?|100]")
        self.port.receive("[odrv.0.maxtorque?|2000]")
        self.assertIsNotNone(self.ui._torque_confirmation)
        self.assertNotIn(b"=", self.port.written)
        return self.ui._torque_confirmation

    def test_dialog_displays_nm_defaults_to_no_and_decline_writes_nothing(self):
        box = self.open_confirmation()
        self.assertIn("20.00 -> 30.00 Nm", box.text())
        self.assertEqual(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
        box.button(QMessageBox.StandardButton.No).click()
        self.assertIsNone(self.ui._transfer)
        self.assertIsNone(self.ui._torque_confirmation)
        self.assertNotIn(b"=", self.port.written)
        self.assertNotIn("Track", self.active)
        self.assertTrue(self.ui.comboBox_profiles.isEnabled())

    def test_acceptance_still_needs_set_ack_and_readback_for_every_parameter(self):
        box = self.open_confirmation()
        box.button(QMessageBox.StandardButton.Yes).click()
        self.port.flush_requests()
        self.assertIn(b"axis.0.power=300;", self.port.written)
        self.assertNotIn(b"odrv.0.maxtorque=3000;", self.port.written)
        self.port.receive("[axis.0.power=300|OK]")
        self.assertNotIn(b"odrv.0.maxtorque=3000;", self.port.written)
        self.port.receive("[axis.0.power?|300]")
        self.port.flush_requests()
        self.assertIn(b"odrv.0.maxtorque=3000;", self.port.written)
        self.port.receive("[odrv.0.maxtorque=3000|OK]")
        self.assertIsNotNone(self.ui._transfer)
        self.port.receive("[odrv.0.maxtorque?|3000]")
        self.assertIsNone(self.ui._transfer)
        self.assertEqual(self.active[-1], "Track")
        self.assertNotIn(b"sys.0.save", self.port.written)

    def test_disconnect_closes_confirmation_and_does_not_write_on_reconnect(self):
        self.open_confirmation()
        self.ui.setEnabled(False)  # main.serialchooser.connected uses this slot.
        self.assertIsNone(self.ui._torque_confirmation)
        self.assertIsNone(self.ui._transfer)
        self.ui.setEnabled(True)
        self.port.flush_requests()
        self.assertNotIn(b"=", self.port.written)
        self.assertNotIn("Track", self.active)


if __name__ == "__main__":
    unittest.main()
