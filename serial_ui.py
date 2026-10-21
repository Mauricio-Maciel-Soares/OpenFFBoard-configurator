"""Serial UI module.

Regroup all required classes to manage the Serial Connection UI
and the link with the communication module.

Module : serial_ui
Authors : yannick
"""
from concurrent.futures import process
import PyQt6.QtGui
import PyQt6.QtSerialPort
import PyQt6.QtCore
import PyQt6.QtWidgets
import base_ui
import main
import helper
from connection_identity import port_identity, same_device


class SerialChooser(base_ui.WidgetUI, base_ui.CommunicationHandler):
    """This classe is the main Serial Chooser manager.

    *) Display the UI
    *) Manage the user interraction : connect/disconnect
    *) Manage the serial port status
    """

    OFFICIAL_VID_PID = [(0x1209, 0xFFB0)]  # Highlighted in serial selector
    connected = PyQt6.QtCore.pyqtSignal(bool)
    shown = PyQt6.QtCore.pyqtSignal()
    hidden = PyQt6.QtCore.pyqtSignal()
    visible = PyQt6.QtCore.pyqtSignal(bool)

    def __init__(self, serial: PyQt6.QtSerialPort.QSerialPort, main_ui: "main.MainUi"):
        """Initialize the manager with the QSerialPort for serial commmunication and the mainUi."""
        base_ui.WidgetUI.__init__(self, main_ui, "serialchooser.ui")
        base_ui.CommunicationHandler.__init__(self)
        self._serial = serial
        self.main = main_ui
        self.main_id = None
        self._classes = []
        self._class_ids = {}
        self._port = None
        self._ports = []
        self._logging = False
        self._closing = False
        self._was_open = None
        self._target = None
        self._manual_disconnect = False
        self._reconnect_attempts = 0
        self._force_reconnect = False
        self._opening = False
        self._opening_error = None
        self._open_target = None
        self._open_attempt = 0
        self._session = 0
        self._error_session = None
        self.open_retry_timer = PyQt6.QtCore.QTimer(self)
        self.open_retry_timer.setSingleShot(True)
        self.open_retry_timer.timeout.connect(self._retry_open)
        self.reconnect_timer = PyQt6.QtCore.QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._retry_connection)
        self.auto_reconnect = PyQt6.QtWidgets.QCheckBox("Reconnect this device after connection loss", self)
        self.auto_reconnect.setChecked(bool(self.main.profile_ui.get_global_setting('auto_reconnect', False)))
        self.auto_reconnect.toggled.connect(self._reconnect_changed)
        self.gridLayout.addWidget(self.auto_reconnect, 1, 0, 1, 2)

        self._serial.errorOccurred.connect(self.serial_error)
        self._serial.aboutToClose.connect(self._invalidate_session)

        self.pushButton_refresh.clicked.connect(self.get_ports)
        self.pushButton_connect.clicked.connect(self.serial_connect_button)
        self.pushButton_send.clicked.connect(self.send_line)
        self.lineEdit_cmd.returnPressed.connect(self.send_line)
        self.pushButton_ok.clicked.connect(self.main_btn)

        self.update()
        self._was_open = None  # Emit initial state after MainUi connects its slots.

    def showEvent(self, event): # pylint: disable=unused-argument, invalid-name
        """On show event, init the param.

        Connect the communication module with the history widget to load the board response.
        """
        if not self._logging:
            self.get_raw_reply().connect(self.serial_log)
            self._logging = True
        self.shown.emit()

    # Tab is hidden
    def hideEvent(self, event): # pylint: disable=unused-argument, invalid-name
        """On hide event, disconnect the event.

        Disconnect the communication module with the history widget
        to stop to log the board response.
        """
        if self._logging:
            self.get_raw_reply().disconnect(self.serial_log)
            self._logging = False
        self.hidden.emit()

    def serial_log(self, txt):
        """Add a new text in the history widget."""
        if isinstance(txt, list):
            txt = "\n".join(txt)
        else:
            txt = str(txt)
        self.serialLogBox.append(txt)

    def send_line(self):
        """Read the command input text, display it in history widget and send it to the board."""
        cmd = self.lineEdit_cmd.text() + "\n"
        self.serial_log(">" + cmd)
        try:
            self.serial_write_raw(cmd)
        except (OSError, ValueError) as exc:
            self.main.log("Command not sent: " + str(exc))

    def write(self, data):
        """Write data to the serial port."""
        self._serial.write(data)

    def update(self):
        """Update the UI when a connection is successfull.

        Disable connection button, dropbox, etc.
        Emit for all the UI the [connected] event.
        """
        opened = self._serial.isOpen() and not self._opening
        waiting = self._opening or self._open_target is not None or self.reconnect_timer.isActive()
        if opened:
            self.pushButton_connect.setText(self.tr("Disconnect"))
            self.comboBox_port.setEnabled(False)
            self.pushButton_refresh.setEnabled(False)
            self.pushButton_send.setEnabled(True)
            self.lineEdit_cmd.setEnabled(True)

        else:
            label = self.tr("Connect")
            if waiting:
                label = self.tr("Cancel reconnect") if self.reconnect_timer.isActive() else self.tr("Cancel connection")
            self.pushButton_connect.setText(label)
            self.comboBox_port.setEnabled(not waiting)
            self.pushButton_refresh.setEnabled(not waiting)
            self.pushButton_send.setEnabled(False)
            self.lineEdit_cmd.setEnabled(False)

            self.groupBox_system.setEnabled(False)

        if opened != self._was_open:
            was_open = self._was_open
            self._was_open = opened
            self.connected.emit(opened)
            if opened:
                self.get_main_classes()
            elif was_open:
                self.request_reconnect()

    def _reconnect_changed(self, enabled):
        self.main.profile_ui.set_global_setting('auto_reconnect', enabled)
        if not enabled:
            self.cancel_reconnect()

    def cancel_reconnect(self):
        self.reconnect_timer.stop()
        self.open_retry_timer.stop()
        self._open_target = None
        self._force_reconnect = False
        self._manual_disconnect = True
        self.update()

    def request_reconnect(self, force=False):
        if force:
            self._manual_disconnect = False
            self._force_reconnect = True
        if self._manual_disconnect or not self._target or getattr(self.main, '_shutdown_done', False):
            return
        if not (self.auto_reconnect.isChecked() or self._force_reconnect):
            return
        if self.reconnect_timer.isActive():
            return
        self._reconnect_attempts = 0
        self.main.log("Connection lost. Waiting for the same device...")
        self.reconnect_timer.start(1500)
        self.update()

    def _retry_connection(self):
        if self._manual_disconnect or getattr(self.main, '_shutdown_done', False) or self._serial.isOpen():
            return
        self._reconnect_attempts += 1
        ports = PyQt6.QtSerialPort.QSerialPortInfo.availablePorts()
        matches = [p for p in ports if same_device(self._target, p)]
        if len(matches) == 1:
            self.get_ports(ports=ports)
            index = next(i for i, p in enumerate(ports) if same_device(self._target, p))
            self.comboBox_port.setCurrentIndex(index)
            self.serial_connect(allow_retry=False)
            self.update()
            if self._serial.isOpen():
                self._force_reconnect = False
                self.main.log("Device reconnected. Pending changes were not replayed.")
                return
        if self._reconnect_attempts >= 8:
            self.main.log("Reconnect stopped after 8 attempts. Check the cable and selected port.")
            self.cancel_reconnect()
            return
        self.reconnect_timer.start(min(15000, 1000 * 2 ** self._reconnect_attempts))
        self.update()

    def serial_connect_button(self):
        if self._serial.isOpen() or self.reconnect_timer.isActive() or self._open_target is not None:
            self.cancel_reconnect()
            self.main.reset_port(immediate=True)
        elif self._port is not None:
            self._manual_disconnect = False
            self.serial_connect()
            self.update()

    def serial_connect(self, allow_retry=True):
        """Open the selected device, with bounded retries for transient OS failures."""
        self.select_port(self.comboBox_port.currentIndex())
        if not self._serial.isOpen() and self._port is not None:
            self._open_target = port_identity(self._port) if allow_retry else None
            self._open_attempt = 0
            self._attempt_open()

    def _attempt_open(self):
        self._open_attempt += 1
        self._invalidate_session()
        self.main.cancel_pending_port_close()
        self.main.comms_reset()
        self._opening = True
        self._opening_error = None
        self.update()
        phase = "configure"
        success = False
        failure = None
        self.main.log("Connecting to {} (attempt {})...".format(self._port.portName(), self._open_attempt))
        try:
            self._serial.setPort(self._port)
            port_type = PyQt6.QtSerialPort.QSerialPort
            settings = (
                (self._serial.setBaudRate, 115200),
                (self._serial.setDataBits, port_type.DataBits.Data8),
                (self._serial.setParity, port_type.Parity.NoParity),
                (self._serial.setStopBits, port_type.StopBits.OneStop),
                (self._serial.setFlowControl, port_type.FlowControl.NoFlowControl),
            )
            configured = all(setter(value) for setter, value in settings)
            if configured:
                phase = "open"
                if self._serial.open(PyQt6.QtCore.QIODevice.OpenModeFlag.ReadWrite):
                    phase = "DTR"
                    success = self._serial.setDataTerminalReady(True)
            if self._opening_error is not None:
                success = False
            if not success:
                # Preserve the original error before close() changes the OS error state.
                failure = self._opening_error or (self._serial.error(), self._serial.errorString())
                if self._serial.isOpen():
                    self._serial.close()
        finally:
            self._opening = False

        if success:
            # Do not clear(AllDirections): on Windows it aborts pending asynchronous I/O.
            self._target = port_identity(self._port)
            self._open_target = None
            self.open_retry_timer.stop()
            self.main.log("Serial port {} opened.".format(self._port.portName()))
        else:
            error, message = failure
            self.main.log("Cannot connect to {} during {}: {} ({}): {}".format(
                self._port.portName(), phase, error.name, error.value, message))
            errors = PyQt6.QtSerialPort.QSerialPort.SerialPortError
            if error == errors.ResourceError and self._open_target is not None and self._open_attempt < 3:
                self.main.log("Retrying the same device after the OS cancelled or lost serial I/O...")
                self.open_retry_timer.start(500 * self._open_attempt)
            else:
                self._open_target = None
                self.main.log("Connection stopped. Close other configurators/serial terminals, reconnect USB, "
                              "then select the board's current port and try again.")
        self.update()

    def _retry_open(self):
        if self._open_target is None or self._manual_disconnect or getattr(self.main, '_shutdown_done', False):
            return
        ports = PyQt6.QtSerialPort.QSerialPortInfo.availablePorts()
        matches = [i for i, port in enumerate(ports) if same_device(self._open_target, port)]
        if len(matches) != 1:
            self.main.log("Connection stopped: the selected device is missing or ambiguous. Refresh the ports.")
            self.cancel_reconnect()
            return
        self.get_ports(ports=ports)
        self.comboBox_port.setCurrentIndex(matches[0])
        self.select_port(matches[0])
        self._attempt_open()

    def _invalidate_session(self):
        self._session += 1
        self._error_session = None

    def serial_error(self, error):
        """Close the port when the device reports an unrecoverable error."""
        errors = PyQt6.QtSerialPort.QSerialPort.SerialPortError
        if error in (errors.NoError, errors.NotOpenError) or self._closing or getattr(self.main, '_resetting_port', False):
            return
        if self._opening:
            if self._opening_error is None:
                self._opening_error = (error, self._serial.errorString())
            return

        self.main.log("Serial error on {}: {} ({}): {}".format(
            self._serial.portName(), error.name, error.value, self._serial.errorString()))
        if error in (errors.ResourceError, errors.DeviceNotFoundError, errors.PermissionError) and self._serial.isOpen():
            # Never reset queues/close a Windows handle from inside its own I/O callback.
            session = self._session
            if self._error_session != session:
                self._error_session = session
                PyQt6.QtCore.QTimer.singleShot(0, lambda: self._close_failed_session(session))

    def _close_failed_session(self, session):
        if session != self._session or not self._serial.isOpen():
            return
        self._closing = True
        try:
            self.main.reset_port(immediate=True)
        finally:
            self._closing = False

    def select_port(self, port_id):
        """Change the selected port."""
        if 0 <= port_id < len(self._ports):
            self._port = self._ports[port_id]
        else:
            self._port = None

    def get_ports(self, checked=False, ports=None):
        """Get all the serial port available on the computer.

        If the VID.VIP is compatible with openFFBoard color the text in green,
        else put it in red.
        """
        oldport = self._port if self._port else None

        # Use one enumeration for matching and selection: USB may disappear between reads.
        self._ports = PyQt6.QtSerialPort.QSerialPortInfo.availablePorts() if ports is None else ports
        self.comboBox_port.clear()
        sel_idx = 0
        nb_compatible_device = 0
        for i, port in enumerate(self._ports):
            supported_vid_pid = (
                port.vendorIdentifier(),
                port.productIdentifier(),
            ) in self.OFFICIAL_VID_PID
            name = port.portName() + " : " + port.description()

            if supported_vid_pid and not name.startswith("cu."):
                name += " (FFBoard device)"
            else:
                name += " (Unsupported device)"
            self.comboBox_port.addItem(name)

            if supported_vid_pid and not name.startswith("cu."):
                sel_idx = i
                nb_compatible_device = nb_compatible_device + 1
                self.comboBox_port.setItemData(
                    i,
                    PyQt6.QtGui.QColor("green"),
                    PyQt6.QtCore.Qt.ItemDataRole.ForegroundRole,
                )
            else:
                self.comboBox_port.setItemData(
                    i,
                    PyQt6.QtGui.QColor("red"),
                    PyQt6.QtCore.Qt.ItemDataRole.ForegroundRole,
                )

        plist = [p.portName() for p in self._ports]
        if (
            (oldport is not None)
            and (
                (oldport.vendorIdentifier(), oldport.productIdentifier())
                in self.OFFICIAL_VID_PID
            )
            and (oldport.portName() in plist)
        ):
            self.comboBox_port.setCurrentIndex(plist.index(oldport.portName()))
        else:
            self.comboBox_port.setCurrentIndex(sel_idx)  # preselect found entry

        self.select_port(self.comboBox_port.currentIndex())
        self.update()

        return nb_compatible_device
    
    def auto_connect(self, nb_compatible_device):
        if (nb_compatible_device == 1) :
            self.serial_connect_button()

    def update_mains(self, dat):
        """Parse the list of main classes received from board, and update the combobox."""
        self.comboBox_main.clear()
        self._class_ids, self._classes = helper.classlistToIds(dat)

        if self.main_id is None:
            # self.main.resetPort()
            self.groupBox_system.setEnabled(False)
            return
        self.groupBox_system.setEnabled(True)

        helper.updateClassComboBox(
            self.comboBox_main, self._class_ids, self._classes, self.main_id
        )

        self.main.log("Detected mode: " + self.comboBox_main.currentText())
        self.main.update_tabs()

    def get_main_classes(self):
        """Get the main classes available from the board in Async."""

        def fct(i):
            """Store the main currently selected to refresh the UI."""
            self.main_id = i
            self.get_value_async("sys", "lsmain", self.update_mains, delete=True)

        self.get_value_async("main", "id", fct, conversion=int, delete=True)

    def main_btn(self):
        """Read the select main class in the combobox.

        Push it to the board and display the reload warning.
        """
        index = self._classes[self.comboBox_main.currentIndex()][0]
        self.send_value("sys", "main", index)
        self.main.reconnect()
        msg = PyQt6.QtWidgets.QMessageBox(
            PyQt6.QtWidgets.QMessageBox.Icon.Information,
            "Main class changed",
            "Chip is rebooting. Please reconnect.",
        )
        msg.exec()
