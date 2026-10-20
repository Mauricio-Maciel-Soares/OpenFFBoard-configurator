"""Behavioral regressions for serial transactions, persistence and profiles."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QObject, pyqtSignal, QByteArray, QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication
from serial_comms import SerialComms
from command_requests import RequestQueue, CommandError
from profile_transfer import ProfileTransfer
from profile_store import load_profiles, save_profiles
from command_capabilities import parse_capabilities
from connection_identity import same_device, port_identity
from diagnostics import decode_axis_errors, parse_payload, AXIS_FIELDS, loop_rates, export_csv
from helper import throttle

app = QApplication.instance() or QApplication([])


class Port(QObject):
    readyRead = pyqtSignal()
    aboutToClose = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.opened = True
        self.data = b''
        self.written = bytearray()
        self.accept = None
    def isOpen(self):
        return self.opened
    def write(self, data):
        size = len(data) if self.accept is None else min(self.accept, len(data))
        if size > 0:
            self.written.extend(data[:size])
        return size
    def readAll(self):
        data, self.data = self.data, b''
        return QByteArray(data)
    def errorString(self):
        return 'test write failure'
    def receive(self, text):
        # Simulated device responses arrive after it has received pending requests.
        if hasattr(self, 'flush_requests'):
            self.flush_requests()
        self.data += text.encode()
        self.readyRead.emit()
    def close(self):
        self.aboutToClose.emit()
        self.opened = False


class TransportTests(unittest.TestCase):
    def setUp(self):
        SerialComms.callbackDict.clear()
        self.messages = []
        self.port = Port()
        self.comms = SerialComms(self, self.port)
        self.port.flush_requests = self.comms._send_over_uart
        self.results, self.errors = [], []
    def log(self, msg):
        self.messages.append(str(msg))
    def tearDown(self):
        self.comms.reset()
        self.comms.request_timer.stop()
        SerialComms.callbackDict.clear()
    def test_pack_bytes_and_terminators(self):
        commands = ['a' * 510 + ';', 'é' * 256 + ';', 'sys.0.id?;']
        self.comms.pack_cmd(''.join(commands))
        self.assertEqual(b''.join(self.comms.send_buffer), ''.join(commands).encode())
        self.assertTrue(all(0 < len(p) <= 1024 and p.endswith(b';') for p in self.comms.send_buffer))
    def test_oversized_single_command_does_not_mutate_queue(self):
        self.comms.pack_cmd('sys.0.id?;')
        old = list(self.comms.send_buffer)
        with self.assertRaises(ValueError):
            self.comms.pack_cmd('é' * 512 + ';')
        self.assertEqual(old, self.comms.send_buffer)
    def test_address_zero(self):
        self.comms.sendCommand('axis', 'cmdinfo', adr=0)
        self.assertEqual(self.port.written, b'axis.0.cmdinfo?0;')
    def test_partial_and_zero_write_resume_without_duplicate_bytes(self):
        self.port.accept = 0
        self.comms.serialWriteRaw('sys.0.id?\naxis.0.degrees?;')
        self.assertFalse(self.port.written)
        self.port.accept = 3
        while self.comms.send_buffer:
            self.comms._send_over_uart()
        self.assertEqual(self.port.written, b'sys.0.id?;axis.0.degrees?;')
    def test_error_removes_request_and_legacy_oneshot(self):
        self.comms.getValueAsync(self, 'axis', 'encvel', self.results.append, on_error=self.errors.append)
        self.comms.registerCallback('legacy', 'axis', 'encvel', self.results.append, delete=True)
        self.port.receive('[axis.0.encvel?|NOT_FOUND]')
        self.assertEqual([e.code for e in self.errors], ['NOT_FOUND'])
        self.assertFalse(self.comms.requests.queues)
        self.assertFalse(SerialComms.callbackDict['axis'])
        self.assertFalse(self.results)
    def test_set_ack_then_readback_no_optimistic_success(self):
        self.comms.setValueVerified(self, 'axis', 'maxtorquerate', 4294967295, self.results.append, self.errors.append)
        self.port.receive('[axis.0.maxtorquerate=4294967295|OK]')
        self.assertFalse(self.results)
        self.comms._send_over_uart()
        self.assertTrue(self.port.written.endswith(b'axis.0.maxtorquerate?;'))
        self.port.receive('[axis.0.maxtorquerate?|4294967295]')
        self.assertEqual(self.results, ['4294967295'])
        self.assertFalse(self.errors)
    def test_wrong_ack_value_cannot_confirm_new_setting(self):
        self.comms.setValueVerified(self, 'axis', 'power', 400, self.results.append, self.errors.append)
        self.port.receive('[axis.0.power=300|OK]')
        self.assertFalse(self.results)
        self.assertNotIn(b'power?;', self.port.written)
        self.port.receive('[axis.0.power=400|ERR]')
        self.assertEqual(self.errors[0].code, 'ERR')
    def test_readback_mismatch_fails(self):
        self.comms.setValueVerified(self, 'axis', 'power', 400, self.results.append, self.errors.append)
        self.port.receive('[axis.0.power=400|OK]')
        self.port.receive('[axis.0.power?|300]')
        self.assertFalse(self.results)
        self.assertEqual(self.errors[0].code, 'VERIFY_FAILED')
    def test_negative_ack_and_address_zero(self):
        self.comms.sendValue(self, 'test', 'value', -30, adr=0, callback=self.results.append)
        self.port.receive('[test.0.value=-30?0|OK]')
        self.assertEqual(self.results, ['OK'])
    def test_unaddressed_reply_does_not_hit_addressed_callback(self):
        self.comms.registerCallback(self, 'axis', 'cmdinfo', self.results.append, adr=0, delete=True)
        self.port.receive('[axis.0.cmdinfo?|1]')
        self.assertFalse(self.results)
    def test_disconnect_fails_pending_and_never_replays(self):
        self.port.accept = 0
        self.comms.sendValue(self, 'axis', 'power', 400, on_error=self.errors.append)
        self.port.close()
        self.port.opened = True
        self.port.accept = None
        self.comms._send_over_uart()
        self.assertFalse(self.port.written)
        self.assertFalse(self.comms.requests.queues)
        self.assertEqual(self.errors[0].code, 'DISCONNECTED')
    def test_invalid_conversion_is_not_dispatched(self):
        self.comms.getValueAsync(self, 'axis', 'power', self.results.append, conversion=int, on_error=self.errors.append)
        self.port.receive('[axis.0.power?|not an int]')
        self.assertFalse(self.results)
        self.assertEqual(self.errors[0].code, 'INVALID_REPLY')
    def test_write_failure_clears_queue(self):
        self.port.accept = -1
        self.comms.sendValue(self, 'axis', 'power', 400, on_error=self.errors.append)
        self.assertFalse(self.comms.send_buffer)
        self.assertFalse(self.comms.requests.queues)
        self.assertEqual(len(self.errors), 1)
    def test_readback_before_read_transmission_cannot_confirm_write(self):
        self.comms.setValueVerified(self, 'axis', 'power', 400, self.results.append, self.errors.append)
        self.port.receive('[axis.0.power=400|OK]')
        del self.port.flush_requests
        self.port.receive('[axis.0.power?|400]')
        self.assertFalse(self.results)
        self.comms._send_over_uart()
        self.port.receive('[axis.0.power?|400]')
        self.assertEqual(self.results, ['400'])
    def test_partial_packet_dispatches_completed_command_before_later_commands(self):
        self.comms.write_timer.start(1000)
        self.comms.getValueAsync(self, 'axis', 'power', self.results.append)
        self.comms.getValueAsync(self, 'axis', 'degrees', self.results.append)
        self.port.accept = len(b'axis.0.power?;')
        self.comms._send_over_uart()
        del self.port.flush_requests
        self.port.receive('[axis.0.power?|400]')
        self.assertEqual(self.results, ['400'])
        self.assertTrue(self.comms.send_buffer)
    def test_stalled_output_is_discarded_at_request_deadline(self):
        self.port.accept = 0
        self.comms.sendValue(self, 'axis', 'power', 400, on_error=self.errors.append)
        self.comms.requests.queues[('axis', 0, 'power', '=', None)][0]['deadline'] = 0
        self.comms._check_requests()
        self.assertFalse(self.port.isOpen())
        self.assertFalse(self.comms.send_buffer)
        self.assertFalse(self.port.written)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.sent, self.errors, self.results = [], [], []
        self.queue = RequestQueue(self.sent.append, self.errors.append, lambda: self.now)
        self.key = ('axis', 0, 'power', '?', None)
    def add(self, handler=None):
        self.queue.add(handler or self, self.key, 'axis.0.power?;', self.results.append, on_error=self.errors.append, timeout=2)
    def test_same_key_is_serialized(self):
        self.add(); self.add()
        self.assertEqual(len(self.sent), 1)
        self.queue.reply(self.key, '1')
        self.assertEqual(len(self.sent), 2)
        self.queue.reply(self.key, '2')
        self.assertEqual(self.results, ['1', '2'])
    def test_timeout_late_response_cannot_satisfy_new_request(self):
        self.add()
        self.now = 2.1
        self.queue.expire()
        self.add()
        self.assertEqual([e.code for e in self.errors], ['TIMEOUT', 'UNSYNCHRONIZED'])
        self.assertEqual(len(self.sent), 1)
        self.queue.reply(self.key, 'old')
        self.assertFalse(self.results)
        self.add()
        self.queue.reply(self.key, 'new')
        self.assertEqual(self.results, ['new'])
    def test_cancel_does_not_run_next_write(self):
        self.add(); self.add()
        self.queue.cancel(self)
        self.assertEqual(len(self.sent), 1)
        self.queue.reply(self.key, 'old')
        self.assertFalse(self.results)
    def test_nested_reply_does_not_double_call_same_request(self):
        def callback(value):
            self.results.append(value)
            self.queue.reply(self.key, 'duplicate')
        self.queue.add(self, self.key, 'axis.0.power?;', callback)
        self.queue.reply(self.key, 'first')
        self.assertEqual(self.results, ['first'])
    def test_nested_reply_does_not_satisfy_queued_unsent_request(self):
        def callback(value):
            self.results.append(value)
            self.queue.reply(self.key, 'duplicate')
        self.queue.add(self, self.key, 'axis.0.power?;', callback)
        self.add()
        self.queue.reply(self.key, 'first')
        self.assertEqual(self.results, ['first'])
        self.queue.reply(self.key, 'second')
        self.assertEqual(self.results, ['first', 'second'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'profiles.json'
        self.data = {'release': 2, 'global': {}, 'profiles': [{'name': 'None', 'data': []}]}
    def tearDown(self):
        self.tmp.cleanup()
    def test_corrupt_file_recovers_valid_backup_and_preserves_damage(self):
        save_profiles(self.path, self.data)
        newer = dict(self.data, global_setting='new')
        save_profiles(self.path, newer)
        self.path.write_text('{truncated')
        recovered, used_backup = load_profiles(self.path)
        self.assertTrue(used_backup)
        self.assertEqual(recovered, self.data)
        self.assertEqual(Path(str(self.path) + '.corrupt').read_text(), '{truncated')
    def test_failed_replace_preserves_original(self):
        save_profiles(self.path, self.data)
        original = self.path.read_bytes()
        def replace(source, destination):
            if str(destination) == str(self.path):
                raise OSError('disk full')
            os.rename(source, destination)
        with patch('profile_store.os.replace', side_effect=replace):
            with self.assertRaises(OSError):
                save_profiles(self.path, dict(self.data, modified=True))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))
    def test_invalid_original_does_not_destroy_backup(self):
        save_profiles(self.path, self.data)
        save_profiles(self.path, self.data)
        backup = Path(str(self.path) + '.bak').read_bytes()
        self.path.write_text('{}')
        with self.assertRaises(ValueError):
            save_profiles(self.path, self.data)
        self.assertEqual(Path(str(self.path) + '.bak').read_bytes(), backup)
    def test_unrecoverable_file_is_preserved(self):
        self.path.write_text('{')
        with self.assertRaises(ValueError):
            load_profiles(self.path)
        self.assertEqual(self.path.read_text(), '{')


class ProfileIO:
    def __init__(self):
        self.pending = []
        self.writes = []
    def get_value_async(self, cls, cmd, callback, instance=0, on_error=None, **kwargs):
        self.pending.append((cmd, callback, on_error))
    get_supported_value = get_value_async
    def set_value_verified(self, cls, cmd, value, callback, on_error=None, instance=0):
        self.writes.append((cmd, value))
        self.pending.append((cmd, callback, on_error))
    def reply(self, value):
        self.pending.pop(0)[1](value)
    def fail(self, code):
        cmd, _, error = self.pending.pop(0)
        error(CommandError(code, cmd))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.io = ProfileIO()
        self.done, self.failed = [], []
        setup = {'callOrder': [{'fullname': 'Axis', 'classname': 'axis', 'key': ['power', 'encvel'], 'optional': ['encvel']}]}
        self.transfer = ProfileTransfer(self.io, setup, lambda *args: self.done.append(args), lambda *args: self.failed.append(args))
        self.items = [dict(fullname='Axis', cls='axis', instance=0, cmd='power', value='300'),
                      dict(fullname='Axis', cls='axis', instance=0, cmd='encvel', value='1')]
    def test_preflight_then_verified_writes_and_only_then_success(self):
        self.transfer.start(self.items)
        self.io.reply('Axis:axis:0\n')
        self.io.reply('100'); self.io.reply('0')
        self.assertEqual(self.io.writes, [('power', '300')])
        self.assertFalse(self.done)
        self.io.reply('300')
        self.assertEqual(self.io.writes[-1], ('encvel', '1'))
        self.assertFalse(self.done)
        self.io.reply('1')
        self.assertEqual(len(self.done), 1)
    def test_preflight_failure_writes_nothing(self):
        self.transfer.start(self.items)
        self.io.reply('Axis:axis:0')
        self.io.fail('TIMEOUT')
        self.assertFalse(self.io.writes)
        self.assertEqual(self.failed[0][1], 0)
    def test_readback_failure_stops_remaining_settings(self):
        self.transfer.start(self.items)
        self.io.reply('Axis:axis:0'); self.io.reply('100'); self.io.reply('0')
        self.io.fail('VERIFY_FAILED')
        self.assertEqual(len(self.io.writes), 1)
        self.assertFalse(self.done)
    def test_unsupported_optional_capture_is_omitted(self):
        self.transfer.start()
        self.io.reply('Axis:axis:0'); self.io.reply('300'); self.io.fail('NOT_FOUND')
        items, skipped = self.done[0]
        self.assertEqual([v['cmd'] for v in items], ['power'])
        self.assertEqual(skipped, ['axis.0.encvel'])
    def test_unsupported_optional_apply_is_reported(self):
        self.transfer.start(self.items)
        self.io.reply('Axis:axis:0'); self.io.reply('300'); self.io.fail('NOT_FOUND')
        self.io.reply('300')
        self.assertEqual(self.done[0][1], ['axis.0.encvel'])
    def test_cancel_prevents_followup_commands(self):
        self.transfer.start(self.items)
        self.transfer.cancel()
        self.io.reply('Axis:axis:0')
        self.assertFalse(self.io.pending)
        self.assertFalse(self.done)
    def test_injected_value_or_inactive_axis_is_rejected(self):
        self.items[0]['value'] = '300;sys.reboot'
        self.transfer.start(self.items)
        self.io.reply('Axis:axis:0')
        self.assertFalse(self.io.writes)
        self.assertEqual(self.failed[0][0].code, 'INVALID_PROFILE')


class DiagnosticsTests(unittest.TestCase):
    def test_errors_are_version_specific_and_preserve_unknown_bits(self):
        self.assertIn('MOTOR_FAILED', decode_axis_errors(0x40, '0.5.6'))
        self.assertNotIn('MOTOR_FAILED', decode_axis_errors(0x40))
        self.assertIn('UNKNOWN_BITS 0x00000002', decode_axis_errors(0x42, '0.5.6'))
        self.assertIn('WATCHDOG_TIMER_EXPIRED', decode_axis_errors(0x840, '0.5.6'))
    def test_invalid_diagnostic_schema_and_nonfinite_values(self):
        for payload in ('2,' + ','.join(['0'] * 18), '1,2', '1,nan,' + ','.join(['0'] * 17)):
            with self.assertRaises(ValueError):
                parse_payload(payload, AXIS_FIELDS)
    def test_counter_rollover_and_reboot(self):
        prev = dict(time_us=0xfffffff0, sequence=0xfffffffe, clip_count=0, slew_count=0)
        current = dict(time_us=1984, sequence=0, clip_count=1, slew_count=0)
        self.assertEqual(loop_rates(prev, current)['loop_hz'], 1000)
        self.assertNotIn('loop_hz', loop_rates(dict(prev, sequence=10000), current))
    def test_csv_contains_all_fields_and_missing_values_stay_empty(self):
        text = export_csv([{'host_end_s': 1, 'axis_dt_us': 1000}, {'host_end_s': 2, 'event': 'disconnected'}]).decode('utf-8-sig')
        self.assertIn('host_end_s,axis_dt_us,event', text)
        self.assertIn('2,,disconnected', text)
    def test_capability_list_including_commas_in_description(self):
        data = parse_capabilities('Command name,CMD ID, Description, Flags\nmaxtorquerate,0xB,counts/ms, 0 disables, R W\n')
        self.assertEqual(data['maxtorquerate']['flags'], {'R', 'W'})
        self.assertNotIn('encvel', data)


class Device:
    def __init__(self, serial, path, vid=0x1209, pid=0xffb0):
        self.values = vid, pid, serial, path
    def vendorIdentifier(self): return self.values[0]
    def productIdentifier(self): return self.values[1]
    def serialNumber(self): return self.values[2]
    def systemLocation(self): return self.values[3]


class ConnectionTests(unittest.TestCase):
    def test_main_classes_are_requested_after_the_main_id(self):
        from serial_ui import SerialChooser
        from types import SimpleNamespace
        pending = []
        chooser = SimpleNamespace(main_id=None, update_mains=lambda value: None,
                                  get_value_async=lambda *args, **kwargs: pending.append(args))
        SerialChooser.get_main_classes(chooser)
        self.assertEqual([(p[0], p[1]) for p in pending], [('main', 'id')])
        pending[0][2](0)
        self.assertEqual(chooser.main_id, 0)
        self.assertEqual(pending[-1][1], 'lsmain')
    def test_reconnect_follows_same_serial_but_never_another_board(self):
        identity = port_identity(Device('abc', 'COM3'))
        self.assertTrue(same_device(identity, Device('abc', 'COM5')))
        self.assertFalse(same_device(identity, Device('def', 'COM3')))
        self.assertFalse(same_device(identity, Device('abc', 'COM5', pid=1)))
    def test_no_serial_requires_same_location(self):
        identity = port_identity(Device('', 'COM3'))
        self.assertTrue(same_device(identity, Device('', 'COM3')))
        self.assertFalse(same_device(identity, Device('', 'COM5')))
    def test_throttle_is_independent_per_axis(self):
        got = []
        class Axis(QObject):
            @throttle(10)
            def edit(self, value):
                got.append(value)
        a, b = Axis(), Axis()
        a.edit('a1'); a.edit('a2'); b.edit('b1'); b.edit('b2')
        loop = QEventLoop()
        QTimer.singleShot(40, loop.quit)
        loop.exec()
        self.assertCountEqual(got, ['a1', 'a2', 'b1', 'b2'])
