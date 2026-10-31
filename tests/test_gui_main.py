import builtins, getpass, importlib.util, io, json, os, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, gui, jarvis
from test_session import FakeOllama


class GuiBridgeTests(unittest.TestCase):
    def test_messages_arrive_in_order_through_the_queue(self):
        import queue
        q = queue.Queue(); g = gui.GuiIO(q)
        g.write("hello"); g.token("a"); g.token("b"); g.status("busy"); g.notify("ring")
        got = []
        while not q.empty(): got.append(q.get())
        self.assertEqual(got, [("text", "hello\n", "bot"), ("text", "a", "bot"), ("text", "b", "bot"), ("status", "busy"), ("text", "\nring\n", "notice")])

    def test_confirm_blocks_the_worker_until_the_window_answers(self):
        import queue
        q = queue.Queue(); g = gui.GuiIO(q); result = []
        worker = threading.Thread(target=lambda: result.append(g.confirm("Lock now?")), daemon=True); worker.start()
        kind, question, box = q.get(timeout=2)
        self.assertEqual((kind, question), ("confirm", "Lock now?")); time.sleep(0.1); self.assertEqual(result, [])      # still waiting
        box["answer"] = True; box["event"].set(); worker.join(2)
        self.assertEqual(result, [True]); self.assertEqual(g.pending, [])

    def test_closing_the_window_releases_waiting_threads_with_no(self):
        import queue
        q = queue.Queue(); g = gui.GuiIO(q); result = []
        workers = [threading.Thread(target=lambda: result.append(g.confirm("x")), daemon=True) for _ in range(3)]
        for w in workers: w.start()
        for _ in range(3): q.get(timeout=2)
        g.close()
        for w in workers: w.join(2)
        self.assertEqual(result, [False, False, False]); self.assertFalse(g.confirm("after close"))

    def test_quit_event_reaches_the_window(self):
        import queue
        q = queue.Queue(); gui.GuiIO(q).quit(); self.assertEqual(q.get_nowait(), ("quit",))

    def test_worker_runs_jobs_one_at_a_time_and_survives_errors(self):
        import queue
        q, jobs = queue.Queue(), queue.Queue(); g = gui.GuiIO(q); seen = []
        class S:
            def handle(self, text):
                if text == "boom": raise RuntimeError("bad")
                seen.append(text)
        t = threading.Thread(target=gui.run_worker, args=(S(), jobs, g), daemon=True); t.start()
        for item in ["one", "boom", "two", None]: jobs.put(item)
        t.join(3)
        self.assertEqual(seen, ["one", "two"]); self.assertFalse(t.is_alive())
        texts = []
        while not q.empty(): texts.append(q.get())
        self.assertTrue(any(k == "text" and "something went wrong" in v[0] for k, *v in texts))

    @unittest.skipIf(importlib.util.find_spec("tkinter"), "tkinter is installed here")
    def test_without_tkinter_the_message_is_friendly(self):
        with self.assertRaises(SystemExit) as cm: gui.run(config.defaults(), lambda a, i: None)
        self.assertIn("python jarvis.py", str(cm.exception))


class MainLoopTests(unittest.TestCase):
    """Runs jarvis.main() the way a person would: passphrase prompts, typed lines, Ctrl-D."""
    GLOBALS = ["MODEL", "BASE_URL", "URL", "NUM_CTX", "KEEP_ALIVE", "THINK", "TIMEOUT", "MAX_OUT", "MEMORY_FILE", "FACT_BUDGET", "HISTORY_BUDGET",
               "RECALL_BUDGET", "MAX_HISTORY"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
        self.saved = {k: getattr(jarvis, k) for k in self.GLOBALS}; self.temps = {m: jarvis.MODES[m]["temp"] for m in jarvis.MODES}
        self.cfg_path = self.d / "config.json"
        cfg = {"memory": {"file": str(self.d / "memory.json")}, "reminders": {"file": str(self.d / "r.json")}, "logging": {"file": str(self.d / "j.log")},
               "semantic": {"enabled": False}, "pc": {"screenshot_dir": str(self.d / "s")}}
        self.cfg_path.write_text(json.dumps(cfg))
        self.ollama = FakeOllama(); p = mock.patch("urllib.request.urlopen", self.ollama); p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup); self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.saved.items(): setattr(jarvis, k, v)
        for m, t in self.temps.items(): jarvis.MODES[m]["temp"] = t
        import logging
        for h in list(logging.getLogger("jarvis").handlers):
            if not isinstance(h, logging.NullHandler): h.close(); logging.getLogger("jarvis").removeHandler(h)

    def drive(self, lines, passwords, extra=()):
        lines, passwords, prompts = iter(lines), iter(passwords), []
        def fake_input(prompt=""):
            try: return next(lines)
            except StopIteration: raise EOFError
        def fake_pw(prompt=""): prompts.append(prompt); return next(passwords)
        out = io.StringIO()
        with mock.patch.object(builtins, "input", fake_input), mock.patch.object(getpass, "getpass", fake_pw), mock.patch("sys.stdout", out), \
             mock.patch.object(jarvis, "find_apps", return_value={"youtube": "https://www.youtube.com"}):
            jarvis.main(["--config", str(self.cfg_path), *extra])
        return out.getvalue(), prompts

    def test_first_run_then_second_run_keeps_memory(self):
        out, prompts = self.drive(["/remember my name is Sam", "what is 6 x 7?", "remind me in 5 minutes to stretch", "/facts"], ["pw1", "pw1"])
        self.assertEqual(prompts, ["Memory passphrase: ", "Confirm passphrase: "])
        for needle in ["Jarvis is ready", "Got it", "6 x 7 = 42", "Reminder #1", "1. my name is Sam"]: self.assertIn(needle, out)
        self.assertNotIn(b"Sam", (self.d / "memory.json").read_bytes())                  # encrypted on disk
        out, prompts = self.drive(["/facts", "list reminders"], ["pw1"])                 # second run: one prompt, everything still there
        self.assertEqual(prompts, ["Memory passphrase: "]); self.assertIn("1. my name is Sam", out); self.assertIn("stretch", out)

    def test_exit_ends_the_loop_and_ignores_later_lines(self):
        out, _ = self.drive(["/remember my name is Sam", "exit", "/facts"], ["pw1", "pw1"])
        self.assertIn("Bye for now!", out); self.assertNotIn("1. my name is Sam", out)         # /facts after exit never ran
        self.assertIn("my name is Sam", json.dumps(jarvis.load_memory(self.d / "memory.json", "pw1")))   # and the memory was saved

    def test_wrong_passphrase_gets_a_retry(self):
        self.drive(["/remember my name is Sam"], ["pw1", "pw1"])
        out, prompts = self.drive(["/facts"], ["wrong", "pw1"])
        self.assertEqual(prompts, ["Memory passphrase: ", "Try again: "]); self.assertIn("my name is Sam", out)
        with self.assertRaises(SystemExit) as cm: self.drive([], ["a", "b", "c", "d"])
        self.assertIn("Too many failed attempts", str(cm.exception))
        self.assertTrue((self.d / "memory.json").exists())                                  # the memory file was left alone

    def test_confirm_mismatch_and_empty_passphrase(self):
        with self.assertRaises(SystemExit) as cm: self.drive([], ["pw1", "pw2"])
        self.assertIn("don't match", str(cm.exception)); self.assertFalse((self.d / "memory.json").exists())
        with self.assertRaises(SystemExit): self.drive([], [""])

    def test_bad_config_stops_with_a_clear_message(self):
        self.cfg_path.write_text(json.dumps({"ollama": {"timeout_seconds": "slow"}, "voice": {"tts_rate": 5}}))
        with self.assertRaises(SystemExit) as cm: self.drive([], ["pw"])
        self.assertIn("ollama.timeout_seconds", str(cm.exception)); self.assertIn("voice.tts_rate", str(cm.exception))

    def test_config_values_are_used_and_write_config(self):
        cfg = json.loads(self.cfg_path.read_text()); cfg["ollama"] = {"model": "tiny:1b"}; self.cfg_path.write_text(json.dumps(cfg))
        out, _ = self.drive(["hello there friend"], ["pw", "pw"])
        self.assertIn("tiny:1b", out); self.assertEqual(self.ollama.chats[-1]["model"], "tiny:1b")
        target = self.d / "template.json"
        with mock.patch("sys.stdout", io.StringIO()): jarvis.main(["--write-config", "--config", str(target)])
        self.assertEqual(json.loads(target.read_text()), config.defaults())


if __name__ == "__main__":
    unittest.main()
