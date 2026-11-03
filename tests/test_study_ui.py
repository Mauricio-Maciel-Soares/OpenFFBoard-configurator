import unittest
from types import SimpleNamespace
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget
from base_ui import CommunicationHandler
from serial_comms import SerialComms
from torque_trace_ui import TorqueTraceDialog
from slew_profiles_ui import SlewProfilesDialog
from diagnostics_ui import DiagnosticsDialog
from diagnostics import AXIS_FIELDS, ODRIVE_FIELDS
from torque_study import AXIS_V2_EXTRA, ODRIVE_V2_EXTRA
from test_reliability import Port, app
from test_torque_study import batch, record


class Signals(QObject):
    connected=pyqtSignal(bool)


class StudyUITests(unittest.TestCase):
    def setUp(self):
        SerialComms.callbackDict.clear();self.port=Port()
        self.comms=SerialComms(SimpleNamespace(log=lambda _:None),self.port)
        self.port.flush_requests=self.comms._send_over_uart
        self.previous=CommunicationHandler.comms;CommunicationHandler.comms=self.comms
        self.parent=QWidget();self.signals=Signals();self.parent.main=SimpleNamespace(serialchooser=self.signals)
        self.dialogs=[]
    def tearDown(self):
        for dialog in self.dialogs:dialog.close()
        self.comms.reset();self.comms.request_timer.stop();self.comms.write_timer.stop()
        CommunicationHandler.comms=self.previous;self.parent.deleteLater();app.processEvents()
    def test_trace_only_reads_and_preserves_samples_after_disconnect(self):
        self.comms.capabilities[('odrv',0)]={'trace':{'flags':{'RA'}}}
        d=TorqueTraceDialog(self.parent,0);self.dialogs.append(d);d.show();d.start()
        self.assertIn(b'trace?-1;',self.port.written)
        self.port.receive('[odrv.0.trace?-1|'+batch([record(1),record(2,2000)])+']')
        self.assertFalse(d.rows) # old firmware history is not the new experiment
        d.timer.stop();d._request()
        self.port.receive('[odrv.0.trace?2|'+batch([record(3,3000)],newest=3)+']')
        self.assertEqual(len(d.rows),1)
        self.assertAlmostEqual(d.rows[0]['estimated_torque_nm'],2.775)
        self.assertNotIn(b'=',self.port.written)
        self.signals.connected.emit(False)
        self.assertFalse(d.running);self.assertFalse(d.timer.isActive())
        self.assertEqual(len(d.rows),1);self.assertTrue(d.export_button.isEnabled())
    def test_old_firmware_never_receives_unknown_trace_request(self):
        self.comms.capabilities[('odrv',0)]={'diag':{'flags':{'R'}}}
        d=TorqueTraceDialog(self.parent,0);self.dialogs.append(d);d.show();d.start()
        self.assertFalse(d.running);self.assertNotIn(b'trace?',self.port.written)
        self.assertIn('does not support',d.status.text())
    def profiles(self):
        self.comms.capabilities[('axis',0)]={key:{'flags':flags} for key,flags in
            {'slewprofile':{'R','W'},'slewslot':{'RA','WA'},'slewvalid':{'R'},'maxtorquerate':{'R'}}.items()}
        d=SlewProfilesDialog(self.parent,0);self.dialogs.append(d);d.show()
        return d
    def reply_profiles(self,active=0,rate=10922,valid=1,slot2=0):
        for cmd,value in [('slewprofile',active),('slewvalid',valid),('maxtorquerate',rate)]:
            self.port.receive('[axis.0.{}?|{}]'.format(cmd,value))
        for slot,value in [(1,10922),(2,slot2),(3,0)]:self.port.receive('[axis.0.slewslot?{}|{}]'.format(slot,value))
    def test_profile_activation_waits_for_ack_and_readback(self):
        d=self.profiles();self.reply_profiles()
        self.assertTrue(d.apply_button.isEnabled());d.active.setCurrentIndex(1);d.activate()
        self.assertFalse(d.apply_button.isEnabled())
        self.port.receive('[axis.0.slewprofile=1|OK]')
        self.assertFalse(d.apply_button.isEnabled())
        self.port.receive('[axis.0.slewprofile?|1]')
        self.reply_profiles(active=1)
        self.assertTrue(d.apply_button.isEnabled());self.assertIn('Active: 1',d.status.text())
    def test_full_width_slot_write_has_address_and_real_readback(self):
        d=self.profiles();self.reply_profiles()
        d.rates[1].setValue(4294967295);d.save_slot(2)
        self.port.flush_requests();self.assertIn(b'slewslot=4294967295?2;',self.port.written)
        self.port.receive('[axis.0.slewslot=4294967295?2|OK]')
        self.assertFalse(d.apply_button.isEnabled())
        self.port.receive('[axis.0.slewslot?2|4294967295]')
        self.reply_profiles(valid=3,slot2=4294967295)
        self.assertEqual(d.rates[1].value(),4294967295)
        self.assertTrue(d.apply_button.isEnabled())
    def test_extended_snapshot_keeps_measured_jitter(self):
        self.comms.capabilities[('axis',0)]={'diag2':{'flags':{'R'}}}
        self.comms.capabilities[('main',0)]={k:{'flags':{'R'}} for k in ('hidrate','cfrate')}
        d=DiagnosticsDialog(self.parent,0);self.dialogs.append(d);d.show();d.start()
        fields=AXIS_FIELDS+AXIS_V2_EXTRA;data={k:0 for k in fields};data.update(version=2,window_stddev_us=12.5)
        self.port.receive('[axis.0.diag2?|'+','.join(str(data[k]) for k in fields)+']')
        self.port.receive('[main.0.hidrate?|324]');self.port.receive('[main.0.cfrate?|217]')
        self.assertEqual(d.rows[0]['axis_window_stddev_us'],12.5)
        self.assertNotIn(b'axis.0.diag?',self.port.written)


if __name__=='__main__':unittest.main()
