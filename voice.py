"""Voice input (Vosk, offline) and spoken replies (pyttsx3, offline). Kept apart from the chat logic: the chat code only
ever calls Voice.listen() to get text and Voice.speak(text) to say something, and every failure is a VoiceError message."""
import importlib.util
import json
import queue
import re
import threading
import time
from pathlib import Path


class VoiceError(Exception):
    pass


def clean_for_speech(text, limit=800):
    """Don't read code, links or markdown symbols out loud."""
    t = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    t = re.sub(r"https?://\S+", " link ", t)
    t = re.sub(r"[*_`#>|]+", " ", t)
    t = " ".join(t.split())
    return t[:limit]


# ---- spoken numbers: the recognizer writes "twenty five", not "25", so commands with numbers wouldn't match ----
_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                     "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_DIGITS = {w: i for w, i in _UNITS.items() if i < 10}
# Only rewrite numbers when the sentence looks like a command or sum, so "no one knows" stays "no one knows".
_NUMBER_CONTEXT = re.compile(r"\b(minutes?|seconds?|hours?|days?|timer|remind|reminder|volume|percent|plus|minus|times|divided|multiplied|"
                             r"calculate|compute|what is|what's|cancel|done with)\b")
_SUM = re.compile(r"^((?:what is |what's |calculate |compute )?)(\d[\d.]*)((?: (?:plus|minus|times|multiplied by|divided by|over) \d[\d.]*)+)$")
_OPS = {"plus": "+", "minus": "-", "times": "*", "multiplied by": "*", "divided by": "/", "over": "/"}


def _read_number(tokens, i):
    """Read a spoken number that starts at tokens[i]. Returns (text, next_index) or None if tokens[i] isn't a number word."""
    total = current = 0
    j, state, seen = i, "start", False   # state: what may follow: start | unit | tens | hundred | thousand
    while j < len(tokens):
        w = tokens[j]
        if w == "and" and state in ("hundred", "thousand") and j + 1 < len(tokens) and (tokens[j + 1] in _UNITS or tokens[j + 1] in _TENS):
            j += 1
            continue
        if w in _TENS and state in ("start", "hundred", "thousand"):
            current, state = current + _TENS[w], "tens"
        elif w in _UNITS and (state in ("start", "hundred", "thousand") or (state == "tens" and 0 < _UNITS[w] < 10)):
            current, state = current + _UNITS[w], "unit"
        elif w == "hundred" and state in ("unit", "tens") and current < 100 and seen:
            current, state = current * 100, "hundred"
        elif w == "thousand" and seen and state in ("unit", "tens", "hundred"):
            total, current, state = total + current * 1000, 0, "thousand"
        else:
            break
        seen = True
        j += 1
    if not seen:
        return None
    value = str(total + current)
    if j + 1 < len(tokens) and tokens[j] == "point" and tokens[j + 1] in _DIGITS:   # "three point five" -> 3.5
        k, decimals = j + 1, ""
        while k < len(tokens) and tokens[k] in _DIGITS:
            decimals += str(_DIGITS[tokens[k]])
            k += 1
        return f"{value}.{decimals}", k
    return value, j


def spoken_to_text(text):
    """'set a twenty five minute timer' -> 'set a 25 minute timer'; 'what is twenty three times forty seven' -> 'what is 23 * 47'."""
    t = " ".join(text.lower().split())
    if not _NUMBER_CONTEXT.search(t):
        return text
    tokens, out, i = t.split(), [], 0
    while i < len(tokens):
        got = _read_number(tokens, i)
        if got:
            out.append(got[0])
            i = got[1]
        else:
            out.append(tokens[i])
            i += 1
    t = " ".join(out)
    m = _SUM.match(t)
    if m:
        lead, first, rest = m.groups()
        for word, sym in sorted(_OPS.items(), key=lambda kv: -len(kv[0])):
            rest = rest.replace(f" {word} ", f" {sym} ")
        t = f"{lead}{first}{rest}"
    return t


_vosk_models = {}


def listen_vosk(model_path, seconds):
    """Record up to `seconds` from the microphone and return what was said."""
    try:
        import sounddevice as sd
        import vosk
    except (ImportError, OSError) as e:  # OSError: the package is installed but its audio library (PortAudio) won't load
        raise VoiceError(f"voice input needs two working packages: pip install vosk sounddevice ({e})")
    if not Path(model_path).is_dir():
        raise VoiceError(f"I can't find the Vosk speech model at '{model_path}'. See the README for the download link.")
    try:
        if not any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices()):
            raise VoiceError("I can't find a microphone.")
        if model_path not in _vosk_models:
            vosk.SetLogLevel(-1)
            _vosk_models[model_path] = vosk.Model(model_path)
        rec = vosk.KaldiRecognizer(_vosk_models[model_path], 16000)
        audio = queue.Queue()
        deadline = time.monotonic() + seconds
        with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16", channels=1, callback=lambda d, f, t, s: audio.put(bytes(d))):
            while time.monotonic() < deadline:
                try:
                    chunk = audio.get(timeout=0.5)
                except queue.Empty:
                    continue
                if rec.AcceptWaveform(chunk):
                    said = json.loads(rec.Result()).get("text", "").strip()
                    if said:
                        return said
        said = json.loads(rec.FinalResult()).get("text", "").strip()
    except VoiceError:
        raise
    except Exception as e:  # PortAudioError, model load failure, device unplugged...
        raise VoiceError(f"the microphone or speech engine failed ({type(e).__name__}: {e})")
    if not said:
        raise VoiceError("I didn't hear anything.")
    return said


def make_pyttsx3(rate):
    try:
        import pyttsx3
    except (ImportError, OSError) as e:
        raise VoiceError(f"spoken replies need pyttsx3: pip install pyttsx3 ({e})")
    try:
        engine = pyttsx3.init()
    except Exception as e:  # e.g. no speech voices installed
        raise VoiceError(f"I couldn't start the speech engine ({e})")
    
    # --- NEW: Speed set to 3.5x ---
    engine.setProperty("rate", int(rate * 3.5))
    
    # --- NEW: Pitch set to 1.9 (ignored by Windows SAPI, works on Mac/Linux) ---
    try:
        engine.setProperty("pitch", 1.9)
    except Exception:
        pass
    
    # --- Find and set a female voice ---
    try:
        voices = engine.getProperty('voices')
        for voice in voices:
            v_name = voice.name.lower()
            v_gender = getattr(voice, 'gender', '').lower()
            
            if ('female' in v_name or 'female' in v_gender 
                or 'zira' in v_name      # Windows default female
                or 'samantha' in v_name  # Mac default female
                or 'hazel' in v_name):   # Windows UK female
                engine.setProperty('voice', voice.id)
                break
    except Exception:
        pass  # If it fails for any reason, it will just fall back to the default voice

    return engine


class Speaker:
    """Speaks on its own thread (pyttsx3 blocks while talking, and its engine must live on the thread that uses it)."""

    def __init__(self, engine_factory):
        self.factory, self.queue, self.thread, self.last_error = engine_factory, queue.Queue(), None, None

    def say(self, text):
        text = clean_for_speech(text)
        if not text:
            return
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="speaker", daemon=True)
            self.thread.start()
        self.queue.put(text)

    def _run(self):
        try:
            try:  # Windows: speech goes through COM, and every thread that uses COM has to initialise it first
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            engine = self.factory()
        except Exception as e:
            self.last_error = str(e)
            return
        while True:
            item = self.queue.get()
            if item is None:
                return
            try:
                engine.say(item)
                engine.runAndWait()
            except Exception as e:
                self.last_error = f"speech failed ({e})"

    def stop(self, timeout=3.0):
        if self.thread and self.thread.is_alive():
            self.queue.put(None)
            self.thread.join(timeout)
        return not (self.thread and self.thread.is_alive())


class Voice:
    def __init__(self, cfg, stt=None, tts_factory=None):
        self.model_path, self.seconds, self.rate = cfg["vosk_model_path"], cfg["listen_seconds"], cfg["tts_rate"]
        self.enabled, self.speak_replies = cfg["enabled"], cfg["speak_replies"]
        self.stt = stt or (lambda: listen_vosk(self.model_path, self.seconds))
        self.speaker = Speaker(tts_factory or (lambda: make_pyttsx3(self.rate)))

    def listen(self):
        if not self.enabled:
            raise VoiceError("voice is turned off. Type /voice on (or set voice.enabled in config.json).")
        return spoken_to_text(self.stt())

    def speak(self, text):
        """Say a reply if voice and spoken replies are on. Never raises: a broken speaker must not break the chat."""
        if self.enabled and self.speak_replies:
            self.speaker.say(text)

    def problems(self):
        """Quick, non-invasive check of what's missing, for /voice status."""
        out = [f"missing package: {m}" for m in ("vosk", "sounddevice", "pyttsx3") if importlib.util.find_spec(m) is None]
        if not Path(self.model_path).is_dir():
            out.append(f"Vosk speech model not found at {self.model_path}")
        if self.speaker.last_error:
            out.append(f"speaker: {self.speaker.last_error}")
        return out

    def status(self):
        p = self.problems()
        head = f"voice is {'on' if self.enabled else 'off'}; spoken replies {'on' if self.speak_replies else 'off'}"
        return head + ("" if not p else ". Problems: " + "; ".join(p))

    def shutdown(self):
        return self.speaker.stop()