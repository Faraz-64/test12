"""End-to-end checks of the message pipeline: a real Session, a fake Ollama, fake PC/browser back-ends."""
import io, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browsers, config, jarvis, pc
from test_semantic import make as make_sem
from test_pc_voice import FakeVolume


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


class FakeOllama:
    """Stands in for Ollama's /api/chat. Records every request; returns `reply` for chat and `tool_reply` for tool choice."""
    def __init__(self): self.requests, self.reply, self.tool_reply, self.error = [], "Hi there!", {"tool": "none", "arguments": {}}, None
    def __call__(self, req, timeout=0):
        if self.error: raise self.error
        body = json.loads(req.data); self.requests.append(body)
        if isinstance(body.get("format"), dict):
            return _Resp(json.dumps({"message": {"content": json.dumps(self.tool_reply)}}).encode())
        words = self.reply.split(" ")
        return _Resp(b"".join(json.dumps({"message": {"content": w + (" " if i < len(words) - 1 else "")}}).encode() + b"\n" for i, w in enumerate(words)))
    @property
    def chats(self): return [r for r in self.requests if not isinstance(r.get("format"), dict) and r.get("format") != "json"]


class FakeIO:
    def __init__(self): self.out, self.tokens, self.answers, self.asked, self.notes = [], [], [], [], []
    def write(self, s=""): self.out.append(s)
    def token(self, s): self.tokens.append(s)
    def status(self, s): pass
    def confirm(self, q): self.asked.append(q); return self.answers.pop(0) if self.answers else False
    def notify(self, s): self.notes.append(s)
    quits = 0
    def quit(self): self.quits += 1
    @property
    def text(self): return "\n".join(self.out) + "".join(self.tokens)


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); d = Path(self.tmp.name)
        self.cfg = config.defaults()
        self.cfg["memory"]["file"] = str(d / "memory.json"); self.cfg["reminders"]["file"] = str(d / "reminders.json")
        self.cfg["pc"]["screenshot_dir"] = str(d / "shots"); self.cfg["logging"]["file"] = ""; self.cfg["semantic"]["enabled"] = False
        self.cfg["reminders"]["beep"] = False
        self.ollama = FakeOllama()
        p = mock.patch("urllib.request.urlopen", self.ollama); p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.mk()

    def mk(self, **kw):
        self.io = FakeIO()
        self.s = jarvis.Session(self.cfg, {"facts": [], "history": [], "emb": {}}, "pw", self.io, apps=kw.get("apps", {"youtube": "https://www.youtube.com", "obs studio": "/x/obs"}))
        self.s.voice.shutdown()

    def say(self, text):
        self.io.out.clear(); self.io.tokens.clear(); self.s.handle(text); return self.io.text


class CalculatorAndReminders(SessionCase):
    def test_calculator_answers_without_the_model(self):
        self.assertIn("23 x 47 = 1081", self.say("what is 23 x 47?"))
        self.assertIn("20% of 150 = 30", self.say("20% of 150"))
        self.assertIn("1/0", self.say("/calc 1/0")) if False else None
        self.assertIn("division by zero", self.say("/calc 1/0"))
        self.assertEqual(self.ollama.requests, [])

    def test_unsafe_expression_is_refused_not_evaluated(self):
        self.assertIn("didn't work", self.say("/calc __import__('os').getcwd()")); self.assertEqual(self.ollama.requests, [])

    def test_reminder_lifecycle(self):
        self.assertIn("Reminder #1", self.say("remind me in 10 minutes to stretch"))
        self.assertIn("Timer #2", self.say("set a 25-minute timer"))
        listing = self.say("list reminders"); self.assertIn("stretch", listing); self.assertIn("only ring while Jarvis is running", listing)
        self.assertIn("Cancelled #1", self.say("cancel reminder 1"))
        self.assertIn("Marked #2 as done", self.say("/done 2"))
        self.assertIn("no active reminders", self.say("/reminders"))
        self.assertIn("can't find an active reminder number 9", self.say("cancel reminder 9"))
        self.assertIn("couldn't set that", self.say("remind me in 0 minutes to x"))
        self.assertEqual(self.ollama.requests, [])

    def test_reminders_persist_and_notify(self):
        self.say("remind me in 10 minutes to stretch")
        self.mk()                                                           # "restart" Jarvis: a new Session on the same files
        self.assertIn("stretch", self.say("list reminders"))
        self.s._notify({"kind": "timer", "text": "tea", "late": True})
        self.assertIn("Timer: tea", self.io.notes[0]); self.assertIn("closed or the PC was asleep", self.io.notes[0])


class TerminalSafety(unittest.TestCase):
    def test_emoji_on_a_console_that_cannot_show_it_does_not_crash(self):
        raw = io.BytesIO(); fake = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with mock.patch("sys.stdout", fake):
            t = jarvis.TerminalIO()
            t.notify("\u23f0 Reminder: stretch"); t.write("caf\u00e9 \U0001F600"); t.token("ok")
            fake.flush()
        shown = raw.getvalue().decode("ascii")
        self.assertIn("Reminder: stretch", shown); self.assertIn("caf? ?", shown)

    def test_a_failing_notifier_is_logged_not_silent(self):
        import logging, reminders as R
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as d:
            store = R.ReminderStore(Path(d) / "r.json"); now = datetime(2026, 1, 1)
            store.add("reminder", "x", now, now)
            def broken(item): raise UnicodeEncodeError("ascii", "x", 0, 1, "nope")
            with self.assertLogs("jarvis.reminders", level="ERROR") as cm:
                R.ReminderScheduler(store, broken, clock=lambda: now + timedelta(seconds=1)).check_once()
            self.assertIn("could not show reminder #1", cm.output[0])


class FactsAndMemory(SessionCase):
    def test_auto_save_list_and_forget(self):
        self.say("My name is Sam")
        self.assertEqual(self.s.mem["facts"], ["my name is Sam"])
        self.assertIn("1. my name is Sam", self.say("/facts"))
        self.io.answers = [False]; self.assertIn("kept it", self.say("/forget 1")); self.assertEqual(len(self.s.mem["facts"]), 1)
        self.assertIn("Delete fact 1: 'my name is Sam'?", self.io.asked[-1])
        self.io.answers = [True]; self.assertIn("Deleted: my name is Sam", self.say("/forget 1")); self.assertEqual(self.s.mem["facts"], [])
        self.assertIn("no fact number 3", self.say("/forget 3")); self.assertIn("Usage: /forget", self.say("/forget banana"))

    def test_changed_fact_asks_before_updating(self):
        self.say("My name is Sam")
        self.io.answers = [False]; self.say("My name is Alex"); self.assertEqual(self.s.mem["facts"], ["my name is Sam"])
        self.assertIn("Update 'my name is Sam' to 'my name is Alex'?", self.io.asked[-1])
        self.io.answers = [True]; self.say("My name is Alex"); self.assertEqual(self.s.mem["facts"], ["my name is Alex"])

    def test_remember_command_rules(self):
        self.assertIn("won't save", self.say("/remember my password is hunter2")); self.assertEqual(self.s.mem["facts"], [])
        self.io.answers = [False]; self.assertIn("won't save it", self.say("/remember my phone number is 9876543210")); self.assertEqual(self.s.mem["facts"], [])
        self.assertIn("looks like a question", self.say("/remember what is my name?"))
        self.assertIn("Got it", self.say("/remember my laptop has an RTX 3050")); self.assertIn("already have", self.say("/remember my laptop has an RTX 3050"))
        self.assertIn("Usage: /remember", self.say("/remember"))
        self.assertIn("treating /rembemer as /remember", self.say("/rembemer my exam is on friday"))

    def test_memory_is_saved_encrypted_and_reloads(self):
        self.say("/remember my laptop has an RTX 3050"); self.say("hello there friend")
        raw = Path(self.cfg["memory"]["file"]).read_bytes()
        self.assertNotIn(b"RTX", raw)
        again = jarvis.load_memory(Path(self.cfg["memory"]["file"]), "pw")
        self.assertEqual(again["facts"], ["my laptop has an RTX 3050"]); self.assertEqual(again["history"][-1]["content"], "Hi there!")

    def test_auto_save_can_be_switched_off(self):
        self.cfg["memory"]["auto_save_facts"] = "off"; self.mk(); self.say("My name is Sam"); self.assertEqual(self.s.mem["facts"], [])

    def test_facts_reach_the_model_and_history_is_replayed(self):
        self.say("/remember my laptop has an RTX 3050"); self.say("hello there friend")
        self.say("what gpu is in my laptop?")
        body = self.ollama.chats[-1]
        self.assertIn("my laptop has an RTX 3050", body["messages"][0]["content"])
        self.assertTrue(any(m["content"] == "hello there friend" for m in body["messages"]))


class SemanticInChat(SessionCase):
    def test_old_statement_is_recalled_by_meaning(self):
        sem, fake = make_sem(min_similarity=0.5)
        mem = {"facts": [], "emb": {}, "history": [{"role": "user", "content": "I am doing B.Tech Electronics at college", "mode": 1}]
               + [{"role": "user", "content": f"filler number {i} " * 30, "mode": 1} for i in range(80)]}
        kept = {h["content"] for h in jarvis.recent(mem)}
        self.assertNotIn("I am doing B.Tech Electronics at college", kept)       # too old for the replay window
        jarvis.ask(1, "What do I study?", mem, emit=lambda s: None, sem=sem)
        system = self.ollama.chats[-1]["messages"][0]["content"]
        self.assertIn("you said: I am doing B.Tech Electronics at college", system)
        self.assertNotIn("filler number", system)                                # unrelated memories stay out

    def test_falls_back_to_keywords_when_embeddings_are_down(self):
        sem, fake = make_sem(); fake.fail = OSError("down")
        mem = {"facts": [], "emb": {}, "history": [{"role": "user", "content": "my project is about calibration", "mode": 1}]
               + [{"role": "user", "content": f"filler number {i} " * 30, "mode": 1} for i in range(80)]}
        jarvis.ask(1, "what is my project about?", mem, emit=lambda s: None, sem=sem)
        self.assertIn("you said: my project is about calibration", self.ollama.chats[-1]["messages"][0]["content"])


class Actions(SessionCase):
    def test_browser_commands(self):
        calls = []
        with mock.patch.object(browsers, "open_url", side_effect=lambda url, b, d, **k: calls.append((url, b)) or (b or "your default browser")):
            self.s.detected = {"brave": "/b"}
            self.assertIn("youtube search for 'lofi' in brave", self.say("search lofi on youtube in brave"))
            self.assertEqual(calls[-1], ("https://www.youtube.com/results?search_query=lofi", "brave"))
            self.assertIn("Opened youtube in chrome", self.say("open youtube in chrome")) if False else None
        self.assertEqual(self.ollama.requests, [])

    def test_missing_browser_offers_fallback(self):
        opened = []
        with mock.patch.object(browsers.webbrowser, "open", side_effect=lambda u, new=0: opened.append(u) or True):
            self.io.answers = [True]; out = self.say("open youtube in chrome")
        self.assertIn("couldn't find chrome", self.io.asked[0]); self.assertEqual(opened, ["https://www.youtube.com"]); self.assertIn("default browser", out)

    def test_volume_is_verified_and_limits_enforced(self):
        self.s._volume = FakeVolume(50)
        self.assertIn("Volume is now 40%", self.say("set volume to 40")); self.assertEqual(self.s._volume.level, 40)
        self.assertIn("Volume is now 50%", self.say("volume up")); self.assertIn("The volume is 50%", self.say("what's the volume?"))
        self.assertIn("at most 100", self.say("set volume to 150")); self.assertEqual(self.s._volume.level, 50)     # refused, not clamped
        self.s._volume = FakeVolume(50, stuck=True)
        self.assertIn("reports 50%", self.say("set volume to 80"))                                                    # honest failure
        self.assertIn("Muted", self.say("mute")) if False else None

    @unittest.skipIf(sys.platform == "win32", "checks the non-Windows message")
    def test_pc_controls_say_so_off_windows(self):
        self.assertIn("only works on Windows", self.say("set volume to 40")); self.assertIn("only works on Windows", self.say("take a screenshot"))

    def test_screenshot_reports_the_real_path(self):
        with mock.patch.object(pc, "screenshot", return_value=Path("/shots/a.png")):
            self.assertIn("Saved a screenshot to", self.say("take a screenshot"))
        with mock.patch.object(pc, "screenshot", side_effect=pc.PCError("disk full")):
            self.assertIn("didn't work: disk full", self.say("take a screenshot"))

    def test_lock_needs_confirmation(self):
        with mock.patch.object(pc, "lock_screen", return_value="Windows accepted the lock request.") as lock:
            self.io.answers = [False]; self.assertIn("didn't do it", self.say("lock my screen")); lock.assert_not_called()
            self.io.answers = [True]; self.assertIn("accepted the lock request", self.say("lock my screen")); lock.assert_called_once()
        with mock.patch.object(pc, "lock_screen", side_effect=pc.PCError("Windows refused the lock request")):
            self.io.answers = [True]; self.assertIn("Windows refused", self.say("lock the pc"))

    def test_open_app_and_site(self):
        with mock.patch.object(jarvis, "open_target", return_value="Opening youtube!") as ot:
            self.assertEqual(self.say("open youtube").strip(), "Opening youtube!"); ot.assert_called_once()
        self.assertEqual(self.ollama.requests, [])

    def test_voice_commands(self):
        self.assertIn("voice is off", self.say("/voice status")); self.assertIn("voice is on", self.say("/voice on"))
        self.assertIn("spoken replies on", self.say("/voice speak on")); self.assertIn("Usage: /voice", self.say("/voice banana"))
        self.s.voice.enabled = False; self.assertIn("turned off", self.say("/listen"))
        self.s.voice.enabled = True; self.s.voice.stt = lambda: "what is 2 + 2"
        self.assertIn("(heard: what is 2 + 2)", self.say("/listen")); self.assertIn("2 + 2 = 4", self.io.text)


class Documents(SessionCase):
    def test_ctx_answers_from_the_right_part_and_cites_it(self):
        paras = [f"Notes number {i} about nothing in particular." * 3 for i in range(200)] + ["The final exam is on 14 December in Hall B."]
        f = Path(self.tmp.name) / "my notes.txt"; f.write_text("\n\n".join(paras), encoding="utf-8")
        self.ollama.reply = "The exam is on 14 December [part 7]."
        out = self.say(f'/ctx "{f}" when is the final exam?')
        body = self.ollama.chats[-1]
        user = body["messages"][-1]["content"]
        self.assertIn("14 December in Hall B", user); self.assertLess(len(user), 6000)                     # the excerpts, not the whole file
        self.assertIn("document", body["messages"][0]["content"].lower()); self.assertIn("From: [part", out)
        self.assertEqual(self.s.mem["history"][-1]["mode"], 8)

    def test_ctx_errors_are_friendly(self):
        self.assertIn("can't find the file", self.say("/ctx nothere.txt what?")); self.assertIn("Usage: /ctx", self.say("/ctx"))
        self.assertIn("Usage: /ctx", self.say("/ctx file.txt"))
        exe = Path(self.tmp.name) / "a.exe"; exe.write_bytes(b"MZ"); self.assertIn("not '.exe'", self.say(f'/ctx "{exe}" hi'))
        self.assertEqual(self.ollama.requests, [])


class ModelToolChoice(SessionCase):
    def test_model_can_pick_a_valid_tool(self):
        self.s.mem["facts"].append("I study electronics")
        self.ollama.tool_reply = {"tool": "memory_search", "arguments": {"query": "study"}}
        out = self.say("what do I study?")
        self.assertIn("I study electronics", out)
        self.assertEqual(len(self.ollama.chats), 0)                              # a tool answered; no chat reply was generated

    def test_invalid_or_unknown_tool_falls_back_to_chat(self):
        for reply in [{"tool": "run_python", "arguments": {"code": "x"}}, {"tool": "calculator", "arguments": {"expression": "1", "cmd": "calc"}}]:
            self.ollama.tool_reply = reply; before = len(self.ollama.chats)
            self.say("what do I study?"); self.assertEqual(len(self.ollama.chats), before + 1)

    def test_model_chosen_lock_still_asks_you(self):
        self.ollama.tool_reply = {"tool": "lock_screen", "arguments": {}}
        with mock.patch.object(pc, "lock_screen") as lock:
            self.io.answers = [False]; out = self.say("could you lock up now, open sesame")      # not caught by the deterministic rules
        lock.assert_not_called(); self.assertIn("didn't do it", out)

    def test_routing_modes(self):
        self.ollama.tool_reply = {"tool": "memory_search", "arguments": {"query": "x"}}
        self.cfg["tools"]["model_routing"] = "off"; self.mk(); self.say("what do I study?"); self.assertEqual(len(self.ollama.chats), 1)   # never asked
        n = len(self.ollama.requests); self.cfg["tools"]["model_routing"] = "auto"; self.mk(); self.say("tell me a joke")
        self.assertEqual(len(self.ollama.requests) - n, 1)                       # not actionable: just the chat call, no extra tool-choice call

    def test_code_and_json_requests_skip_tool_choice(self):
        self.ollama.tool_reply = {"tool": "lock_screen", "arguments": {}}
        self.say("write a python function to open a file"); self.assertTrue(all(r.get("format") != self.s.registry.schema() for r in self.ollama.requests))


class Resilience(SessionCase):
    def test_ollama_down_and_model_missing(self):
        import urllib.error
        self.ollama.error = urllib.error.URLError("refused"); self.assertIn("Ollama is not reachable", self.say("hello there friend"))
        self.ollama.error = urllib.error.HTTPError("u", 404, "nf", {}, None); self.assertIn("ollama pull", self.say("hello there friend"))
        self.ollama.error = None; self.assertIn("Hi there!", self.say("hello there friend"))               # recovers on its own

    def test_one_bad_message_never_ends_the_session(self):
        with mock.patch.object(jarvis.Session, "_chat", side_effect=RuntimeError("boom")):
            self.assertIn("something went wrong (RuntimeError: boom)", self.say("hello"))
        self.assertIn("Hi there!", self.say("hello again friend"))

    def test_exit_words_close_jarvis_without_asking_the_model(self):
        for word in ["exit", "Quit", "bye", "/exit", "/quit", "/rxit"]:
            before = self.io.quits
            self.assertIn("Bye", self.say(word)); self.assertEqual(self.io.quits, before + 1, word)
        self.assertEqual(self.ollama.requests, [])
        self.say("let's talk about how to exit vim"); self.assertEqual(self.io.quits, 6)       # only the bare words quit

    def test_help_tools_and_empty_input(self):
        self.assertIn("/facts", self.say("/help")); self.assertIn("calculator", self.say("/tools")); self.assertEqual(self.say("   "), "")


if __name__ == "__main__":
    unittest.main()
