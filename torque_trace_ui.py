"""Explicit passive current capture and an observational study by motor RPM."""
import json
import time
from collections import deque
from PyQt6.QtCore import QTimer, QPointF, Qt
from PyQt6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                            QSpinBox, QDoubleSpinBox, QLineEdit, QFileDialog, QPlainTextEdit)
from base_ui import CommunicationHandler
from diagnostics import export_csv
from profile_store import atomic_write
from torque_study import TraceCapture, parse_trace, analyze


class TorqueTraceDialog(QDialog, CommunicationHandler):
    def __init__(self, parent, axis):
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.rows = deque(maxlen=12000)
        self.generation = 0
        self.running = False
        self.axis = axis
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._request)
        self.setWindowTitle('Torque / current study')
        self.resize(900, 680)
        layout = QVBoxLayout(self)
        note = QLabel('Passive capture: no forces or motor settings are sent. Each Iq frame records the latest '
                      'queued torque and encoder sample with their ages. Iq × Kt is an electromagnetic torque '
                      'estimate, not measured shaft torque. The firmware keeps 64 samples; the PC keeps the latest 12000.')
        note.setWordWrap(True);layout.addWidget(note)
        form = QHBoxLayout()
        self.instance = QSpinBox();self.instance.setRange(0,1);self.instance.setValue(min(axis,1))
        self.kt = QDoubleSpinBox();self.kt.setRange(.00001,1000);self.kt.setDecimals(5);self.kt.setValue(1.11)
        self.label = QLineEdit('baseline');self.label.setMaxLength(64)
        self.rpm_bin = QSpinBox();self.rpm_bin.setRange(1,1000);self.rpm_bin.setValue(30)
        for title, widget in [('ODrive instance',self.instance),('Configured Kt (Nm/A)',self.kt),
                              ('Capture label',self.label),('RPM bin',self.rpm_bin)]:
            form.addWidget(QLabel(title));form.addWidget(widget)
        layout.addLayout(form)
        buttons=QHBoxLayout()
        self.start_button=QPushButton('Start new capture');self.stop_button=QPushButton('Stop')
        self.export_button=QPushButton('Export trace CSV');self.study_button=QPushButton('Analyze / export JSON')
        self.start_button.clicked.connect(self.start);self.stop_button.clicked.connect(self.stop)
        self.export_button.clicked.connect(self.export);self.study_button.clicked.connect(self.study)
        for button in (self.start_button,self.stop_button,self.export_button,self.study_button):buttons.addWidget(button)
        layout.addLayout(buttons)
        self.chart=QChart();self.series=[]
        for name,field in [('Queued torque (Nm)','queued_torque_nm'),('Iq setpoint × Kt (Nm)','iq_setpoint_torque_nm'),
                           ('Measured Iq × Kt (Nm)','estimated_torque_nm')]:
            series=QLineSeries();series.setName(name);self.chart.addSeries(series);self.series.append((series,field))
        self.xaxis,self.yaxis=QValueAxis(),QValueAxis()
        self.xaxis.setTitleText('CAN receive time since first captured sample (s)')
        self.yaxis.setTitleText('Motor coordinates (Nm)')
        self.chart.addAxis(self.xaxis,Qt.AlignmentFlag.AlignBottom);self.chart.addAxis(self.yaxis,Qt.AlignmentFlag.AlignLeft)
        for series,_ in self.series:series.attachAxis(self.xaxis);series.attachAxis(self.yaxis)
        layout.addWidget(QChartView(self.chart),1)
        self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(110);layout.addWidget(self.details)
        self.status=QLabel('Stopped. Requires firmware with odrv.trace and incoming Get_Iq frames.')
        self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        parent.main.serialchooser.connected.connect(self.connection_changed)
        self._controls()

    def _controls(self):
        self.start_button.setEnabled(not self.running);self.stop_button.setEnabled(self.running)
        self.export_button.setEnabled(bool(self.rows));self.study_button.setEnabled(bool(self.rows) and not self.running)
        for widget in (self.instance,self.kt,self.label,self.rpm_bin):widget.setEnabled(not self.running)

    def start(self):
        if not self.comms.serial.isOpen():
            self.status.setText('Connect the board first.');return
        self.generation+=1;generation=self.generation
        self.capture=TraceCapture(self.kt.value());self.rows.clear();self.running=True
        self.start_time=time.monotonic();self.priming=True;self.last_plot=0
        for series,_ in self.series:series.clear()
        self._controls();self.status.setText('Checking passive trace support...')
        def ready(capabilities):
            if generation!=self.generation or not self.running:return
            if 'RA' not in capabilities.get('trace',{}).get('flags',set()):
                self._fail('Firmware does not support passive current trace.');return
            self._request()
        self.get_capabilities('odrv',ready,instance=self.instance.value(),on_error=self._fail)

    def _request(self):
        if not self.running:return
        generation=self.generation
        def received(payload):
            if not self.running or generation!=self.generation:return
            try:
                if self.priming:
                    # Discard pre-capture history, without writing/resetting firmware.
                    self.capture.cursor=parse_trace(payload)['newest'];self.priming=False
                else:
                    for row in self.capture.accept(payload):
                        row.update(host_elapsed_s=time.monotonic()-self.start_time,label=self.label.text(),
                                   axis_instance=self.axis,odrive_instance=self.instance.value())
                        self.rows.append(row)
            except (ValueError,KeyError) as exc:
                self._fail(exc);return
            if time.monotonic()-self.last_plot>=.2:
                self._plot();self.last_plot=time.monotonic()
            self._controls()
            overwritten=max(0,self.capture.total-len(self.rows))
            self.status.setText('{} retained / {} received; {} lost before USB read; {} resets; {} oldest PC rows discarded.'.format(
                len(self.rows),self.capture.total,self.capture.lost,self.capture.restarts,overwritten)
                if self.rows else 'Waiting for Get_Iq frames. Check ODrive iq_rate_ms if no samples arrive; no setting is changed here.')
            self.timer.start(20)
        self.get_value_async('odrv','trace',received,instance=self.instance.value(),adr=self.capture.cursor,on_error=self._fail)

    def _fail(self,error):
        if not self.running:return
        self.stop();self.status.setText('Capture stopped: '+str(error))

    def stop(self):
        self.running=False;self.generation+=1;self.timer.stop();self.remove_callbacks();self._controls()
        self.status.setText('Stopped. {} retained samples can be exported.'.format(len(self.rows)))

    def hideEvent(self,event):
        self.stop()

    def connection_changed(self,connected):
        if not connected and self.running:self._fail('Disconnected; captured samples retained.')

    def _plot(self):
        data=list(self.rows)[-600:]
        # Never draw a line across a known missing batch or timer/reset epoch.
        cut=0
        for i,row in enumerate(data):
            if row['lost_before'] or (i and row['capture_epoch']!=data[i-1]['capture_epoch']):cut=i
        data=data[cut:]
        values=[]
        for series,field in self.series:
            points=[QPointF(row['sample_time_s'],row[field]) for row in data
                    if field!='queued_torque_nm' or row['tx_sequence']]
            series.replace(points);values += [p.y() for p in points]
        if data and values:
            self.xaxis.setRange(data[0]['sample_time_s'],max(data[0]['sample_time_s']+.1,data[-1]['sample_time_s']))
            lo,hi=min(values),max(values);margin=max(.1,(hi-lo)*.1);self.yaxis.setRange(lo-margin,hi+margin)
            self.details.setPlainText('Latest sample ages: queued torque {} µs; encoder {} µs; voltage {} µs.\n'
                                      'Axis state {}; axis errors 0x{:08X}; encoder valid {}.\n'
                                      'Graph: latest contiguous observations. Command/current differences include asynchronous timing.'.format(
                                          data[-1]['torque_age_us'],data[-1]['encoder_age_us'],data[-1]['voltage_age_us'],
                                          data[-1]['axis_state'],data[-1]['axis_errors'],data[-1]['encoder_valid']))

    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'Export retained current trace','torque_trace.csv','CSV (*.csv)')
        if path:
            try:atomic_write(path,export_csv(list(self.rows)))
            except OSError as exc:self.status.setText('Export failed: '+str(exc))
            else:self.status.setText('Exported {} retained observations.'.format(len(self.rows)))

    def study(self):
        result=analyze(list(self.rows),self.rpm_bin.value())
        result.update(capture_total=self.capture.total,capture_lost=self.capture.lost,
                      pc_rows_discarded=max(0,self.capture.total-len(self.rows)))
        self.details.setPlainText(json.dumps(result,ensure_ascii=False,indent=2))
        path,_=QFileDialog.getSaveFileName(self,'Export observational study','torque_study.json','JSON (*.json)')
        if path:
            try:atomic_write(path,json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8'))
            except (OSError,ValueError) as exc:self.status.setText('Export failed: '+str(exc))
            else:self.status.setText('Study exported. {} valid speed groups; {} rejected observations.'.format(len(result['groups']),result['rejected_rows']))
