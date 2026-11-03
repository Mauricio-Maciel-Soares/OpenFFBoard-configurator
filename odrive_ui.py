from PyQt6.QtWidgets import QMainWindow
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QWidget
from PyQt6.QtWidgets import QMessageBox,QVBoxLayout,QCheckBox,QButtonGroup 
from PyQt6 import uic
from helper import res_path,classlistToIds
from PyQt6.QtCore import QTimer
import main
from base_ui import WidgetUI
from base_ui import CommunicationHandler
import portconf_ui
from diagnostics import decode_axis_errors
from PyQt6.QtWidgets import QComboBox, QLabel

class OdriveUI(WidgetUI,CommunicationHandler):
    ODRIVE_STATES = ["AXIS_STATE_UNDEFINED","AXIS_STATE_IDLE","AXIS_STATE_STARTUP_SEQUENCE","AXIS_STATE_FULL_CALIBRATION_SEQUENCE","AXIS_STATE_MOTOR_CALIBRATION","-","AXIS_STATE_ENCODER_INDEX_SEARCH","AXIS_STATE_ENCODER_OFFSET_CALIBRATION","AXIS_STATE_CLOSED_LOOP_CONTROL","AXIS_STATE_LOCKIN_SPIN","AXIS_STATE_ENCODER_DIR_FIND","AXIS_STATE_HOMING","AXIS_STATE_ENCODER_HALL_POLARITY_CALIBRATION","AXIS_STATE_ENCODER_HALL_PHASE_CALIBRATION"]
    def __init__(self, main=None, unique=None):
        WidgetUI.__init__(self, main,'odrive.ui')
        CommunicationHandler.__init__(self)
        self.main = main #type: main.MainUi

        self.timer = QTimer(self)
        self.canOptions = portconf_ui.CanOptionsDialog(0,"CAN",main)
        self.pushButton_apply.clicked.connect(self.apply)
        self.pushButton_cansettings.clicked.connect(self.canOptions.exec)
        #self.pushButton_anticogging.clicked.connect(self.antigoggingBtn) #TODO test first
        self.timer.timeout.connect(self.updateTimer)
        self.prefix = unique
        self.connected = False
        self.last_error_codes = 0
        self.error_version = QComboBox(self)
        self.error_version.addItem("Error decoding: raw / other firmware", 'raw')
        self.error_version.addItem("Error decoding: ODrive 0.5.6", '0.5.6')
        self.verticalLayout.insertWidget(0, self.error_version)
        self.error_version.currentIndexChanged.connect(lambda: self.showErrors(self.last_error_codes))
        self.label_errornames.setWordWrap(True)

        self.register_callback("odrv","canid",self.spinBox_id.setValue,self.prefix,int)
        #self.registerCallback("odrv","canspd",self.updateCanSpd,self.prefix,int)
        self.register_callback("odrv","connected",self.connectedCb,self.prefix,int)
        self.register_callback("odrv","maxtorque",self.updateTorque,self.prefix,int)
        self.register_callback("odrv","vbus",self.voltageCb,self.prefix,int)
        self.register_callback("odrv","errors",lambda v : self.showErrors(v),self.prefix,int)
        self.register_callback("odrv","state",lambda v : self.stateCb(v),self.prefix,int)
        self.register_callback("odrv","storepos",lambda v : self.checkBox_storeencoffset.setChecked(v != 0),self.prefix,int)

        self.init_ui()
        
    # Tab is currently shown
    def showEvent(self,event):
        self.init_ui()
        self.timer.start(500)

    # Tab is hidden
    def hideEvent(self,event):
        self.timer.stop()

    def init_ui(self):
        commands = ["canid","canspd","maxtorque","storepos"]
        self.send_commands("odrv",commands,self.prefix)

       
    def connectedCb(self,v):
        self.connected = False if v == 0 else True

    # def updateCanSpd(self,preset):
    #     self.comboBox_baud.setCurrentIndex(preset-3) # 3 is lowest preset!

    def updateTorque(self,torque):
        self.doubleSpinBox_torque.setValue(torque/100)

    def voltageCb(self,v):
        if not self.connected:
            self.label_voltage.setText("Not connected")
            return
        self.label_voltage.setText("{}V".format(v/1000))

    def showErrors(self, codes):
        self.last_error_codes = codes
        if not self.connected:
            self.label_errornames.setText("Not connected; last raw error: 0x{:08X}".format(codes))
            return
        try:
            text = decode_axis_errors(codes, self.error_version.currentData())
        except ValueError as exc:
            text = str(exc)
        if codes and self.error_version.currentData() == '0.5.6':
            text += "\nSubsystem failures require details from odrivetool."
        self.label_errornames.setText(text)

    def stateCb(self,dat):
        if not self.connected:
            self.label_state.setText("Not connected")
            return
        if(dat < len(self.ODRIVE_STATES)):
            self.label_state.setText(self.ODRIVE_STATES[dat])
        else:
            self.label_state.setText(str(dat))


    def updateTimer(self):
        self.send_commands("odrv",["connected","vbus","errors","state"],self.prefix)
        #self.serial_get_async(["odriveVbus?","odriveErrors?","odriveState?"],self.statusUpdateCb,int,self.prefix)

        
    def apply(self):
        #spdPreset = str(self.comboBox_baud.currentIndex()+3) # 3 is lowest preset!
        canId = str(self.spinBox_id.value())
        torqueScaler = str(int(self.doubleSpinBox_torque.value() * 100))
        #self.send_value("odrv","canspd",spdPreset,instance=self.prefix)
        self.send_value("odrv","canid",canId,instance=self.prefix)
        self.send_value("odrv","maxtorque",torqueScaler,instance=self.prefix)
        self.send_value("odrv","storepos",1 if self.checkBox_storeencoffset.isChecked() else 0,instance=self.prefix)
        

        self.init_ui() # Update UI

