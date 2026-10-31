import os, sys, tempfile, threading, time, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, pc, voice as V
from PIL import Image


class FakeVolume:
    def __init__(self, level=50, stuck=False): self.level, self.muted, self.stuck = level, False, stuck
    def get(self): return self.level
    def set(self, v):
        if not self.stuck: self.level = v
    def get_mute(self): return self.muted
    def set_mute(self, f):
        if not self.stuck: self.muted = f


class VolumeTests(unittest.TestCase):
    def test_set_get_change(self):
        v = FakeVolume(50)
        self.assertEqual(pc.set_volume(v, 30), 30)
        self.assertEqual(pc.change_volume(v, 10), 40)
        self.assertEqual(pc.change_volume(v, -100), 0)         # clamps at 0
        self.assertEqual(pc.change_volume(v, 100), 100)         # and at 100
        self.assertEqual(pc.get_volume(v), 100)

    def test_invalid_levels_are_rejected_before_touching_windows(self):
        v = FakeVolume(50)
        for bad in (-1, 101, 5.5, "40", None, True):
            with self.assertRaises(pc.PCError, msg=repr(bad)): pc.set_volume(v, bad)
        self.assertEqual(v.level, 50)

    def test_failure_is_reported_not_hidden(self):
        with self.assertRaises(pc.PCError) as cm: pc.set_volume(FakeVolume(50, stuck=True), 80)
        self.assertIn("reports 50%", str(cm.exception))
        with self.assertRaises(pc.PCError): pc.set_mute(FakeVolume(50, stuck=True), True)

    def test_mute(self):
        v = FakeVolume(); self.assertTrue(pc.set_mute(v, True)); self.assertFalse(pc.set_mute(v, False))

    def test_real_backend_refuses_off_windows(self):
        if sys.platform != "win32":
            with self.assertRaises(pc.PCError) as cm: pc.WindowsVolume()
            self.assertIn("only works on Windows", str(cm.exception))


class ScreenshotTests(unittest.TestCase):
    def test_saved_and_verified(self):
        with tempfile.TemporaryDirectory() as d:
            now = lambda: datetime(2026, 10, 24, 12, 0, 0)
            p1 = pc.screenshot(Path(d) / "shots", grab=lambda: Image.new("RGB", (64, 48), "red"), now=now)
            p2 = pc.screenshot(Path(d) / "shots", grab=lambda: Image.new("RGB", (64, 48), "blue"), now=now)   # same second
            self.assertEqual(p1.name, "screenshot-20261024-120000.png"); self.assertNotEqual(p1, p2)
            with Image.open(p1) as im: self.assertEqual(im.size, (64, 48))

    def test_failures_are_reported(self):
        class Broken:
            def save(self, *a, **k): raise OSError("disk full")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(pc.PCError) as cm: pc.screenshot(d, grab=lambda: Broken())
            self.assertIn("disk full", str(cm.exception))
            blocker = Path(d) / "file"; blocker.write_text("x")
            with self.assertRaises(pc.PCError): pc.screenshot(blocker / "sub", grab=lambda: Image.new("RGB", (4, 4)))

    def test_real_grab_refuses_off_windows(self):
        if sys.platform != "win32":
            with self.assertRaises(pc.PCError): pc.screenshot(tempfile.gettempdir())


class LockTests(unittest.TestCase):
    def test_lock(self):
        self.assertIn("accepted", pc.lock_screen(lambda: 1))
        with self.assertRaises(pc.PCError): pc.lock_screen(lambda: 0)
        if sys.platform != "win32":
            with self.assertRaises(pc.PCError): pc.lock_screen()


class ParseTests(unittest.TestCase):
    def test_phrases(self):
        cases = {"set volume to 40": ("set_volume", {"action": "set", "level": 40}), "Set the volume to 75%": ("set_volume", {"action": "set", "level": 75}),
                 "volume 20": ("set_volume", {"action": "set", "level": 20}), "turn the volume up": ("set_volume", {"action": "up"}),
                 "volume down": ("set_volume", {"action": "down"}), "please lower the volume": ("set_volume", {"action": "down"}),
                 "increase system volume": ("set_volume", {"action": "up"}), "louder": ("set_volume", {"action": "up"}),
                 "what is the volume?": ("set_volume", {"action": "get"}), "mute": ("set_volume", {"action": "mute"}),
                 "unmute the sound": ("set_volume", {"action": "unmute"}), "Take a screenshot": ("take_screenshot", {}),
                 "screenshot": ("take_screenshot", {}), "grab a screen capture": ("take_screenshot", {}),
                 "lock my screen": ("lock_screen", {}), "lock the pc": ("lock_screen", {}),
                 "set volume to 150": ("set_volume", {"action": "set", "level": 150})}      # passed on; validation refuses it loudly
        for text, want in cases.items():
            self.assertEqual(pc.parse_command(text), want, text)

    def test_ordinary_sentences_are_left_alone(self):
        for t in ["the volume of a sphere", "what is the volume of a cube", "lock", "how do I lock a door", "mute point", "tell me about screenshots",
                  "volume is too loud in this song", "open obs", "I took a screenshot yesterday"]:
            self.assertIsNone(pc.parse_command(t), t)


VCFG = config.defaults()["voice"]


class VoiceTests(unittest.TestCase):
    def mk(self, **kw):
        cfg = dict(VCFG, **kw); self.spoken = []
        class Engine:
            def __init__(s): s.q = []
            def say(s, t): s.q.append(t)
            def runAndWait(s): self_spoken.extend(s.q); s.q.clear()
        self_spoken = self.spoken
        return V.Voice(cfg, stt=lambda: "open youtube", tts_factory=lambda: Engine())

    def test_clean_for_speech(self):
        t = V.clean_for_speech("Here:\n```python\nprint(1)\n```\nSee https://x.com/a **bold** # head")
        self.assertNotIn("print", t); self.assertNotIn("http", t); self.assertNotIn("*", t); self.assertIn("code omitted", t)
        self.assertEqual(V.clean_for_speech(""), ""); self.assertLessEqual(len(V.clean_for_speech("a " * 1000)), 800)

    def test_disabled_voice_does_not_listen_or_speak(self):
        v = self.mk(enabled=False, speak_replies=True)
        with self.assertRaises(V.VoiceError) as cm: v.listen()
        self.assertIn("/voice on", str(cm.exception))
        v.speak("hello"); time.sleep(0.1); self.assertEqual(self.spoken, [])

    def test_listen_and_speak_when_enabled(self):
        v = self.mk(enabled=True, speak_replies=True)
        self.assertEqual(v.listen(), "open youtube")
        v.speak("Opening YouTube"); v.speak("```code only```")
        deadline = time.time() + 2
        while len(self.spoken) < 2 and time.time() < deadline: time.sleep(0.02)
        self.assertEqual(self.spoken[0], "Opening YouTube"); self.assertTrue(v.shutdown())

    def test_speak_replies_off_stays_silent(self):
        v = self.mk(enabled=True, speak_replies=False); v.speak("hello"); time.sleep(0.1); self.assertEqual(self.spoken, [])

    def test_errors_become_messages_not_crashes(self):
        def mic_fail(): raise V.VoiceError("I can't find a microphone.")
        v = V.Voice(dict(VCFG, enabled=True), stt=mic_fail)
        with self.assertRaises(V.VoiceError): v.listen()
        def no_engine(): raise V.VoiceError("spoken replies need pyttsx3: pip install pyttsx3")
        v2 = V.Voice(dict(VCFG, enabled=True, speak_replies=True), tts_factory=no_engine)
        v2.speak("hi"); time.sleep(0.2)                       # must not raise
        self.assertIn("pyttsx3", v2.speaker.last_error); self.assertIn("pyttsx3", v2.status())

    def test_engine_failure_mid_speech_is_recorded(self):
        class Bad:
            def say(s, t): raise RuntimeError("audio device lost")
            def runAndWait(s): pass
        v = V.Voice(dict(VCFG, enabled=True, speak_replies=True), tts_factory=lambda: Bad())
        v.speak("one"); time.sleep(0.2); self.assertIn("audio device lost", v.speaker.last_error); self.assertTrue(v.shutdown())

    def test_status_lists_whats_missing(self):
        s = V.Voice(dict(VCFG, vosk_model_path="/no/such/model")).status()
        self.assertIn("Vosk speech model not found", s)

    @unittest.skipIf(__import__("importlib").util.find_spec("vosk"), "vosk is installed here")
    def test_real_listener_reports_missing_or_broken_packages(self):
        with self.assertRaises(V.VoiceError) as cm: V.listen_vosk("/no/model", 1)
        self.assertIn("pip install vosk sounddevice", str(cm.exception))

    @unittest.skipIf(__import__("importlib").util.find_spec("pyttsx3"), "pyttsx3 is installed here")
    def test_real_tts_reports_missing_package(self):
        with self.assertRaises(V.VoiceError): V.make_pyttsx3(175)


if __name__ == "__main__":
    unittest.main()
