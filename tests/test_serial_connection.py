"""Connection lifecycle regressions, including Windows-style cancelled I/O.

Use the real Qt chooser, timers, request queue and MainUi cleanup with a serial
double: no physical board, Windows driver or torque commands are needed.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import pyqtSignal, QTimer
from PyQt6.QtSerialPort import QSerialPort
from PyQt6.QtWidgets import QWidget

from base_ui import CommunicationHandler
from main import MainUi
from serial_comms import SerialComms
from serial_ui import SerialChooser
from test_reliability import Port, app


Errors = QSerialPort.SerialPortError
ABORTED = "A operação de E/S foi anulada devido a uma saída de thread ou a uma requisição de aplicativo."


class PortInfo:
    def __init__(self, name="COM7", serial="board-a"):
        self.name, self.serial = name, serial

    def portName(self):
        return self.name

    def systemLocation(self):
        return self.name

    def serialNumber(self):
        return self.serial

    def vendorIdentifier(self):
        return 0x1209

    def productIdentifier(self):
        return 0xFFB0

    def description(self):
        return "OpenFFBoard"


class SerialPort(Port):
    errorOccurred = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.opened = False
        self.info = PortInfo()
        self.outcomes = []
        self.open_calls = []
        self.close_calls = 0
        self.current_error = Errors.NoError
        self.dtr_error = Errors.NoError
        self.pending_bytes = 0
        self.clear = Mock(side_effect=AssertionError("must not abort pending OS I/O"))
        self.flush = Mock(return_value=True)
        self.setBaudRate = Mock(return_value=True)
        self.setDataBits = Mock(return_value=True)
        self.setParity = Mock(return_value=True)
        self.setStopBits = Mock(return_value=True)
        self.setFlowControl = Mock(return_value=True)

    def setPort(self, info):
        self.info = info

    def portName(self):
        return self.info.portName()

    def open(self, mode):
        self.open_calls.append(self.portName())
        error = self.outcomes.pop(0) if self.outcomes else Errors.NoError
        self.opened = error == Errors.NoError
        # Qt also emits NoError on a successful open (historical behavior).
        self.fail(error)
        return self.opened

    def setDataTerminalReady(self, enabled):
        if self.dtr_error != Errors.NoError:
            self.fail(self.dtr_error)
            return False
        return True

    def error(self):
        return self.current_error

    def errorString(self):
        return ABORTED if self.current_error == Errors.ResourceError else self.current_error.name

    def fail(self, error):
        self.current_error = error
        self.errorOccurred.emit(error)

    def bytesToWrite(self):
        return self.pending_bytes

    def close(self):
        self.close_calls += 1
        super().close()
        self.current_error = Errors.NoError


class Main(QWidget):
    reset_port = MainUi.reset_port
    cancel_pending_port_close = MainUi.cancel_pending_port_close
    comms_reset = CommunicationHandler.comms_reset

    def __init__(self, port):
        super().__init__()
        self.serial = port
        self._port_reset_generation = 0
        self._resetting_port = False
        self._shutdown_done = False
        self.messages = []
        self.profile_ui = SimpleNamespace(
            get_global_setting=lambda key, default: default,
            set_global_setting=Mock(), setEnabled=Mock())
        self.process_events_timer = QTimer(self)
        self.serial_timer = QTimer(self)
        self.reset_tabs = Mock()
        self.timeouting = False
        self.comms = SerialComms(self, port)

    def log(self, message):
        self.messages.append(message)


class SerialConnectionTests(unittest.TestCase):
    def setUp(self):
        SerialComms.callbackDict.clear()
        self.port = SerialPort()
        self.main = Main(self.port)
        self.old_comms = CommunicationHandler.comms
        CommunicationHandler.comms = self.main.comms
        self.chooser = SerialChooser(self.port, self.main)
        self.main.serialchooser = self.chooser
        self.states = []
        self.chooser.connected.connect(self.states.append)
        self.enumerate = patch("serial_ui.PyQt6.QtSerialPort.QSerialPortInfo.availablePorts",
                               return_value=[PortInfo()]).start()
        self.addCleanup(patch.stopall)
        self.chooser.get_ports()
        self.states.clear()

    def tearDown(self):
        self.chooser.cancel_reconnect()
        self.main.reset_port(immediate=True)
        app.processEvents()
        self.main.comms.request_timer.stop()
        self.chooser.remove_callbacks()
        SerialComms.callbackDict.clear()
        CommunicationHandler.comms = self.old_comms
        self.main.deleteLater()

    def retry_open(self):
        self.assertTrue(self.chooser.open_retry_timer.isActive())
        self.chooser.open_retry_timer.start(0)
        app.processEvents()

    def test_cancelled_open_retries_same_board_at_new_com_and_never_purges(self):
        self.port.outcomes = [Errors.ResourceError, Errors.NoError]
        self.chooser.serial_connect_button()
        self.assertFalse(self.port.isOpen())
        self.assertFalse(self.states)
        self.assertFalse(self.port.written)
        self.assertFalse(self.chooser.comboBox_port.isEnabled())
        detail = "COM7 during open: ResourceError ({}): ".format(Errors.ResourceError.value) + ABORTED
        self.assertTrue(any(detail in msg
                            for msg in self.main.messages))
        self.enumerate.return_value = [PortInfo("COM8")]
        self.retry_open()
        self.assertEqual(self.port.open_calls, ["COM7", "COM8"])
        self.assertEqual(self.states, [True])
        self.assertTrue(self.port.isOpen())
        self.assertFalse(self.chooser.open_retry_timer.isActive())
        self.port.clear.assert_not_called()
        self.port.setFlowControl.assert_called_with(QSerialPort.FlowControl.NoFlowControl)
        self.assertNotIn(b"=", self.port.written)

    def test_cancelled_open_stops_after_three_attempts(self):
        self.port.outcomes = [Errors.ResourceError] * 3
        self.chooser.serial_connect_button()
        self.retry_open()
        self.retry_open()
        self.assertEqual(len(self.port.open_calls), 3)
        self.assertFalse(self.chooser.open_retry_timer.isActive())
        self.assertFalse(self.states)
        self.assertTrue(self.chooser.comboBox_port.isEnabled())
        self.assertEqual(self.chooser.pushButton_connect.text(), "Connect")

    def test_permission_and_missing_port_fail_without_retries(self):
        for error in (Errors.PermissionError, Errors.DeviceNotFoundError):
            with self.subTest(error=error):
                self.port.outcomes = [error]
                self.chooser.serial_connect_button()
                self.assertFalse(self.chooser.open_retry_timer.isActive())
                self.assertFalse(self.port.isOpen())
                self.assertFalse(self.states)

    def test_cancel_button_prevents_pending_open(self):
        self.port.outcomes = [Errors.ResourceError]
        self.chooser.serial_connect_button()
        self.chooser.serial_connect_button()
        self.chooser._retry_open()  # Even an already delivered timer must not reconnect.
        self.assertEqual(self.port.open_calls, ["COM7"])
        self.assertFalse(self.chooser.open_retry_timer.isActive())
        self.assertFalse(self.port.written)

    def test_missing_different_or_ambiguous_device_is_never_opened(self):
        for ports in ([], [PortInfo(serial="board-b")], [PortInfo(), PortInfo("COM8")]):
            with self.subTest(ports=ports):
                self.enumerate.return_value = [PortInfo()]
                self.chooser.get_ports()
                self.port.outcomes = [Errors.ResourceError]
                self.chooser.serial_connect_button()
                calls = len(self.port.open_calls)
                self.enumerate.return_value = ports
                self.retry_open()
                self.assertEqual(len(self.port.open_calls), calls)
                self.assertFalse(self.chooser.open_retry_timer.isActive())

    def test_dtr_failure_closes_handle_without_claiming_connection(self):
        self.port.dtr_error = Errors.ResourceError
        self.chooser.serial_connect_button()
        self.assertEqual(self.port.close_calls, 1)
        self.assertFalse(self.port.isOpen())
        self.assertFalse(self.states)
        self.assertFalse(self.port.written)
        self.assertTrue(any("during DTR: ResourceError" in msg for msg in self.main.messages))
        # close() resets the fake error; the log must retain the original failure.
        self.assertTrue(any(ABORTED in msg for msg in self.main.messages))

    def test_configuration_failure_does_not_open_handle(self):
        def unsupported(value):
            self.port.fail(Errors.UnsupportedOperationError)
            return False
        self.port.setFlowControl.side_effect = unsupported
        self.chooser.serial_connect_button()
        self.assertFalse(self.port.open_calls)
        self.assertFalse(self.chooser.open_retry_timer.isActive())
        self.assertTrue(any("during configure" in msg for msg in self.main.messages))

    def test_runtime_error_defers_close_until_serial_callback_returns(self):
        self.chooser.serial_connect_button()
        self.port.fail(Errors.ResourceError)
        self.assertTrue(self.port.isOpen())
        self.assertEqual(self.port.close_calls, 0)
        app.processEvents()
        self.assertFalse(self.port.isOpen())
        self.assertEqual(self.port.close_calls, 1)
        self.assertEqual(self.states, [True, False])

    def test_error_inside_write_does_not_reset_queues_reentrantly(self):
        self.chooser.serial_connect_button()
        self.main.comms.reset()
        write = self.port.write
        def error_during_write(data):
            self.port.fail(Errors.ResourceError)
            return write(data)
        self.port.write = error_during_write
        self.main.comms.getValueAsync(self, "sys", "swver", lambda reply: None,
                                     on_error=lambda error: None)
        self.assertTrue(self.port.isOpen())
        app.processEvents()
        self.assertFalse(self.port.isOpen())
        self.assertFalse(self.main.comms.send_buffer)
        self.assertFalse(self.main.comms.requests.queues)

    def test_stale_deferred_error_cannot_close_new_connection(self):
        self.chooser.serial_connect_button()
        callbacks = []
        with patch("serial_ui.PyQt6.QtCore.QTimer.singleShot", side_effect=lambda ms, cb: callbacks.append(cb)):
            self.port.fail(Errors.ResourceError)
        self.main.reset_port(immediate=True)
        self.chooser.serial_connect_button()
        for callback in callbacks:
            callback()
        self.assertTrue(self.port.isOpen())
        self.assertEqual(self.port.close_calls, 1)

    def test_immediate_close_does_not_flush_failed_handle(self):
        self.chooser.serial_connect_button()
        self.port.pending_bytes = 25
        self.main.reset_port(immediate=True)
        self.port.flush.assert_not_called()
        self.assertFalse(self.port.isOpen())

    def test_old_delayed_cleanup_cannot_close_new_connection(self):
        self.chooser.serial_connect_button()
        self.port.pending_bytes = 25
        callbacks = []
        with patch("main.PyQt6.QtCore.QTimer.singleShot", side_effect=lambda ms, cb: callbacks.append(cb)):
            self.main.reset_port()
        self.port.flush.assert_called_once()
        self.main.reset_port(immediate=True)
        self.chooser.serial_connect_button()
        for callback in callbacks:
            callback()
        self.assertTrue(self.port.isOpen())
        self.assertEqual(self.port.close_calls, 1)

    def test_reconnect_uses_one_enumeration_without_nested_open_retries(self):
        self.chooser.auto_reconnect.setChecked(True)
        self.chooser.serial_connect_button()
        self.main.reset_port(immediate=True)
        self.assertTrue(self.chooser.reconnect_timer.isActive())
        self.port.outcomes = [Errors.ResourceError]
        self.enumerate.reset_mock()
        self.chooser.reconnect_timer.start(0)
        app.processEvents()
        self.enumerate.assert_called_once_with()
        self.assertFalse(self.chooser.open_retry_timer.isActive())
        self.assertTrue(self.chooser.reconnect_timer.isActive())
        self.assertEqual(len(self.port.open_calls), 2)


if __name__ == "__main__":
    unittest.main()
