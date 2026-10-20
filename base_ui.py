"""Base module helper for UI.

Regroup helper for UI class : the widget_ui, communication_handler and the
inner EventLogger.

Module : base_ui
Authors : yannick, vincet
"""
import logging
import PyQt6.QtCore
import PyQt6.QtWidgets
import helper
import serial_comms


class EventLogger(PyQt6.QtCore.QObject):
    """Manage all operation on log_event signal and receiver registration."""

    __log_event = PyQt6.QtCore.pyqtSignal(str)

    def __init__(self):
        """Init the mandatory QObject to manage Signal."""
        PyQt6.QtCore.QObject.__init__(self)

    def log_message(self, msg: str):
        """Send the log message to all receiver."""
        self.__log_event.emit(msg)

    def register_to_logger(self, logger):
        """Register a receiver on the log signal."""
        self.__log_event.connect(logger)

    def unregister_to_logger(self, logger):
        """Unregister a receiver on the log signal."""
        self.__log_event.disconnect(logger)


class WidgetUI(PyQt6.QtWidgets.QWidget):
    """Load the .ui file and set item to the current class. Provide a quick access to logger."""

    logger = EventLogger()
    tech_log : logging.Logger = None 

    def __init__(self, parent: PyQt6.QtWidgets.QWidget = None, ui_form: str = ""):
        """Load the .ui file and map it."""
        PyQt6.QtWidgets.QWidget.__init__(self, parent)
        self.tech_log = logging.getLogger(ui_form)
        if ui_form:
            PyQt6.uic.loadUi(helper.res_path(ui_form), self)
            # print(f"UI file loaded : {ui_form}")

    def init_ui(self):
        """Prototype of init_ui to manage this status in subclass."""
        return True

    def log(self, message):
        """Access to the internal logger and offer a log message to all subclass."""
        self.logger.log_message(message)


class CommunicationHandler:
    """Store the serial communication to share it to subclass and offer register operation."""

    CMDFLAG_GET	= 0x01
    CMDFLAG_SET	 =	0x02
    CMDFLAG_INFOSTRING	= 0x08
    CMDFLAG_GETADR	 =	0x10
    CMDFLAG_SETADR	 =	0x20
    CMDFLAG_HIDDEN	= 0x40
    CMDFLAG_DEBUG	= 0x80

    comms: serial_comms.SerialComms = None

    def __init__(self):
        """Do nothing on the constructor."""

    def __del__(self):
        """Unregister all callback on class destruction."""
        self.remove_callbacks()

    # deletes all callbacks to this class
    def remove_callbacks(self, handler = None):
        """Remove all callback in SerialComms object (static)."""
        if handler is None : handler = self
        serial_comms.SerialComms.removeCallbacks(handler)

    def remove_callback(
        self,cls=None,cmd=None,callback=None,instance=0,adr=None,typechar='?'):
        """Register a callback that can be deleted automatically later."""
        # Callbacks normally must prevent sending a value change command in this callback
        # to prevent the same value from being sent back again
        serial_comms.SerialComms.removeCallback(
            self,
            cls=cls,
            cmd=cmd,
            callback=callback,
            instance=instance,
            adr=adr,
            typechar=typechar,
        )

    def register_callback(
        self,
        cls,
        cmd,
        callback,
        instance=0,
        conversion=None,
        adr=None,
        delete=False,
        typechar="?",
    ):
        """Register a callback that can be deleted automatically later."""
        # Callbacks normally must prevent sending a value change command in this callback
        # to prevent the same value from being sent back again
        serial_comms.SerialComms.registerCallback(
            self,
            cls=cls,
            cmd=cmd,
            callback=callback,
            instance=instance,
            conversion=conversion,
            adr=adr,
            delete=delete,
            typechar=typechar,
        )

    def get_value_async(
        self,
        cls,
        cmd,
        callback,
        instance: int = 0,
        conversion=None,
        adr=None,
        typechar="?",
        delete=True,
        on_error=None,
        timeout=3.0,
    ):
        """Ask a value to the board from in async way."""
        self.comms.getValueAsync(
            self,
            cls=cls,
            cmd=cmd,
            callback=callback,
            instance=instance,
            conversion=conversion,
            adr=adr,
            typechar=typechar,
            delete=delete,
            on_error=on_error,
            timeout=timeout,
        )

    def get_capabilities(self, cls, callback, instance=0, on_error=None):
        from command_capabilities import parse_capabilities
        key = (cls, instance)
        if key in self.comms.capabilities:
            callback(self.comms.capabilities[key])
            return
        def received(capabilities):
            self.comms.capabilities[key] = capabilities
            callback(capabilities)
        self.get_value_async(cls, 'help', received, instance=instance, typechar='!',
                             conversion=parse_capabilities, on_error=on_error)

    def get_supported_value(self, cls, cmd, callback, instance=0, conversion=None,
                            on_error=None, require_write=False):
        from command_requests import CommandError
        def available(capabilities):
            flags = capabilities.get(cmd, {}).get('flags', set())
            if 'R' not in flags or (require_write and 'W' not in flags):
                (on_error or self.comms._log_error)(CommandError('NOT_FOUND', '{}.{}.{}'.format(cls, instance, cmd)))
                return
            self.get_value_async(cls, cmd, callback, instance=instance,
                                 conversion=conversion, on_error=on_error)
        self.get_capabilities(cls, available, instance, on_error)

    def serial_write_raw(self, cmd):
        """Write a command in direct mode througt serial."""
        self.comms.serialWriteRaw(cmd)

    def send_value(self, cls, cmd, val, adr=None, instance=0, callback=None, on_error=None):
        """Send a value for a specific paramter to the board."""
        self.comms.sendValue(
            self, cls=cls, cmd=cmd, val=val, adr=adr, instance=instance,
            callback=callback, on_error=on_error
        )

    def send_command(self, cls, cmd, instance=0, typechar="?",adr=None):
        """Send one command to the board."""
        self.comms.sendCommand(cls, cmd, instance=instance, typechar=typechar,adr=adr)

    def send_commands(self, cls, cmds, instance=0, typechar="?",adr=None):
        """Send colection of command to the board."""
        for cmd in cmds:
            self.send_command(cls, cmd, instance, typechar, adr)

    def set_value_verified(self, cls, cmd, val, callback, on_error=None, instance=0, adr=None):
        self.comms.setValueVerified(self, cls, cmd, val, callback, on_error, instance, adr)

    def comms_reset(self):
        """Send the reset reply."""
        self.comms.reset()

    def process_virtual_comms_buffer(self, buffer:str):
        """Inject the string buffer in the comms parser and process it as it a board answer."""
        first_end_marker = buffer.find("]")
        first_start_marker = buffer.find("[")
        if first_start_marker < 0 or first_end_marker < first_start_marker:
            return
        match = self.comms.cmdRegex.search(
            buffer, first_start_marker, first_end_marker + 1
        )
        if match:
            self.comms.processMatchedReply(match)

    def get_raw_reply(self):
        """Expose the raw reply pySignal to connect on it."""
        return self.comms.rawReply
