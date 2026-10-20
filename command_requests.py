"""Bounded requests for a protocol without transaction IDs.

Only one request per wire key is sent at a time. A timed-out key is blocked
until its late reply is drained or the connection is reset: replaying writes or
assigning a delayed answer to a newer request could silently change settings.
"""
import time
from collections import deque
from decimal import Decimal, InvalidOperation


class CommandError(Exception):
    def __init__(self, code, command, detail=""):
        self.code, self.command = code, command
        super().__init__("{}: {}{}".format(command, code, ": " + detail if detail else ""))


def same_value(expected, actual):
    try:
        a, b = Decimal(str(expected).strip()), Decimal(str(actual).strip())
        return a.is_finite() and b.is_finite() and a == b
    except InvalidOperation:
        return str(expected).strip() == str(actual).strip()


class RequestQueue:
    MAX_PENDING = 256

    def __init__(self, send, log, clock=time.monotonic, defer_sent=False):
        self.send, self.log, self.clock = send, log, clock
        self.defer_sent = defer_sent
        self.queues = {}
        self.blocked = set()
        self.finishing = set()
        self.timeouts = 0
        self.errors = 0

    def add(self, handler, key, wire, callback=None, convert=None,
            on_error=None, timeout=3.0, value=None):
        request = dict(handler=handler, key=key, wire=wire, callback=callback,
                       convert=convert, on_error=on_error, timeout=timeout,
                       value=value, deadline=None, sent=False)
        if key in self.blocked:
            self._error(request, "UNSYNCHRONIZED", "waiting for a late reply; reconnect if needed")
        elif sum(map(len, self.queues.values())) >= self.MAX_PENDING:
            self._error(request, "BUSY", "request queue is full")
        else:
            self.queues.setdefault(key, deque()).append(request)
            self._start(key)

    def _start(self, key):
        queue = self.queues.get(key)
        if not queue or key in self.finishing or queue[0]["deadline"] is not None:
            return
        req = queue[0]
        req["deadline"] = self.clock() + req["timeout"]
        try:
            if self.defer_sent:
                self.send(req['wire'], lambda: req.update(sent=True))
            else:
                req['sent'] = True
                self.send(req["wire"])
        except (OSError, ValueError) as exc:
            self._fail_key(key, "TRANSPORT", str(exc), block=False)

    def _invoke(self, callback, value):
        if callback is not None:
            try:
                callback(value)
            except Exception as exc:
                self.log("Callback failed: " + str(exc))

    def _error(self, req, code, detail=""):
        self.errors += 1
        error = CommandError(code, req["wire"].rstrip(";"), detail)
        self._invoke(req["on_error"] or self.log, error)

    def _fail_key(self, key, code, detail="", block=True):
        pending = self.queues.pop(key, ())
        if block:
            self.blocked.add(key)
        for req in pending:
            self._error(req, code, detail)

    def expire(self):
        now = self.clock()
        for key, queue in list(self.queues.items()):
            if queue and queue[0]["deadline"] is not None and now >= queue[0]["deadline"]:
                self.timeouts += 1
                self._fail_key(key, "TIMEOUT", "device did not reply within the deadline")

    def reply(self, key, reply, echoed_value=None):
        if key in self.blocked:
            self.blocked.remove(key)
            return True  # Drain stale answer; never present it as a new result.
        queue = self.queues.get(key)
        if not queue:
            return False
        req = queue[0]
        if not req['sent']:
            return False  # A nested event loop cannot satisfy a request not yet sent.
        if key[3] == "=" and not same_value(req["value"], echoed_value):
            return False
        queue.popleft()
        if not queue:
            del self.queues[key]
        self.finishing.add(key)
        try:
            if reply.strip().upper() in ("ERR", "NOT_FOUND", "BUSY"):
                self._error(req, reply.strip().upper())
            else:
                try:
                    value = req["convert"](reply) if req["convert"] else reply
                except (ValueError, TypeError, OverflowError) as exc:
                    self._error(req, "INVALID_REPLY", str(exc))
                else:
                    self._invoke(req["callback"], value)
        finally:
            self.finishing.discard(key)
            self._start(key)
        return True

    def cancel(self, handler):
        for key, queue in list(self.queues.items()):
            if queue[0]["handler"] is handler:
                # A reply for the cancelled request may still arrive.
                self._fail_key(key, "CANCELLED")
            else:
                self.queues[key] = deque(r for r in queue if r["handler"] is not handler)

    def reset(self):
        pending = self.queues
        self.queues = {}
        self.blocked.clear()
        self.finishing.clear()
        for queue in pending.values():
            for req in queue:
                self._error(req, "DISCONNECTED", "connection closed; command was not replayed")
