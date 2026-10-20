from os import path
from functools import wraps
import sys
import time

from PyQt6.QtCore import QObject,QTimer

RESPATH = "res"

def res_path(file,respath=RESPATH):
    if getattr(sys, 'frozen',False) and hasattr(sys, '_MEIPASS'):
        return path.join(sys._MEIPASS,respath,file)
    return path.join(respath,file)

# Parses a classchooser reply into a list of classes and a dict to translate the class ID to (list index,name)
def classlistToIds(dat):
    classes = []
    idToIdx = {}
    if(dat):
        n=0
        for c in dat.split("\n"):
            if(c):
                id,creatable,name = c.split(":",2)
                idToIdx[int(id)] = (n,name)
                classes.append([int(id),name,bool(creatable != "0")])
                n+=1
    return (idToIdx,classes)

def updateClassComboBox(combobox,ids,classes,selected = None):
    """Populates a combobox with entries from a parsed classchooser reply"""
    combobox.clear()
    if(selected != None):
        selected = int(selected)
    for c in classes:
        creatable = c[2] or (selected == int(c[0]))
        combobox.addItem(c[1])
        combobox.model().item(ids[c[0]][0]).setEnabled(creatable)
    
    if(selected in ids and combobox.currentIndex() != ids[selected][0]):
        combobox.setCurrentIndex(ids[selected][0])

def updateListComboBox(combobox,reply,entrySep=',',dataSep=':',lookup = None,dataconv = None,labelconv = None):
    """Populates a combobox with entries formatted as Entrylabel<datasep>data<entrysep>..."""
    combobox.clear()
    if lookup != None:
        lookup.clear()
    i = 0
    for s in reply.split(entrySep):
        if not s:
            continue # empty
        e = s.split(dataSep)
        data = e[1]
        label = e[0]
        if dataconv != None:
            data = dataconv(data)
        if labelconv != None:
            label = labelconv(label)
        combobox.addItem(label,data)
        if lookup != None:
            lookup[data] = i
        i += 1

def splitListReply(reply,itemdelim = ':', entrydelim = '\n'):
    """Splits a reply in default format into a list of lists"""
    return [ line.split(itemdelim) for line in reply.split(entrydelim) if line]


def qtBlockAndCall(object : QObject,function,value):
    object.blockSignals(True)
    function(value)
    object.blockSignals(False)

def throttle(ms):
    """Coalesce rapid edits per widget, using a monotonic clock."""
    def decorator(fn):
        attribute = '_throttle_' + fn.__name__
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            state = getattr(self, attribute, None)
            if state is None:
                timer = QTimer(self if isinstance(self, QObject) else None)
                timer.setSingleShot(True)
                state = {'timer': timer, 'last': float('-inf'), 'pending': None}
                setattr(self, attribute, state)
                def call():
                    pending = state['pending']
                    state['pending'] = None
                    if pending is not None:
                        state['last'] = time.monotonic()
                        fn(self, *pending[0], **pending[1])
                timer.timeout.connect(call)
                state['call'] = call
            state['pending'] = (args, kwargs)
            elapsed = (time.monotonic() - state['last']) * 1000
            state['timer'].stop()
            if elapsed >= ms:
                return state['call']()
            state['timer'].start(max(1, int(ms - elapsed)))
        return wrapper
    return decorator

# Splits a reply in the format "name:value,name2:value2"... into a dict
def map_infostring(repl,type=float):
    return{key:type(value) for (key,value) in [entry.split(":") for entry in repl.split(",")]}