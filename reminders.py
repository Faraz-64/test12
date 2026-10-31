"""Reminders and timers: natural-language parsing, a JSON store that survives restarts, and a background scheduler.

Limits (also in the README): reminders only ring while Jarvis is running. Anything that came due while it was closed,
or while the PC was asleep, is announced as 'missed' the next time Jarvis checks (at start-up or on wake).
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path


log = logging.getLogger("jarvis.reminders")
log.addHandler(logging.NullHandler())  # silent unless Jarvis sets up a log file


class ReminderError(ValueError):
    pass


_UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)"
_NUM = r"(\d+(?:\.\d+)?)"
_END = r"[\s.!]*$"
MAX_SECONDS = 365 * 86400
LATE_AFTER_SECONDS = 30  # due this long ago when first noticed = "missed while Jarvis was closed"

_TIMER = [
    re.compile(r"^(?:please\s+)?(?:set|start|create)\s+(?:me\s+)?(?:an?\s+)?" + _NUM + r"[\s-]*" + _UNIT + r"\s+timer(?:\s+(?:for|called|named|to)\s+(.+?))?" + _END, re.I),
    re.compile(r"^(?:please\s+)?(?:set|start|create)\s+(?:an?\s+)?timer\s+(?:for|of)\s+" + _NUM + r"\s*" + _UNIT + r"(?:\s+(?:for|called|named|to)\s+(.+?))?" + _END, re.I),
    re.compile(r"^timer\s+(?:for\s+)?" + _NUM + r"\s*" + _UNIT + r"(?:\s+(?:for|called|named|to)\s+(.+?))?" + _END, re.I),
]
_REL_FIRST = re.compile(r"^(?:please\s+)?remind me\s+(?:in|after)\s+" + _NUM + r"\s*" + _UNIT + r"\s+(?:to|that|about)\s+(.+?)" + _END, re.I)
_REL_LAST = re.compile(r"^(?:please\s+)?remind me\s+(?:to|that|about)\s+(.+?)\s+(?:in|after)\s+" + _NUM + r"\s*" + _UNIT + _END, re.I)
_CLOCK = r"(?:(tomorrow)\s+)?(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_ABS_FIRST = re.compile(r"^(?:please\s+)?remind me\s+" + _CLOCK + r"\s+(?:to|that|about)\s+(.+?)" + _END, re.I)
_ABS_LAST = re.compile(r"^(?:please\s+)?remind me\s+(?:to|that|about)\s+(.+?)\s+" + _CLOCK + _END, re.I)

_LIST = re.compile(r"^(?:please\s+)?(?:(?:list|show|view)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?(?:reminders|timers)|what (?:reminders|timers) do i have)" + _END, re.I)
_CANCEL = re.compile(r"^(?:please\s+)?(?:cancel|delete|remove)\s+(?:the\s+)?(?:reminder|timer)\s+#?(\d+)" + _END, re.I)
_DONE = [re.compile(r"^(?:please\s+)?(?:i(?:'m| am)\s+)?done with\s+(?:reminder|timer)\s+#?(\d+)" + _END, re.I),
         re.compile(r"^(?:please\s+)?(?:mark\s+)?(?:reminder|timer)\s+#?(\d+)\s+(?:as\s+)?(?:done|complete|completed|finished)" + _END, re.I),
         re.compile(r"^(?:please\s+)?(?:complete|finish|mark)\s+(?:reminder|timer)\s+#?(\d+)(?:\s+as\s+(?:done|complete))?" + _END, re.I)]


def _seconds(amount, unit):
    secs = float(amount) * _UNITS[unit.lower().rstrip("s")]
    if secs <= 0:
        raise ReminderError("the time has to be more than zero")
    if secs > MAX_SECONDS:
        raise ReminderError("that's more than a year away; I only keep reminders up to 365 days")
    return secs


def _clock_due(now, tomorrow, hour, minute, ampm):
    hour, minute = int(hour), int(minute or 0)
    if ampm:
        if not 1 <= hour <= 12:
            raise ReminderError(f"'{hour}{ampm.lower()}' isn't a valid time")
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ReminderError(f"{hour}:{minute:02d} isn't a valid time")
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow:
        due += timedelta(days=1)
    elif due <= now:
        due += timedelta(days=1)  # "at 9am" said at 10am means tomorrow
    return due


def parse(text, now=None):
    """Turn a sentence into {'kind', 'text', 'due'}, or None if it isn't a reminder request. Raises ReminderError if it is
    one but the numbers are invalid (0 minutes, 25:99, 5000 days...)."""
    now = now or datetime.now()
    t = text.strip()
    for rx in _TIMER:
        m = rx.match(t)
        if m:
            amount, unit, label = m.groups()
            return {"kind": "timer", "text": (label or f"{amount} {unit} timer").strip(), "due": now + timedelta(seconds=_seconds(amount, unit))}
    m = _REL_FIRST.match(t)
    if m:
        amount, unit, what = m.groups()
        return {"kind": "reminder", "text": what.strip(), "due": now + timedelta(seconds=_seconds(amount, unit))}
    m = _REL_LAST.match(t)
    if m:
        what, amount, unit = m.groups()
        return {"kind": "reminder", "text": what.strip(), "due": now + timedelta(seconds=_seconds(amount, unit))}
    m = _ABS_FIRST.match(t)
    if m:
        tomorrow, h, mi, ap, what = m.groups()
        return {"kind": "reminder", "text": what.strip(), "due": _clock_due(now, tomorrow, h, mi, ap)}
    m = _ABS_LAST.match(t)
    if m:
        what, tomorrow, h, mi, ap = m.groups()
        return {"kind": "reminder", "text": what.strip(), "due": _clock_due(now, tomorrow, h, mi, ap)}
    return None


def parse_management(text):
    """('list',) | ('cancel', id) | ('done', id) | None"""
    t = text.strip()
    if _LIST.match(t):
        return ("list",)
    m = _CANCEL.match(t)
    if m:
        return ("cancel", int(m.group(1)))
    for rx in _DONE:
        m = rx.match(t)
        if m:
            return ("done", int(m.group(1)))
    return None


def _fmt_delta(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {secs:02d}s" if mins < 10 else f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 48:
        return f"{hours}h {mins:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def describe(item, now=None):
    now = now or datetime.now()
    due = datetime.fromisoformat(item["due"])
    if item["status"] == "fired":
        when = "went off" + (" while Jarvis was closed" if item.get("late") else "") + f" at {due:%H:%M} (say 'done with reminder {item['id']}' to clear it)"
    else:
        when = f"in {_fmt_delta((due - now).total_seconds())} ({due:%a %H:%M})"
    return f"{item['id']}. [{item['kind']}] {item['text']}: {when}"


class ReminderStore:
    """JSON file of reminders. Every change is written to disk straight away (atomically), so nothing is lost on a crash."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.warning = None
        self.data = {"next_id": 1, "items": []}
        self._load()

    def _load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            d = json.loads(raw)
            if not isinstance(d, dict) or not isinstance(d.get("items"), list):
                raise ValueError("unexpected layout")
            for it in d["items"]:
                datetime.fromisoformat(it["due"])
                it["id"], it["text"], it["status"], it["kind"]
            d["next_id"] = int(d.get("next_id", 1 + max([i["id"] for i in d["items"]], default=0)))
            self.data = d
        except (ValueError, KeyError, TypeError):
            backup = self.path.with_suffix(f".corrupt-{datetime.now():%Y%m%d%H%M%S}")
            self.path.replace(backup)  # keep the damaged file instead of overwriting it
            self.warning = f"{self.path.name} was unreadable; I kept it as {backup.name} and started a fresh list."

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, kind, text, due, now=None):
        with self.lock:
            item = {"id": self.data["next_id"], "kind": kind, "text": text, "due": due.isoformat(timespec="seconds"),
                    "created": (now or datetime.now()).isoformat(timespec="seconds"), "status": "pending", "late": False}
            self.data["next_id"] += 1
            self.data["items"].append(item)
            self._save()
            return dict(item)

    def active(self):
        with self.lock:
            return sorted((dict(i) for i in self.data["items"] if i["status"] in ("pending", "fired")), key=lambda i: i["due"])

    def _get(self, rid):
        for i in self.data["items"]:
            if i["id"] == rid and i["status"] in ("pending", "fired"):
                return i
        raise ReminderError(f"I can't find an active reminder number {rid}. Say 'list reminders' to see them.")

    def cancel(self, rid):
        with self.lock:
            item = self._get(rid)
            item["status"] = "cancelled"
            self._save()
            return dict(item)

    def complete(self, rid):
        with self.lock:
            item = self._get(rid)
            item["status"] = "done"
            self._save()
            return dict(item)

    def take_due(self, now):
        """Mark everything that has come due as 'fired' (saved before returning, so a restart can't repeat it)."""
        with self.lock:
            fired = []
            for i in self.data["items"]:
                if i["status"] == "pending" and datetime.fromisoformat(i["due"]) <= now:
                    i["status"] = "fired"
                    i["late"] = (now - datetime.fromisoformat(i["due"])).total_seconds() > LATE_AFTER_SECONDS
                    fired.append(dict(i))
            if fired:
                self._save()
            return fired


class ReminderScheduler:
    """Background thread that polls the store and calls notify(item) when something is due."""

    def __init__(self, store, notify, poll_seconds=1.0, clock=datetime.now):
        self.store, self.notify, self.poll, self.clock = store, notify, poll_seconds, clock
        self._stop = threading.Event()
        self.thread = None

    def check_once(self):
        fired = self.store.take_due(self.clock())
        for item in fired:
            try:
                self.notify(item)
            except Exception:  # a broken notifier must never kill the scheduler, but the failure must not be silent either
                log.exception("could not show reminder #%s (%s)", item.get("id"), item.get("text"))
        return fired

    def _run(self):
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(self.poll)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, name="reminder-scheduler", daemon=True)
        self.thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        if self.thread:
            self.thread.join(timeout)
        return not (self.thread and self.thread.is_alive())
