"""Safe Windows PC controls: volume, mute, screenshots, lock screen.

Nothing here runs shell commands or accepts free text: every function takes validated numbers/paths, and every action is
checked afterwards (volume is read back, the screenshot file is re-opened) so Jarvis only says "done" when it is.
"""
import ctypes
import platform
import re
from datetime import datetime
from pathlib import Path


class PCError(Exception):
    pass


def _need_windows(what):
    if platform.system() != "Windows":
        raise PCError(f"{what} only works on Windows (this computer is {platform.system()}).")


class WindowsVolume:
    """Master volume through pycaw (pip install pycaw). Handles both the old and new pycaw APIs."""

    def __init__(self):
        _need_windows("Volume control")
        try:
            from pycaw.pycaw import AudioUtilities
        except ImportError:
            raise PCError("volume control needs pycaw: pip install pycaw")
        try:
            dev = AudioUtilities.GetSpeakers()
            if hasattr(dev, "EndpointVolume"):  # newer pycaw
                self._ep = dev.EndpointVolume
            else:  # older pycaw
                from ctypes import POINTER, cast
                from comtypes import CLSCTX_ALL
                from pycaw.pycaw import IAudioEndpointVolume
                self._ep = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
        except Exception as e:
            raise PCError(f"I couldn't reach the Windows audio device ({e}).")

    def get(self):
        return round(self._ep.GetMasterVolumeLevelScalar() * 100)

    def set(self, level):
        self._ep.SetMasterVolumeLevelScalar(level / 100, None)

    def get_mute(self):
        return bool(self._ep.GetMute())

    def set_mute(self, flag):
        self._ep.SetMute(1 if flag else 0, None)


def get_volume(backend):
    return backend.get()


def set_volume(backend, level):
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
        raise PCError("volume has to be a whole number from 0 to 100")
    backend.set(level)
    actual = backend.get()
    if abs(actual - level) > 2:  # read it back: don't claim success if Windows disagrees
        raise PCError(f"I asked Windows for {level}% but it reports {actual}%")
    return actual


def change_volume(backend, delta):
    if isinstance(delta, bool) or not isinstance(delta, int) or abs(delta) > 100:
        raise PCError("the volume step has to be a whole number up to 100")
    return set_volume(backend, max(0, min(100, backend.get() + delta)))


def set_mute(backend, flag):
    backend.set_mute(bool(flag))
    if backend.get_mute() != bool(flag):
        raise PCError("Windows didn't " + ("mute" if flag else "unmute") + " the sound")
    return bool(flag)


def _grab_screen():
    _need_windows("Screenshots")
    try:
        from PIL import ImageGrab
    except ImportError:
        raise PCError("screenshots need Pillow: pip install pillow")
    return ImageGrab.grab(all_screens=True)


def screenshot(directory, grab=None, now=datetime.now):
    """Save the whole screen as a PNG in `directory`. Returns the saved path after re-opening the file to check it."""
    img = (grab or _grab_screen)()
    d = Path(directory)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PCError(f"I couldn't create the screenshot folder ({e}).")
    path = d / f"screenshot-{now():%Y%m%d-%H%M%S}.png"
    n = 1
    while path.exists():  # two shots in the same second must not overwrite each other
        path = d / f"screenshot-{now():%Y%m%d-%H%M%S}-{n}.png"
        n += 1
    try:
        img.save(path, "PNG")
        from PIL import Image
        with Image.open(path) as check:
            check.verify()
        if path.stat().st_size == 0:
            raise OSError("empty file")
    except Exception as e:
        raise PCError(f"the screenshot wasn't saved properly ({type(e).__name__}: {e}).")
    return path


def lock_screen(lock_fn=None):
    """Ask Windows to lock the workstation. Windows reports whether it accepted the request; it cannot be checked beyond that."""
    if lock_fn is None:
        _need_windows("Locking the screen")
        lock_fn = ctypes.windll.user32.LockWorkStation
    if not lock_fn():
        raise PCError("Windows refused the lock request")
    return "Windows accepted the lock request."


_END = r"[\s.!]*$"
_PCWORDS = r"(?:the\s+)?(?:system\s+|pc\s+|computer\s+|laptop\s+)?"
_VOL_TO = re.compile(r"^(?:please\s+)?(?:(?:set|change|turn|make|put)\s+)?" + _PCWORDS + r"volume\s+(?:to|at)\s+(\d{1,4})\s*(?:%|percent)?" + _END, re.I)
_VOL_NUM = re.compile(r"^volume\s+(\d{1,4})\s*(?:%|percent)?" + _END, re.I)
_VOL_UPDOWN = re.compile(r"^(?:please\s+)?(?:(?:turn|make|bring|put)\s+)?" + _PCWORDS + r"volume\s+(up|down)" + _END, re.I)
_VOL_VERB = re.compile(r"^(?:please\s+)?(increase|raise|lower|decrease|reduce)\s+" + _PCWORDS + r"volume" + _END, re.I)
_LOUDER = re.compile(r"^(?:please\s+)?(?:make it\s+)?(louder|quieter)" + _END, re.I)
_VOL_GET = re.compile(r"^(?:what(?:'s| is)|show|tell me)\s+(?:the\s+)?(?:current\s+|system\s+)*volume\??" + _END + r"|^how loud is it\??" + _END, re.I)
_MUTE = re.compile(r"^(?:please\s+)?(un)?mute(?:\s+(?:the\s+)?(?:sound|volume|audio|pc|computer|laptop|system))?" + _END, re.I)
_SHOT = re.compile(r"^(?:please\s+)?(?:take|capture|grab|make)\s+(?:me\s+)?(?:a\s+)?(?:screenshot|screen\s?shot|screen capture|screengrab)(?:\s+please)?" + _END + r"|^screenshot" + _END, re.I)
_LOCK = re.compile(r"^(?:please\s+)?lock\s+(?:my\s+|the\s+)?(?:screen|pc|computer|laptop|workstation)" + _END, re.I)


def parse_command(text):
    """Everyday phrase -> (tool_name, arguments) or None. Out-of-range numbers are passed through on purpose, so the tool
    validation can refuse them with a clear message instead of silently changing what you asked for."""
    t = text.strip()
    for rx in (_VOL_TO, _VOL_NUM):
        m = rx.match(t)
        if m:
            return "set_volume", {"action": "set", "level": int(m.group(1))}
    m = _VOL_UPDOWN.match(t)
    if m:
        return "set_volume", {"action": m.group(1).lower()}
    m = _VOL_VERB.match(t)
    if m:
        return "set_volume", {"action": "up" if m.group(1).lower() in ("increase", "raise") else "down"}
    m = _LOUDER.match(t)
    if m:
        return "set_volume", {"action": "up" if m.group(1).lower() == "louder" else "down"}
    if _VOL_GET.match(t):
        return "set_volume", {"action": "get"}
    m = _MUTE.match(t)
    if m:
        return "set_volume", {"action": "unmute" if m.group(1) else "mute"}
    if _SHOT.match(t):
        return "take_screenshot", {}
    if _LOCK.match(t):
        return "lock_screen", {}
    return None
