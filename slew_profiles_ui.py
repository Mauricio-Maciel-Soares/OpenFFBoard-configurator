"""Three on-board slew slots, readback verification and persistent selection."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QDoubleSpinBox,QComboBox)
from base_ui import CommunicationHandler


class SlewProfilesDialog(QDialog,CommunicationHandler):
    def __init__(self,parent,axis):
        super().__init__(parent);self.axis=axis;self.generation=0;self.values={}
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle('Persistent slew profiles — axis {}'.format(axis));self.resize(580,300)
        layout=QVBoxLayout(self)
        note=QLabel('Three slots are stored on the board. Rates are counts/ms; zero disables the limiter. '
                    'Saving an active slot also applies its new rate. Kt, ODrive torque range and other gains are unchanged.')
        note.setWordWrap(True);layout.addWidget(note)
        self.rates=[];self.save_buttons=[]
        for slot in range(1,4):
            row=QHBoxLayout();row.addWidget(QLabel('Slot {}'.format(slot)))
            rate=QDoubleSpinBox();rate.setDecimals(0);rate.setRange(0,4294967295);rate.setSuffix(' counts/ms')
            button=QPushButton('Save slot {}'.format(slot));button.clicked.connect(lambda _,n=slot:self.save_slot(n))
            row.addWidget(rate);row.addWidget(button);layout.addLayout(row)
            self.rates.append(rate);self.save_buttons.append(button)
        row=QHBoxLayout();self.active=QComboBox()
        self.active.addItem('Custom — keep current rate',0)
        for slot in range(1,4):self.active.addItem('Slot {}'.format(slot),slot)
        self.apply_button=QPushButton('Activate and verify');self.apply_button.clicked.connect(self.activate)
        self.refresh=QPushButton('Read from board');self.refresh.clicked.connect(self.read_values)
        row.addWidget(self.active);row.addWidget(self.apply_button);row.addWidget(self.refresh);layout.addLayout(row)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        self._busy(True)

    def _busy(self,busy):
        for widget in self.rates+self.save_buttons+[self.active,self.apply_button]:widget.setEnabled(not busy and bool(self.values))
        self.refresh.setEnabled(not busy)

    def showEvent(self,event):self.read_values()

    def hideEvent(self,event):
        self.generation+=1;self.remove_callbacks()

    def _fail(self,error):
        self.values={};self._busy(False);self.status.setText('Operation not confirmed: '+str(error)+'. Read from board before retrying.')

    def read_values(self):
        self.generation+=1;generation=self.generation;self.values={};self._busy(True)
        self.status.setText('Reading saved rates and selection...')
        def supported(capabilities):
            if generation!=self.generation:return
            required={'slewprofile':{'R','W'},'slewslot':{'RA','WA'},'slewvalid':{'R'},'maxtorquerate':{'R'}}
            if any(not flags<=capabilities.get(cmd,{}).get('flags',set()) for cmd,flags in required.items()):
                self._fail('Firmware does not support persistent slew profiles');return
            items=[('slewprofile',None),('slewvalid',None),('maxtorquerate',None)]+[('slewslot',n) for n in range(1,4)]
            def advance():
                if generation!=self.generation:return
                if not items:
                    active=self.values[('slewprofile',None)];valid=self.values[('slewvalid',None)]
                    if not 0<=active<=3 or not 0<=valid<=7 or (active and (not valid & (1<<(active-1)) or
                            self.values[('maxtorquerate',None)]!=self.values[('slewslot',active)])):
                        self._fail('Inconsistent device profile values');return
                    self.active.setCurrentIndex(active)
                    for n,rate in enumerate(self.rates,1):rate.setValue(self.values[('slewslot',n)])
                    self._busy(False)
                    self.status.setText('Active: {}. Rate: {} counts/ms. Saved slots: {}.'.format(
                        'Custom' if not active else active,self.values[('maxtorquerate',None)],
                        ', '.join(str(n) for n in range(1,4) if valid&(1<<(n-1))) or 'none'))
                    return
                cmd,adr=items.pop(0)
                def received(value):
                    if generation!=self.generation:return
                    if not 0<=value<=4294967295:self._fail('Invalid rate returned by device');return
                    self.values[(cmd,adr)]=value;advance()
                self.get_value_async('axis',cmd,received,instance=self.axis,adr=adr,conversion=int,on_error=self._fail)
            advance()
        self.get_capabilities('axis',supported,instance=self.axis,on_error=self._fail)

    def save_slot(self,slot):
        self._write('slewslot',int(self.rates[slot-1].value()),slot)

    def activate(self):
        slot=self.active.currentData()
        if slot and not self.values[('slewvalid',None)]&(1<<(slot-1)):
            self.status.setText('Save this slot before activating it.');return
        self._write('slewprofile',slot)

    def _write(self,cmd,value,adr=None):
        self._busy(True);generation=self.generation
        self.status.setText('Saving and verifying on the board...')
        def confirmed(_):
            if generation==self.generation:self.read_values()
        self.set_value_verified('axis',cmd,value,confirmed,self._fail,instance=self.axis,adr=adr)
