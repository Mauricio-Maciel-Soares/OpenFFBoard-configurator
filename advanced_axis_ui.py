"""Capability-probed axis settings with acknowledged, verified writes."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
                            QLabel, QDoubleSpinBox, QCheckBox, QPushButton)
from base_ui import CommunicationHandler
from slew_profiles_ui import SlewProfilesDialog


class AdvancedAxisDialog(QDialog, CommunicationHandler):
    def __init__(self, parent, axis):
        super().__init__(parent)
        self.axis = axis
        self.generation = 0
        self.values = {}
        self.profiles_dialog = None
        self.setWindowTitle("Advanced axis settings — axis {}".format(axis))
        self.resize(540, 290)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.rate = QDoubleSpinBox(self)
        self.rate.setDecimals(0)
        self.rate.setRange(0, 4294967295)
        self.rate.setSuffix(" counts/ms")
        self.rate.setSpecialValueText("Disabled (0)")
        self.disable_rate = QPushButton("Disable limit", self)
        self.disable_rate.clicked.connect(lambda: self.rate.setValue(0))
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate)
        rate_row.addWidget(self.disable_rate)
        form.addRow("Torque rate limit", rate_row)
        self.encvel = QCheckBox("Prefer fresh encoder velocity", self)
        form.addRow(self.encvel)
        layout.addLayout(form)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        note = QLabel("Values are read back from the board after Apply. Persistence across power cycles "
                      "requires firmware that saves these settings; the custom persistence firmware does so automatically.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.refresh = QPushButton("Read from board", self)
        self.apply_button = QPushButton("Apply and verify", self)
        self.refresh.clicked.connect(self.read_values)
        self.apply_button.clicked.connect(self.apply_values)
        buttons.addWidget(self.refresh)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        self.profiles_button = QPushButton('Persistent slew profiles', self)
        self.profiles_button.clicked.connect(self.open_profiles)
        layout.addWidget(self.profiles_button)

    def open_profiles(self):
        if self.profiles_dialog is None:
            self.profiles_dialog = SlewProfilesDialog(self, self.axis)
            self.profiles_dialog.finished.connect(lambda _: self.read_values() if self.isVisible() else None)
        self.profiles_dialog.show()
        self.profiles_dialog.raise_()

    def showEvent(self, event):
        self.read_values()

    def hideEvent(self, event):
        self.generation += 1
        self.remove_callbacks()
        if self.profiles_dialog is not None:
            self.profiles_dialog.close()

    def _busy(self, busy):
        self.profiles_button.setEnabled(not busy)
        self.refresh.setEnabled(not busy)
        self.apply_button.setEnabled(not busy and bool(self.values))
        self.rate.setEnabled(not busy and 'maxtorquerate' in self.values)
        self.disable_rate.setEnabled(self.rate.isEnabled())
        self.encvel.setEnabled(not busy and 'encvel' in self.values)

    def read_values(self):
        self.generation += 1
        generation = self.generation
        self.values = {}
        self.encvel.hide()
        self._busy(True)
        self.status.setText("Reading supported settings...")
        remaining = {'maxtorquerate', 'encvel'}
        messages = []
        def finish(cmd):
            remaining.discard(cmd)
            if not remaining:
                self._busy(False)
                self.status.setText("; ".join(messages) if messages else "Values read from the board.")
        def received(cmd, value):
            if generation != self.generation:
                return
            if (cmd == 'encvel' and value not in (0, 1)) or not 0 <= value <= 4294967295:
                messages.append(cmd + ": invalid device value")
            else:
                self.values[cmd] = value
                if cmd == 'encvel':
                    self.encvel.setChecked(bool(value))
                    self.encvel.show()
                else:
                    self.rate.setValue(value)
            finish(cmd)
        def failed(cmd, error):
            if generation != self.generation:
                return
            messages.append(cmd + (": not supported by this firmware" if error.code == 'NOT_FOUND' else ": " + str(error)))
            finish(cmd)
        for cmd in tuple(remaining):
            self.get_supported_value('axis', cmd, lambda v, c=cmd: received(c, v), self.axis, int,
                                     require_write=True,
                                 on_error=lambda e, c=cmd: failed(c, e))

    def apply_values(self):
        desired = {'maxtorquerate': int(self.rate.value()), 'encvel': int(self.encvel.isChecked())}
        changes = [(cmd, desired[cmd]) for cmd, value in self.values.items() if desired[cmd] != value]
        if not changes:
            self.status.setText("No changes to apply.")
            return
        self._busy(True)
        generation = self.generation
        def failed(error):
            if generation == self.generation:
                self.status.setText("Change not confirmed: " + str(error) + ". Read from board before retrying.")
                self.values = {}
                self._busy(False)
        def advance(_=None):
            if generation != self.generation:
                return
            if not changes:
                self.status.setText("All changes confirmed by device readback.")
                self._busy(False)
                return
            cmd, value = changes.pop(0)
            self.status.setText("Applying and verifying " + cmd + "...")
            def verified(actual):
                if generation == self.generation:
                    self.values[cmd] = int(actual)
                    advance()
            self.set_value_verified('axis', cmd, value, verified, failed, instance=self.axis)
        advance()
