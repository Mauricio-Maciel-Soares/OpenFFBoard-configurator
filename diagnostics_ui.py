"""Bounded, on-demand passive capture. No motor-setting or effect commands."""
import math
import time
from collections import deque
from PyQt6.QtCore import QTimer, QPointF, Qt
from PyQt6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                            QComboBox, QSpinBox, QCheckBox, QFileDialog, QPlainTextEdit)
from base_ui import CommunicationHandler
from diagnostics import AXIS_FIELDS, ODRIVE_FIELDS, parse_payload, loop_rates, decode_axis_errors, export_csv
from profile_store import atomic_write


class DiagnosticsDialog(QDialog, CommunicationHandler):
    PERIOD_MS = 200

    def __init__(self, parent, axis):
        super().__init__(parent)
        self.axis = axis
        self.rows = deque(maxlen=600)
        self.running = False
        self.generation = 0
        self.disconnects = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._sample)
        self.setWindowTitle("Live diagnostics — axis {}".format(axis))
        self.resize(800, 660)
        layout = QVBoxLayout(self)
        note = QLabel("Passive capture, up to 5 samples/s; keeps the latest 600 samples. "
                      "HID/CF rates describe effect updates from the game. Host timing includes polling and UI delays; "
                      "it is not USB latency or motor-loop jitter. Torque counts are commands, not measured shaft torque.")
        note.setWordWrap(True)
        layout.addWidget(note)
        options = QHBoxLayout()
        self.odrive_enabled = QCheckBox("Include ODrive instance")
        self.odrive_instance = QSpinBox()
        self.odrive_instance.setRange(0, 255)
        self.odrive_instance.setValue(axis)
        self.odrive_version = QComboBox()
        self.odrive_version.addItem("ODrive errors: raw / other firmware", 'raw')
        self.odrive_version.addItem("ODrive errors: firmware 0.5.6", '0.5.6')
        options.addWidget(self.odrive_enabled)
        options.addWidget(self.odrive_instance)
        options.addWidget(self.odrive_version)
        layout.addLayout(options)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start new capture")
        self.stop_button = QPushButton("Stop")
        self.export_button = QPushButton("Export CSV")
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.export_button.clicked.connect(self.export)
        for button in (self.start_button, self.stop_button, self.export_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.metric = QComboBox()
        for title, field in (("Output torque command (counts)", 'axis_output_counts'),
                             ("Torque command, legacy (counts)", 'axis_curtorque_counts'),
                             ("HID effect update rate (Hz)", 'hidrate_hz'),
                             ("Constant force update rate (Hz)", 'cfrate_hz'),
                             ("Host sampling interval (ms)", 'host_interval_ms'),
                             ("Firmware last dt (µs)", 'axis_dt_us'),
                             ("Firmware loop rate between snapshots (Hz)", 'loop_hz'),
                             ("ODrive measured Iq, fresh only (A)", 'iq_fresh_measured_a')):
            self.metric.addItem(title, field)
        self.metric.currentIndexChanged.connect(self._plot)
        layout.addWidget(self.metric)
        self.chart = QChart()
        self.series = QLineSeries()
        self.chart.addSeries(self.series)
        self.chart.legend().hide()
        self.xaxis, self.yaxis = QValueAxis(), QValueAxis()
        self.xaxis.setTitleText("Host elapsed time (s)")
        self.chart.addAxis(self.xaxis, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.yaxis, Qt.AlignmentFlag.AlignLeft)
        self.series.attachAxis(self.xaxis)
        self.series.attachAxis(self.yaxis)
        layout.addWidget(QChartView(self.chart), 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(150)
        layout.addWidget(self.details)
        self.status = QLabel("Stopped. No samples collected.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.stop_button.setEnabled(False)
        self.export_button.setEnabled(False)
        parent.main.serialchooser.connected.connect(self.connection_changed)

    def _controls(self):
        self.start_button.setEnabled(not self.running)
        self.stop_button.setEnabled(self.running)
        self.export_button.setEnabled(bool(self.rows))
        for widget in (self.odrive_enabled, self.odrive_instance, self.odrive_version):
            widget.setEnabled(not self.running)

    def start(self):
        if not self.comms.serial.isOpen():
            self.status.setText("Connect the board before starting capture.")
            return
        self.rows.clear()
        self.series.clear()
        self.unsupported = set()
        self.previous_axis = None
        self.previous_start = None
        self.start_time = time.monotonic()
        self.running = True
        self.generation += 1
        self._controls()
        self._sample()

    def stop(self):
        self.running = False
        self.generation += 1
        self.timer.stop()
        self.remove_callbacks()
        self._controls()
        self.status.setText("Stopped. {} samples available for CSV export.".format(len(self.rows)))

    def hideEvent(self, event):
        self.stop()

    def connection_changed(self, connected):
        if not connected and self.running:
            self.disconnects += 1
            self.rows.append({'host_end_s': time.monotonic() - self.start_time,
                              'event': 'disconnected', 'disconnects': self.disconnects})
            self.stop()
            self.status.setText("Disconnected. Capture stopped; previous samples can be exported.")

    def _sample(self):
        if not self.running:
            return
        generation = self.generation
        started = time.monotonic()
        row = {'axis_instance': self.axis, 'host_start_s': started - self.start_time,
               'host_interval_ms': '' if self.previous_start is None else (started - self.previous_start) * 1000,
               'request_timeouts': self.comms.requests.timeouts, 'request_errors': self.comms.requests.errors,
               'disconnects': self.disconnects}
        self.previous_start = started
        commands = [('axis', self.axis, 'diag'), ('main', 0, 'hidrate'), ('main', 0, 'cfrate')]
        if ('axis', self.axis, 'diag') in self.unsupported:
            commands[0] = ('axis', self.axis, 'curtorque')
        if self.odrive_enabled.isChecked():
            instance = self.odrive_instance.value()
            row['odrive_instance'] = instance
            row['odrive_error_version'] = self.odrive_version.currentData()
            commands += [('odrv', instance, name) for name in ('diag', 'connected', 'errors')]
        def next_command():
            if not self.running or generation != self.generation:
                return
            if not commands:
                row['host_end_s'] = time.monotonic() - self.start_time
                row['host_capture_ms'] = (time.monotonic() - started) * 1000
                self.rows.append(row)
                self.details.setPlainText('\n'.join('{}: {}'.format(k, v) for k, v in row.items()
                                                    if k not in ('axis_version', 'odrive_version')))
                self._plot()
                self._controls()
                self.status.setText("Capturing: {} / 600 samples. Unsupported: {}".format(
                    len(self.rows), ', '.join('{}.{}.{}'.format(*k) for k in sorted(self.unsupported)) or 'none'))
                self.timer.start(max(1, self.PERIOD_MS - int((time.monotonic() - started) * 1000)))
                return
            key = commands.pop(0)
            if key in self.unsupported:
                next_command()
                return
            cls, instance, cmd = key
            sent = time.monotonic()
            def received(value):
                if not self.running or generation != self.generation:
                    return
                row['{}_{}_reply_s'.format(cls, cmd)] = time.monotonic() - self.start_time
                row['{}_{}_request_ms'.format(cls, cmd)] = (time.monotonic() - sent) * 1000
                try:
                    if cmd == 'diag':
                        data = parse_payload(value, AXIS_FIELDS if cls == 'axis' else ODRIVE_FIELDS)
                        prefix = 'axis_' if cls == 'axis' else 'odrive_'
                        row.update({prefix + k: v for k, v in data.items()})
                        if cls == 'axis':
                            row.update(loop_rates(self.previous_axis, data))
                            self.previous_axis = data
                        else:
                            fresh = data['iq_sequence'] != 0 and data['iq_age_ms'] <= 50
                            row['iq_fresh'] = int(fresh)
                            row['iq_fresh_measured_a'] = data['iq_measured_a'] if fresh else ''
                    elif cmd == 'errors':
                        row['odrive_axis_errors'] = int(value)
                        row['odrive_errors_text'] = decode_axis_errors(int(value), self.odrive_version.currentData())
                    else:
                        number = float(value)
                        if not math.isfinite(number):
                            raise ValueError('Non-finite reading')
                        field = {'hidrate': 'hidrate_hz', 'cfrate': 'cfrate_hz',
                                 'curtorque': 'axis_curtorque_counts', 'connected': 'odrive_connected'}[cmd]
                        row[field] = number
                except (ValueError, KeyError) as exc:
                    self.stop()
                    self.status.setText("Invalid diagnostic reply: " + str(exc))
                    return
                next_command()
            def failed(error):
                if not self.running or generation != self.generation:
                    return
                if error.code == 'NOT_FOUND':
                    self.unsupported.add(key)
                    if key == ('axis', self.axis, 'diag'):
                        commands.insert(0, ('axis', self.axis, 'curtorque'))
                    next_command()
                else:
                    row.update(host_end_s=time.monotonic() - self.start_time, event=str(error))
                    self.rows.append(row)
                    self.stop()
                    self.status.setText("Capture stopped: " + str(error))
            self.get_supported_value(cls, cmd, received, instance=instance, on_error=failed)
        next_command()

    def _plot(self):
        field = self.metric.currentData()
        points = [QPointF(row['host_end_s'], row[field]) for row in self.rows
                  if isinstance(row.get(field), (int, float)) and 'host_end_s' in row]
        self.series.replace(points)
        self.chart.setTitle(self.metric.currentText())
        if points:
            self.xaxis.setRange(points[0].x(), max(points[0].x() + 1, points[-1].x()))
            low, high = min(p.y() for p in points), max(p.y() for p in points)
            margin = max(1, (high - low) * .1)
            self.yaxis.setRange(low - margin, high + margin)

    def export(self):
        if not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export latest diagnostic samples", "openffboard-diagnostics.csv", "CSV (*.csv)")
        if path:
            try:
                atomic_write(path, export_csv(list(self.rows)))
                self.status.setText("Exported {} samples to {}".format(len(self.rows), path))
            except OSError as exc:
                self.status.setText("CSV export failed: " + str(exc))
