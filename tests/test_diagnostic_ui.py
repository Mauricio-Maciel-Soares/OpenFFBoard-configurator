"""Exercise real Qt dialogs with a simulated serial device, without hardware."""
import unittest
from types import SimpleNamespace
from PyQt6.QtCore import QObject, pyqtSignal, QEventLoop, QTimer
from PyQt6.QtWidgets import QWidget
from base_ui import CommunicationHandler
from serial_comms import SerialComms
from advanced_axis_ui import AdvancedAxisDialog
from diagnostics_ui import DiagnosticsDialog
from test_reliability import Port, app


class Signals(QObject):
    connected = pyqtSignal(bool)


class DialogTests(unittest.TestCase):
    def setUp(self):
        SerialComms.callbackDict.clear()
        self.port = Port()
        self.comms = SerialComms(SimpleNamespace(log=lambda msg: None), self.port)
        self.port.flush_requests = self.comms._send_over_uart
        self.previous_comms = CommunicationHandler.comms
        CommunicationHandler.comms = self.comms
        self.parent = QWidget()
        self.signals = Signals()
        self.parent.main = SimpleNamespace(serialchooser=self.signals)
        self.dialogs = []
    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
        self.comms.reset()
        self.comms.request_timer.stop()
        CommunicationHandler.comms = self.previous_comms
        self.parent.deleteLater()
        app.processEvents()
    def test_old_firmware_hides_encvel_without_sending_unknown_command(self):
        dialog = AdvancedAxisDialog(self.parent, 0)
        self.dialogs.append(dialog)
        dialog.show()
        self.port.receive('[axis.0.help!|Command name,CMD ID, Description, Flags\nmaxtorquerate,0xB,limit, R W\n]')
        # Two callers share the cached class list once it has arrived; a queued
        # second help request may already exist and receives the same device list.
        self.port.receive('[axis.0.help!|maxtorquerate,0xB,limit, R W\n]')
        self.port.receive('[axis.0.maxtorquerate?|300]')
        self.assertTrue(dialog.encvel.isHidden())
        self.assertNotIn(b'encvel?', self.port.written)
        self.assertEqual(dialog.rate.value(), 300)
        self.assertTrue(dialog.apply_button.isEnabled())
    def test_full_uint32_setting_requires_real_readback(self):
        self.comms.capabilities[('axis', 0)] = {'maxtorquerate': {'flags': {'R', 'W'}}}
        dialog = AdvancedAxisDialog(self.parent, 0)
        self.dialogs.append(dialog)
        dialog.show()
        self.port.receive('[axis.0.maxtorquerate?|0]')
        dialog.rate.setValue(4294967295)
        dialog.apply_values()
        self.assertFalse(dialog.apply_button.isEnabled())
        self.port.receive('[axis.0.maxtorquerate=4294967295|OK]')
        self.assertNotIn('All changes confirmed', dialog.status.text())
        self.port.receive('[axis.0.maxtorquerate?|4294967295]')
        self.assertIn('All changes confirmed', dialog.status.text())
        self.assertTrue(dialog.apply_button.isEnabled())
    def test_missing_diag_uses_legacy_torque_and_never_writes_settings(self):
        self.comms.capabilities[('axis', 0)] = {'curtorque': {'flags': {'R'}}}
        self.comms.capabilities[('main', 0)] = {cmd: {'flags': {'R'}} for cmd in ('hidrate', 'cfrate')}
        dialog = DiagnosticsDialog(self.parent, 0)
        self.dialogs.append(dialog)
        dialog.show()
        dialog.start()
        self.port.receive('[axis.0.curtorque?|12]')
        self.port.receive('[main.0.hidrate?|324]')
        self.port.receive('[main.0.cfrate?|217]')
        self.assertEqual(len(dialog.rows), 1)
        self.assertEqual(dialog.rows[0]['axis_curtorque_counts'], 12)
        self.assertEqual(dialog.rows[0]['cfrate_hz'], 217)
        self.assertNotIn(b'=', self.port.written)
        self.assertNotIn(b'axis.0.diag?', self.port.written)
        self.signals.connected.emit(False)
        self.assertFalse(dialog.running)
        self.assertFalse(dialog.timer.isActive())
        self.assertEqual(dialog.rows[-1]['event'], 'disconnected')
        self.assertTrue(dialog.export_button.isEnabled())
    def test_dialog_close_cancels_pending_capture(self):
        self.comms.capabilities[('axis', 0)] = {'diag': {'flags': {'R'}}}
        dialog = DiagnosticsDialog(self.parent, 0)
        self.dialogs.append(dialog)
        dialog.show()
        dialog.start()
        dialog.close()
        before = bytes(self.port.written)
        self.port.receive('[axis.0.diag?|1,' + ','.join(['0'] * 18) + ']')
        self.assertEqual(bytes(self.port.written), before)
        self.assertFalse(dialog.rows)
