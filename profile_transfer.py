"""Sequential profile capture/application; all writes require device readback."""
import re
from command_requests import CommandError


class ProfileTransfer:
    def __init__(self, io, setup, done, failed):
        self.io, self.setup, self.done, self.failed = io, setup, done, failed
        self.cancelled = False
        self.applied = 0
        self.skipped = []

    def cancel(self):
        self.cancelled = True

    def _fail(self, error):
        if not self.cancelled:
            self.cancelled = True
            self.failed(error, self.applied)

    def _active(self, reply):
        active = set()
        for line in reply.splitlines():
            if line.strip():
                name, cls, instance = line.rsplit(":", 2)
                active.add((name, cls, int(instance)))
        return active

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
                self.index = 0
                self._write_next()
            return
        item = self.items[self.index]
        def received(value):
            if self.cancelled:
                return
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
