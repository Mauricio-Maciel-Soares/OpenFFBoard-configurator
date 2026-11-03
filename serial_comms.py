import queue,time, traceback, sys, codecs
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication
from collections import deque
import re
import PyQt6.QtSerialPort
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtCore import QTimer
import logging

import weakref
from command_requests import RequestQueue, CommandError, same_value

# Regex group indices:
GRP_CLS         = 0
GRP_INSTANCE    = 1
GRP_CMD         = 2
GRP_TYPE        = 3
GRP_CMDVAL1     = 4
GRP_CMDVAL2     = 5
GRP_REPLY       = 6

class SerialComms(QObject):
    MAX_REQUEST_SIZE = 1024
    MAX_DELAY_SEND_CMD = 30
    MAX_REPLY_BUFFER = 65536

    cmdRegex = re.compile(r"\[(\w+)\.(?:(\d+)\.)?(\w+)([?!=]?)(?:([-+]?\d+))?(?:\?([-+]?\d+))?\|(.*)\]",re.DOTALL)
    callbackDict = {}
    rawReply = pyqtSignal(str)
    _instances = weakref.WeakSet()

    def __init__(self,main,serialport : PyQt6.QtSerialPort.QSerialPort):
        QObject.__init__(self)
        self.serial : PyQt6.QtSerialPort.QSerialPort = serialport 
        self.main=main
        self.serial.readyRead.connect(self.serialReceive)
        self.serial.aboutToClose.connect(self.reset)
        self.replytext = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.logger = logging.getLogger("serial_comms")
        self.send_buffer = []
        self.send_callbacks = []
        self._resetting = False
        self.capabilities = {}
        self.requests = RequestQueue(self.serialWriteRaw, self._log_error, defer_sent=True)
        self.request_timer = QTimer(self)
        self.request_timer.setInterval(100)
        self.request_timer.timeout.connect(self._check_requests)
        self.request_timer.start()
        self.write_timer = QTimer(self)
        self.write_timer.setSingleShot(True)
        self.write_timer.timeout.connect(self._send_over_uart)
        self._instances.add(self)

    def _check_requests(self):
        if self.send_buffer and any(queue and queue[0]['deadline'] is not None
                                    and time.monotonic() >= queue[0]['deadline']
                                    for queue in self.requests.queues.values()):
            self._transport_failed("Serial output stalled; unsent commands discarded")
            return
        self.requests.expire()

    def _transport_failed(self, message):
        self._log_error(message)
        self.reset()
        if hasattr(self.main, 'reset_port'):
            self.main.reset_port(immediate=True)
        else:
            self.serial.close()

    def _log_error(self, error):
        self.logger.warning(str(error))
        self.main.log(str(error))

    @staticmethod
    def registerCallback(handler,cls,cmd,callback,instance=0,conversion=None,adr=None,delete=False,typechar='?'):
        if cls not in SerialComms.callbackDict:
            SerialComms.callbackDict[cls] = []

        callbackObj = {"handler":handler,"callback":callback,"convert":conversion,"instance":instance,"class":cls,"cmd":cmd,"address":adr,"delete":delete,"typechar":typechar}
        if callbackObj not in SerialComms.callbackDict[cls]:
            SerialComms.callbackDict[cls].append(callbackObj)

    @staticmethod
    def removeCallback(handler,cls=None,cmd=None,callback=None,instance=0,adr=None,typechar='?'):
        if cls in SerialComms.callbackDict:
            SerialComms.callbackDict[cls] = [ entry for entry in SerialComms.callbackDict[cls] if entry["handler"] != handler or ( (entry["instance"] not in [instance,None]) or (entry["cmd"] not in [cmd,None]) or (entry["callback"] not in [callback,None]) or (entry["address"] not in [adr,None]) or (entry["typechar"] not in  [typechar,None]))]

    @staticmethod
    def removeCallbacks(handler):
        for comms in list(SerialComms._instances):
            comms.requests.cancel(handler)
        for cls,item in (SerialComms.callbackDict.items()):
            SerialComms.callbackDict[cls] = [ entry for entry in item if entry["handler"] != handler]

    def removeAllCallbacks(self):
        SerialComms.callbackDict.clear()

    def getValueAsync(self,handler,cls,cmd,callback,instance=0,conversion=None,adr=None,
                      typechar='?',delete=True,on_error=None,timeout=3.0):
        typechar = typechar or ''
        if not delete:
            self.registerCallback(handler, cls, cmd, callback, instance, conversion,
                                  adr, False, typechar)
            callback, conversion = None, None
        wire = "{}.{}.{}{}{};".format(cls, instance, cmd, typechar,
                                     '' if adr is None else adr)
        self._request(handler, (cls, instance, cmd, typechar, adr), wire,
                      callback, conversion, on_error, timeout)

    def _request(self, handler, key, wire, callback=None, conversion=None,
                 on_error=None, timeout=3.0, value=None):
        if not self.serial.isOpen() or self._resetting:
            # Avoid reentering widgets while their constructors are still running.
            error = CommandError("DISCONNECTED", wire.rstrip(';'))
            QTimer.singleShot(0, lambda: self.requests._invoke(on_error or self._log_error, error))
            return
        self.requests.add(handler, key, wire, callback, conversion, on_error, timeout, value)

    def sendCommand(self,cls,cmd,instance=0,typechar='?',adr=None):
        key = (cls, instance, cmd, typechar or '', adr)
        if key not in self.requests.queues and key not in self.requests.blocked:
            self.getValueAsync(self, cls, cmd, None, instance, adr=adr, typechar=typechar)

    def sendValue(self,handler,cls,cmd,val,adr=None,instance=0,callback=None,on_error=None):
        wire = "{}.{}.{}={}{};".format(cls, instance, cmd, val,
                                      '' if adr is None else '?' + str(adr))
        self._request(handler, (cls, instance, cmd, '=', adr), wire,
                      callback or self.checkOk, on_error=on_error, value=val)

    def setValueVerified(self,handler,cls,cmd,val,callback,on_error=None,instance=0,adr=None):
        fail = on_error or self._log_error
        command = "{}.{}.{}".format(cls, instance, cmd)
        def read_back(actual):
            if same_value(val, actual):
                callback(actual)
            else:
                fail(CommandError("VERIFY_FAILED", command,
                                  "requested {}; read back {}".format(val, actual)))
        def acknowledged(reply):
            if reply.strip() != "OK":
                fail(CommandError("INVALID_ACK", command, reply))
                return
            self.getValueAsync(handler, cls, cmd, read_back, instance,
                               adr=adr, on_error=fail)
        self.sendValue(handler, cls, cmd, val, adr, instance, acknowledged, fail)

    def reset(self):
        """Drop buffers and pending requests; never replay writes on reconnect."""
        self._resetting = True
        self.replytext = ""
        self.decoder.reset()
        self.send_buffer = []
        self.send_callbacks = []
        self.write_timer.stop()
        self.requests.reset()
        self.capabilities.clear()
        for cls in self.callbackDict:
            self.callbackDict[cls] = [c for c in self.callbackDict[cls] if not c['delete']]
        self._resetting = False

    def checkOk(self,reply):
        if reply.strip() != "OK":
            self._log_error(reply)

    def pack_cmd(self,cmd,on_sent=None):
        # Validate the whole input before changing the queue. Limits are bytes.
        atoms = [(part.strip() + ';').encode('utf-8')
                 for part in re.split(r'[;\r\n]+', cmd) if part.strip()]
        if any(len(atom) > self.MAX_REQUEST_SIZE for atom in atoms):
            raise ValueError("A single command exceeds 1024 UTF-8 bytes")
        if sum(map(len, self.send_buffer)) + sum(map(len, atoms)) > 65536:
            raise ValueError("Serial output queue is full")
        for atom in atoms:
            if self.send_buffer and len(self.send_buffer[-1]) + len(atom) <= self.MAX_REQUEST_SIZE:
                self.send_buffer[-1] += atom
            else:
                self.send_buffer.append(atom)
                self.send_callbacks.append([])
        if on_sent and atoms:
            self.send_callbacks[-1].append((len(self.send_buffer[-1]), on_sent))

    def serialWriteRaw(self,cmdraw,on_sent=None):
        if not self.serial.isOpen() or self._resetting:
            raise OSError("Serial port is closed")
        self.pack_cmd(cmdraw,on_sent)
        if not self.write_timer.isActive():
            self._send_over_uart()

    def _send_over_uart(self):
        if not self.serial.isOpen():
            self.reset()
            return
        if not self.send_buffer:
            return
        data = self.send_buffer[0]
        written = self.serial.write(data)
        if written < 0:
            self._transport_failed("Serial write failed: " + self.serial.errorString())
            return
        callbacks = self.send_callbacks[0]
        ready = [callback for offset, callback in callbacks if offset <= written]
        self.send_callbacks[0] = [(offset - written, callback) for offset, callback in callbacks if offset > written]
        if written < len(data):
            self.send_buffer[0] = data[written:]
        else:
            self.send_buffer.pop(0)
            self.send_callbacks.pop(0)
        for callback in ready:
            callback()
        # Coalesce bursts and allow the firmware to consume each bounded packet.
        self.write_timer.start(self.MAX_DELAY_SEND_CMD)


    def serialReceive(self):
        """Decodes incoming bytes and dispatches every complete reply frame."""
        # Incremental decoding keeps multibyte characters split across reads intact
        self.replytext += self.decoder.decode(self.serial.readAll().data())

        if len(self.replytext) > SerialComms.MAX_REPLY_BUFFER:
            self.logger.error("Reply buffer overflow (%d bytes). Discarding.", len(self.replytext))
            self.replytext = ""
            return

        try:
            while self.replytext:
                firstStartmarker = self.replytext.find("[")
                if firstStartmarker < 0:
                    self.replytext = "" # Nothing left that could start a frame
                    break
                if firstStartmarker > 0:
                    self.replytext = self.replytext[firstStartmarker::] # Drop noise or truncated frames

                firstEndmarker = self.replytext.find("]")
                if firstEndmarker < 0:
                    break # Frame is still incomplete

                match = self.cmdRegex.search(self.replytext,0,firstEndmarker+1)
                if (match):
                    curRepl = self.replytext[1:firstEndmarker]
                    self.replytext = self.replytext[match.end()::] # cut out everything before the end of the match
                    if not self.processMatchedReply(match):
                        self.rawReply.emit(curRepl)
                else:
                    self.rawReply.emit(self.replytext[1:firstEndmarker])
                    self.replytext = self.replytext[firstEndmarker+1::]
        except Exception as e:
            self.logger.error("Can not process reply: %s", e)
            traceback.print_exception(*sys.exc_info())


    def processMatchedReply(self,match):
        groups = match.groups()
        #print(groups)
        cls = groups[GRP_CLS]
        instance = int(groups[GRP_INSTANCE]) if groups[GRP_INSTANCE] else 0
        reply = str(groups[GRP_REPLY])
        typechar = groups[GRP_TYPE] if groups[GRP_TYPE] else ''
        cmd = groups[GRP_CMD]
        deleted = False
        adr = int(groups[GRP_CMDVAL2]) if groups[GRP_CMDVAL2] != None else int(groups[GRP_CMDVAL1]) if groups[GRP_CMDVAL1] != None and typechar == '?' else None  
        val = int(groups[GRP_CMDVAL1]) if groups[GRP_CMDVAL1] != None  else None
    
        callbacks = list(self.callbackDict.get(cls, []))
        key = (cls, instance, cmd, typechar, adr)
        stale = key in self.requests.blocked
        deleted = self.requests.reply(key, reply, val)
        if stale:
            return True
        # Remove one-shots before invoking user code (which may run a nested event loop).
        matching = [c for c in callbacks if c in self.callbackDict.get(cls, [])
                    and c['cmd'] == cmd and c['instance'] in (instance, 0xff)
                    and c['typechar'] in (typechar, None) and c['address'] == adr]
        for entry in matching:
            if entry['delete']:
                self.callbackDict[cls].remove(entry)
                deleted = True
        for entry in matching:
            if reply.strip().upper() in ("ERR", "NOT_FOUND", "BUSY"):
                self.logger.warning("%s.%s: %s", cls, cmd, reply)
                continue
            try:
                value = entry['convert'](reply) if entry['convert'] else reply
                entry['callback'](value)
            except Exception as exc:
                self.logger.error("Callback failed for %s.%s: %s", cls, cmd, exc)
                if entry in self.callbackDict.get(cls, []):
                    self.callbackDict[cls].remove(entry)
        return deleted
