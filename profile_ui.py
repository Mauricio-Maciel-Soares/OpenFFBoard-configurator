"""Profile module.

Regroup all required classes to manage Profiles in UI.

Module : profile_ui
Authors : vincent
"""

import os
import json
import copy
import sys

import PyQt6.QtCore
import PyQt6.QtWidgets
import PyQt6.QtGui
import base_ui
import helper
import profile_store
from profile_transfer import ProfileTransfer

def get_config_dir_path(profiles_filename):
    if sys.platform == "linux" and not os.path.exists(profiles_filename):
        defaultConfigDir = os.path.expandvars("$HOME/.config")
        return os.path.join(os.getenv("XDG_CONFIG_HOME", defaultConfigDir), "openffboard")

    return os.getcwd()

class ProfileUI(base_ui.WidgetUI, base_ui.CommunicationHandler):
    """Manage the Profile selector and the board communication about them."""

    __RELEASE = 2

    __PROFILES_FILENAME = "profiles.json"
    __PROFILES_FILEPATH = os.path.join(get_config_dir_path(__PROFILES_FILENAME), __PROFILES_FILENAME)
    __PROFILESSETUP_FILENAME = helper.res_path("profile.cfg")
    __PROFILES_TEMPLATE = {
        "release": __RELEASE,
        "global":{},
        "profiles": [{"name": "None", "data": []}],
    }
    FLASH_PROFILE_NAME = "Flash profile"
    NONE_PROFILE_NAME = "None"

    profiles_updated_event = PyQt6.QtCore.pyqtSignal(list)
    profile_selected_event = PyQt6.QtCore.pyqtSignal(str)

    def __init__(self, main=None):
        """Initialize profile without UI."""
        self.main = main
        self.profiles_dlg = None  # ProfilesDialog is not initialized yet
        self.profile_setup = {}
        self.profiles = {}

        self._current_class = -1
        self._current_command = -1
        self._current_instance = -1
        self._map_class_running = []
        self._running_profile = []
        self._profilename_tosave: str = None

        self.ui_initialized = False
        self._startup_messages = []
        self._transfer = None
        self._torque_confirmation = None
        self._storage_blocked = False

        self.load_profile_settings()
        self.load_profiles()

    def initialize_ui(self):
        """Init the UI and link the event."""
        base_ui.WidgetUI.__init__(self, self.main, "profile.ui")
        base_ui.CommunicationHandler.__init__(self)
        
        self.profiles_dlg = ProfilesDialog(self)
        self.profiles_dlg.closeSignal.connect(self.close_profile_manager)

        self.pushButton_save.clicked.connect(self.save_clicked)
        icon_save = PyQt6.QtGui.QIcon(
            self.style().standardIcon(PyQt6.QtWidgets.QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self.toolButton_save.setIcon(icon_save)
        self.toolButton_save.clicked.connect(self.save_config_in_profile)

        self.comboBox_profiles.currentIndexChanged.connect(self.apply_config)
        icon_manage = PyQt6.QtGui.QIcon(
            self.style().standardIcon(
                PyQt6.QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView
            )
        )
        self.toolButton_manage.setIcon(icon_manage)
        self.toolButton_manage.clicked.connect(self.open_profile_manager)

        self.profiles_updated_event.connect(self.main.systray.refresh_profile_list)
        self.profile_selected_event.connect(
            self.main.systray.refresh_profile_action_status
        )

        self.ui_initialized = True
        self.setEnabled(False)
        PyQt6.QtCore.QTimer.singleShot(0, self._flush_startup_messages)

    def log(self, message):
        if not self.ui_initialized:
            self._startup_messages.append(message)
        else:
            super().log(message)

    def _flush_startup_messages(self):
        for message in self._startup_messages:
            self.log(message)
        self._startup_messages.clear()

    def save_clicked(self):
        if self._transfer is not None:
            return
        self.set_save_btn(False)
        def failed(error):
            self.log("Flash save failed: " + str(error))
            self.set_save_btn(True)
        def saved(reply):
            self.set_save_btn(True)
            if reply.strip() != "OK":
                self.log("Flash save not confirmed: " + reply)
                return
            self.log("Settings saved in flash")
            self.save_config_in_profile(self.FLASH_PROFILE_NAME)
        self.get_value_async("sys", "save", saved, on_error=failed)

    def onclose(self):
        self._cancel_transfer()
        self.remove_callbacks()

    def _cancel_transfer(self):
        if self._transfer is not None:
            self._transfer.cancel()
            self._transfer = None
            self.remove_callbacks()
            self.log("Profile operation cancelled; already confirmed changes remain on the board")
        if self._torque_confirmation is not None:
            box = self._torque_confirmation
            self._torque_confirmation = None
            box.reject()
            box.deleteLater()
        if self.ui_initialized:
            self._set_busy(False)

    def setEnabled(self, a0: bool) -> None:
        if not a0:
            self._cancel_transfer()
        elif self.comboBox_profiles.count() == 0:
            self.refresh_combox_list()
        return super().setEnabled(a0)

    def _set_busy(self, busy):
        for widget in (self.comboBox_profiles, self.toolButton_save,
                       self.toolButton_manage, self.pushButton_save):
            widget.setEnabled(not busy)

    def set_save_btn(self, status):
        """Enable the save button based on status."""
        self.pushButton_save.setEnabled(status)

    def open_profile_manager(self):
        """Open the profile list manager."""
        self.profiles_dlg.set_profiles(self.profiles)
        self.profiles_dlg.show()

    def close_profile_manager(self, profile_name:str = ''):
        """Close the profile list manager."""
        self.comboBox_profiles.currentIndexChanged.disconnect(self.apply_config)
        self.refresh_combox_list()
        self.comboBox_profiles.currentIndexChanged.connect(self.apply_config)
        if (profile_name):
            self.select_profile(profile_name)
        if self.create_or_update_profile_file():
            self.log('Profile: profiles list updated')

    def load_profile_settings(self):
        """Load the settings for the profile manager in profile.cfg file."""
        check = os.path.exists(self.__PROFILESSETUP_FILENAME)
        if check:
            with open(self.__PROFILESSETUP_FILENAME, "r", encoding="utf_8") as file:
                self.profile_setup = json.load(file)

    def load_profiles(self):
        if os.path.exists(self.__PROFILES_FILEPATH) or os.path.exists(self.__PROFILES_FILEPATH + ".bak"):
            self.load_profiles_from_file()
        else:
            self.create_or_update_profile_file(create=True)

    def load_profiles_from_file(self):
        try:
            self.profiles, recovered = profile_store.load_profiles(self.__PROFILES_FILEPATH)
            self.log("Profile: recovered last good backup" if recovered else "Profile: profiles loaded")
        except (OSError, ValueError) as exc:
            self.profiles = copy.deepcopy(self.__PROFILES_TEMPLATE)
            self._storage_blocked = True
            self.log("Profile: " + str(exc) + ". Repair the file before saving.")
        if self.ui_initialized:
            self.refresh_combox_list()

    def create_or_update_profile_file(self, create=False):
        if self._storage_blocked:
            self.log("Profile: saving blocked to preserve unreadable files")
            return False
        if create:
            self.profiles = copy.deepcopy(self.__PROFILES_TEMPLATE)
            lang = PyQt6.QtCore.QLocale.system().name()
            self.profiles['global']['language'] = lang if os.path.exists(os.path.join('translations', lang + '.qm')) else 'en_US'
        try:
            profile_store.save_profiles(self.__PROFILES_FILEPATH, self.profiles)
            return True
        except (OSError, ValueError) as exc:
            self.log("Profile: save failed: " + str(exc))
            return False

    def get_global_setting(self,key : str, default = None):
        """Returns an entry of the global section or saves a default if set and not found"""
        if "global" in self.profiles:
            if (key not in self.profiles['global']) and default != None:
                self.set_global_setting(key,default)
            return self.profiles['global'].get(key,None)
        return None

    def set_global_setting(self,key : str, entry, save : bool = True):
        """Adds an item to the global section of the profile file. Set save true to write file immediately"""
        self.profiles['global'][key] = entry
        if save:
            self.create_or_update_profile_file()


    def refresh_combox_list(self):
        """Refresh the combo list of profile when init UI or when list change."""
        self.comboBox_profiles.clear()
        listprofile = []
        data = self.profiles["profiles"]
        for profilename in data:
            listprofile.append(profilename["name"])

        try:
            listprofile.index(ProfileUI.FLASH_PROFILE_NAME)
            listprofile.remove(ProfileUI.NONE_PROFILE_NAME)
        except ValueError:
            pass

        self.comboBox_profiles.addItems(listprofile)
        self.profiles_updated_event.emit(listprofile)

        current_name_selected = self.comboBox_profiles.currentText()
        if current_name_selected is not None:
            self.profile_selected_event.emit(current_name_selected)

    def select_profile(self, profilename: str):
        """Change the current selection with the specified profile name."""
        if profilename == "":
            return
        if profilename != str(self.comboBox_profiles.currentText()):
            index = self.comboBox_profiles.findText(
                profilename, PyQt6.QtCore.Qt.MatchFlag.MatchFixedString
            )
            if index >= 0 and index != self.comboBox_profiles.currentIndex():
                self.comboBox_profiles.setCurrentIndex(index)

    def apply_config(self):
        if not self.isEnabled() or self._transfer is not None:
            return
        name = self.comboBox_profiles.currentText()
        profile = next((p for p in self.profiles['profiles'] if p['name'] == name), None)
        if profile is None or name == self.NONE_PROFILE_NAME:
            return
        def done(items, skipped):
            self._transfer = None
            self._set_busy(False)
            if skipped:
                self.log("Profile partially applied; firmware does not support: " + ', '.join(skipped))
                self.profile_selected_event.emit("")
            else:
                self.log("Profile '{}' active: {} settings verified on the board".format(name, len(items)))
                self.profile_selected_event.emit(name)
        self._start_transfer(done, copy.deepcopy(profile['data']))

    def save_config_in_profile(self, profile_name=""):
        if self._transfer is not None:
            return
        name = profile_name or self.comboBox_profiles.currentText()
        if not profile_name and name in (self.NONE_PROFILE_NAME, self.FLASH_PROFILE_NAME):
            self.save_clicked()
            return
        def done(items, skipped):
            self._transfer = None
            self._set_busy(False)
            previous = copy.deepcopy(self.profiles)
            profile = next((p for p in self.profiles['profiles'] if p['name'] == name), None)
            if profile is None:
                profile = {'name': name, 'data': []}
                self.profiles['profiles'].append(profile)
            profile['data'] = items
            if not self.create_or_update_profile_file():
                self.profiles = previous
                return
            self.log("Profile '{}' saved from device readings".format(name))
            if skipped:
                self.log("Unsupported optional settings omitted: " + ', '.join(skipped))
            blocked = self.comboBox_profiles.blockSignals(True)
            self.refresh_combox_list()
            self.select_profile(name)
            self.comboBox_profiles.blockSignals(blocked)
        self._start_transfer(done)

    def _confirm_torque_increase(self, changes, respond):
        """Ask once for all increases, before any setting from the profile is sent."""
        box = PyQt6.QtWidgets.QMessageBox(self.window())
        box.setWindowTitle(self.tr("Confirm torque increase"))
        box.setIcon(PyQt6.QtWidgets.QMessageBox.Icon.Warning)
        box.setTextFormat(PyQt6.QtCore.Qt.TextFormat.PlainText)
        lines = [
            "odrv.{}.maxtorque: {:.2f} -> {:.2f} Nm".format(
                change["instance"], change["current"] / 100.0, change["target"] / 100.0)
            for change in changes
        ]
        box.setText(self.tr("Applying this profile increases the ODrive torque scale:")
                    + "\n\n" + "\n".join(lines) + "\n\n"
                    + self.tr("This can increase wheel force for the same FFB input. Apply the entire profile?"))
        buttons = PyQt6.QtWidgets.QMessageBox.StandardButton
        box.setStandardButtons(buttons.Yes | buttons.No)
        box.setDefaultButton(buttons.No)
        box.setEscapeButton(buttons.No)
        box.setWindowModality(PyQt6.QtCore.Qt.WindowModality.WindowModal)
        self._torque_confirmation = box
        def finished(result):
            if self._torque_confirmation is not box:
                return
            self._torque_confirmation = None
            box.deleteLater()
            respond(result == buttons.Yes.value)
        box.finished.connect(finished)
        box.open()

    def _start_transfer(self, done, data=None):
        self._set_busy(True)
        self.profile_selected_event.emit("")
        def failed(error, count):
            self._transfer = None
            self._set_busy(False)
            if error.code == "CANCELLED" and count == 0:
                self.log("Profile application cancelled; no settings were written")
            else:
                self.log("Profile failed after {} verified changes: {}. Check the board before retrying.".format(count, error))
        self._transfer = ProfileTransfer(self, self.profile_setup, done, failed,
                                         confirm_torque_increase=self._confirm_torque_increase)
        self._transfer.start(data)


class ProfilesDialog(PyQt6.QtWidgets.QDialog):
    """This class manage the dialog box which contain the list profile manager."""

    closeSignal = PyQt6.QtCore.pyqtSignal(str)

    def __init__(self, profile_ui=None):
        """Create and attach the DLG to the profile_ui and store locally the profiles list."""
        PyQt6.QtWidgets.QDialog.__init__(self, profile_ui)
        self.profiles = None
        self.profile_manager_ui = ProfilesManagerUI(self)
        self.layout = PyQt6.QtWidgets.QVBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.addWidget(self.profile_manager_ui)
        self.setLayout(self.layout)
        self.setWindowTitle(self.tr("Profiles manager"))
        self.setModal(True)

    def set_profiles(self, profiles):
        """Update the profiles stored in the DLG."""
        self.profiles = profiles

    def closeEvent(self, a0: PyQt6.QtGui.QCloseEvent) -> None:  # pylint: disable=invalid-name
        """Emit the close signal before to close, this help parent to resfresh combobox."""

        # get the current selection item to push it to the main UI on closing action
        name = None

        if self.profile_manager_ui.selection_model.selection() and \
            len(self.profile_manager_ui.selection_model.selection().indexes()) != 0:
            name = self.profile_manager_ui.selection_model.selection().indexes()[0].data()

        self.closeSignal.emit(name)
        return super().closeEvent(a0)


class ProfilesManagerUI(base_ui.WidgetUI, base_ui.CommunicationHandler):
    """Init the UI in the dialog box to manage profile from a list box."""

    def __init__(self, parent: ProfilesDialog = None):
        """Attach the UI to the dialog box and keep a reference to get profiles."""
        base_ui.WidgetUI.__init__(self, parent, "profile_list.ui")
        base_ui.CommunicationHandler.__init__(self)
        self.profile_dlg = parent
        self.model = PyQt6.QtGui.QStandardItemModel(self.listView)
        self.listView.setModel(self.model)
        self.selection_model = self.listView.selectionModel()
        self.selection_model.selectionChanged.connect(self.onClicked)

        self.pushButton_refresh.clicked.connect(self.read_profiles)
        self.pushButton_close.clicked.connect(self.profile_dlg.close)
        self.pushButton_delete.clicked.connect(self.delete)
        self.pushButton_copyas.clicked.connect(self.copy_as)
        self.pushButton_rename.clicked.connect(self.rename)

    def showEvent(self, a0):  # pylint: disable=invalid-name, unused-argument
        """Init the profile list on the show event."""
        self.read_profiles()

    def onClicked(self, index):  # pylint: disable=invalid-name, unused-argument
        """Activate all button if it's not the "None" or "Flash profile" one."""
        if len(self.selection_model.selection().indexes()) <= 0:
            self.pushButton_delete.setEnabled(False)
            self.pushButton_rename.setEnabled(False)
            self.pushButton_copyas.setEnabled(False)
            return

        item = self.selection_model.selection().indexes()[0]
        if item.data() == ProfileUI.NONE_PROFILE_NAME:
            self.pushButton_delete.setEnabled(False)
            self.pushButton_rename.setEnabled(False)
            self.pushButton_copyas.setEnabled(False)
        elif item.data() == ProfileUI.FLASH_PROFILE_NAME:
            self.pushButton_delete.setEnabled(False)
            self.pushButton_rename.setEnabled(False)
            self.pushButton_copyas.setEnabled(True)
        else:
            self.pushButton_delete.setEnabled(True)
            self.pushButton_rename.setEnabled(True)
            self.pushButton_copyas.setEnabled(True)

    def delete(self):
        """Remove the selected item when click on remove, and refresh profiles list."""
        if len(self.selection_model.selection().indexes()) <= 0:
            return
        item_name = self.selection_model.selection().indexes()[0].data()
        for i in range(len(self.profile_dlg.profiles["profiles"])):
            if self.profile_dlg.profiles["profiles"][i]["name"] == item_name:
                self.profile_dlg.profiles["profiles"].pop(i)
                break
        self.read_profiles()

    def copy_as(self):
        """Prompt the new name, copy the selected item and refresh profiles list."""
        if len(self.selection_model.selection().indexes()) <= 0:
            return
        item_name = self.selection_model.selection().indexes()[0].data()
        name, status = PyQt6.QtWidgets.QInputDialog.getText(self, "Copy as", "new name")
        if status and (name != "") and (name not in self.get_profiles_name()):
            profile_json_entry = next(
                filter(
                    lambda x: x["name"] == item_name,
                    self.profile_dlg.profiles["profiles"],
                ),
                None,
            )
            new_profile = copy.deepcopy(profile_json_entry)
            if profile_json_entry is not None:
                new_profile["name"] = name
                self.profile_dlg.profiles["profiles"].append(new_profile)
        self.read_profiles()

    def rename(self):
        """Prompt the new name, rename the selected item and refresh profiles list."""
        if len(self.selection_model.selection().indexes()) <= 0:
            return
        item_name = self.selection_model.selection().indexes()[0].data()
        name, status = PyQt6.QtWidgets.QInputDialog.getText(
            self, "Copy as", "new name", text=item_name
        )
        if status and (name != "") and (name not in self.get_profiles_name()):
            profile_json_entry = next(
                filter(
                    lambda x: x["name"] == item_name,
                    self.profile_dlg.profiles["profiles"],
                ),
                None,
            )
            if profile_json_entry is not None:
                profile_json_entry["name"] = name
        self.read_profiles()

    def read_profiles(self):
        """Update the list box with element read in the profiles list store in the dialog box."""
        self.model.clear()

        for profile in self.get_profiles_name():
            item = PyQt6.QtGui.QStandardItem(profile)
            item.setEditable(False)
            self.model.appendRow(item)

        item = self.model.item(0, 0)
        index = self.model.indexFromItem(item)
        self.selection_model.select(
            index, PyQt6.QtCore.QItemSelectionModel.SelectionFlag.Select
        )

    def get_profiles_name(self):
        """Create the list of profiles name from the profiles list store in the dialog box."""
        list_name = []
        data = self.profile_dlg.profiles['profiles']
        nb_profile = len(data)
        # return the list of name profile without the None one if there is more than one profile.
        for profile in data:
            list_name.append(profile["name"])

        if nb_profile > 1:
            list_name.remove(ProfileUI.NONE_PROFILE_NAME)

        return list_name
