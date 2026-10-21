"""Sequential profile capture/application; all writes require device readback."""
import re
from command_requests import CommandError


class ProfileTransfer:
    def __init__(self, io, setup, done, failed, confirm_torque_increase=None):
        self.io, self.setup, self.done, self.failed = io, setup, done, failed
        self.confirm_torque_increase = confirm_torque_increase
        self.cancelled = False
        self.applied = 0
        self.skipped = []
        self.torque_increases = []
        self.awaiting_confirmation = False

    def cancel(self):
        self.cancelled = True
        self.awaiting_confirmation = False

    def _fail(self, error):
        if not self.cancelled:
            self.cancelled = True
            self.awaiting_confirmation = False
            self.failed(error, self.applied)

    def _active(self, reply):
        active = set()
        for line in reply.splitlines():
            if not line.strip():
                continue
            # Current firmware: name:class:instance:class_id:handler_id.
            # Older firmware/profile tests may provide only the first three.
            fields = line.strip().split(":")
            if len(fields) not in (3, 5):
                raise ValueError("Invalid active class: " + line)
            name, cls, instance = fields[:3]
            instance = int(instance)
            if not name or not cls or not 0 <= instance <= 255:
                raise ValueError("Invalid active class: " + line)
            if len(fields) == 5:
                int(fields[3])
                int(fields[4])
            active.add((name, cls, instance))
        return active

    @staticmethod
    def _is_torque_scale(item):
        return item["cls"] == "odrv" and item["cmd"] == "maxtorque"

    @staticmethod
    def _torque_value(value):
        # CAN ODrive in the supported firmware uses 0.01 Nm, capped at 0xfff.
        text = str(value).strip()
        if not re.fullmatch(r"[-+]?\d+", text):
            raise ValueError("ODrive maxtorque must be an integer in 0.01 Nm")
        value = int(text)
        if not 0 <= value <= 0xfff:
            raise ValueError("ODrive maxtorque must be between 0 and 4095 (0..40.95 Nm)")
        return value

    def start(self, profile=None):
        self.profile = profile
        self.io.get_value_async("sys", "lsactive", self._prepare, on_error=self._fail)

    def _prepare(self, reply):
        if self.cancelled:
            return
        try:
            active = self._active(reply)
            allowed = {(c["fullname"], c["classname"], key): key in c.get("optional", [])
                       for c in self.setup["callOrder"] for key in c["key"]}
            if self.profile is None:
                self.items = [dict(fullname=c["fullname"], cls=c["classname"], instance=i, cmd=key)
                              for c in self.setup["callOrder"]
                              for name, cls, i in sorted(active)
                              if (name, cls) == (c["fullname"], c["classname"])
                              for key in c["key"]]
            else:
                self.items = [dict(item) for item in self.profile]
                seen = set()
                for item in self.items:
                    identity = (item["fullname"], item["cls"], item["instance"])
                    key = identity + (item["cmd"],)
                    if identity not in active or (identity[0], identity[1], item["cmd"]) not in allowed:
                        raise ValueError("Inactive or unsupported setting: " + str(key))
                    if key in seen or not re.fullmatch(r"[-+]?\d+", str(item["value"]).strip()):
                        raise ValueError("Duplicate setting or invalid numeric value: " + str(key))
                    if self._is_torque_scale(item):
                        self._torque_value(item["value"])
                    seen.add(key)
            if not self.items:
                raise ValueError("No applicable profile settings")
            self.optional = allowed
            self.index = 0
            self.supported = []
            self._read_next()
        except (ValueError, KeyError, TypeError) as exc:
            self._fail(CommandError("INVALID_PROFILE", "profile", str(exc)))

    def _read_next(self):
        if self.cancelled:
            return
        if self.index == len(self.items):
            if self.profile is None:
                self.done(self.supported, self.skipped)
            else:
                self._authorize_writes()
            return
        item = self.items[self.index]
        def received(value):
            if self.cancelled:
                return
            if self._is_torque_scale(item):
                try:
                    current = self._torque_value(value)
                except (ValueError, TypeError) as exc:
                    self._fail(CommandError("INVALID_REPLY", "odrv.{}.maxtorque".format(item["instance"]), str(exc)))
                    return
                if self.profile is not None:
                    target = self._torque_value(item["value"])
                    if target > current:
                        self.torque_increases.append(dict(instance=item["instance"],
                                                          current=current, target=target))
            if self.profile is None:
                item["value"] = value
            self.supported.append(item)
            self.index += 1
            self._read_next()
        def failed(error):
            if self.cancelled:
                return
            if error.code == "NOT_FOUND" and self.optional.get((item["fullname"], item["cls"], item["cmd"])):
                self.skipped.append("{}.{}.{}".format(item["cls"], item["instance"], item["cmd"]))
                self.index += 1
                self._read_next()
            else:
                self._fail(error)
        if self.optional.get((item['fullname'], item['cls'], item['cmd'])):
            self.io.get_supported_value(item['cls'], item['cmd'], received, instance=item['instance'],
                                        on_error=failed, require_write=self.profile is not None)
        else:
            self.io.get_value_async(item['cls'], item['cmd'], received, instance=item['instance'], on_error=failed)

    def _authorize_writes(self):
        if self.cancelled:
            return
        if not self.torque_increases:
            self.index = 0
            self._write_next()
            return
        if self.confirm_torque_increase is None:
            self._fail(CommandError("CONFIRMATION_REQUIRED", "profile",
                                    "Increasing ODrive torque scale requires confirmation"))
            return
        self.awaiting_confirmation = True
        def decided(approved):
            # Closing/disconnecting or a second answer must never start writes.
            if self.cancelled or not self.awaiting_confirmation:
                return
            self.awaiting_confirmation = False
            if approved is not True:
                self._fail(CommandError("CANCELLED", "profile", "Torque increase declined"))
                return
            self.index = 0
            self._write_next()
        self.confirm_torque_increase([dict(change) for change in self.torque_increases], decided)

    def _write_next(self):
        if self.cancelled:
            return
        if self.index == len(self.supported):
            self.done(self.supported, self.skipped)
            return
        item = self.supported[self.index]
        def verified(value):
            if self.cancelled:
                return
            self.applied += 1
            self.index += 1
            self._write_next()
        self.io.set_value_verified(item["cls"], item["cmd"], item["value"], verified,
                                   on_error=self._fail, instance=item["instance"])
