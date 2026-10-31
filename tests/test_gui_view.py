"""Tests the window's LOGIC (styling choices, sending, themes, dialogs) using stand-in widgets, because Tkinter isn't available
in the environment these were written in. How it LOOKS on a real screen can only be judged by running: python jarvis.py --gui"""
import os, queue, sys, unittest
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, gui


class FakeVar:
    def __init__(self, value=""): self.v = value
    def set(self, v): self.v = v
    def get(self): return self.v


def make_window(**cfg_over):
    cfg = config.defaults()
    for sec, vals in cfg_over.items(): cfg[sec].update(vals)
    made = []
    def widget(*a, **k):
        w = mock.MagicMock(); w.kw = k; made.append(w); return w
    tk = SimpleNamespace(Frame=widget, Label=widget, Button=widget, Text=widget, StringVar=FakeVar, TclError=type("TclError", (Exception,), {}))
    ttk = SimpleNamespace(Style=widget, Scrollbar=widget)
    jobs, asked, closed = queue.Queue(), [], []
    win = gui.ChatWindow(tk, ttk, mock.MagicMock(), cfg, jobs, lambda q: asked.append(q) or True, lambda: closed.append(1))
    win.jobs_q, win.asked, win.closed, win.made = jobs, asked, closed, made
    return win


def inserts(win):
    return [(c.args[1], c.args[2]) for c in win.chat.insert.call_args_list]


class PureHelperTests(unittest.TestCase):
    def test_split_code(self):
        self.assertEqual(gui.split_code("plain"), [(False, "plain")])
        self.assertEqual(gui.split_code("Here:\n```python\nprint(1)\n```\ndone"), [(False, "Here:\n"), (True, "print(1)"), (False, "\ndone")])
        self.assertEqual(gui.split_code("```\nx = 1\n```"), [(True, "x = 1")])
        self.assertEqual(gui.split_code("```js\nlet a\n```and```py\nb\n```"), [(True, "let a"), (False, "and"), (True, "b")])
        self.assertEqual(gui.split_code("a ```print(1)``` b"), [(False, "a "), (True, "print(1)"), (False, " b")])      # same-line fence: not mistaken for a language
        self.assertEqual(gui.split_code("```python\nunclosed"), [(True, "unclosed")])                                     # still streaming: show it as code

    def test_tags(self):
        for line in ["[mode 1]", "Results:\n1. x", "From: [page 2]", "(heard: hi)", "(treating /rembemer as /remember)"]: self.assertEqual(gui.tag_for(line), "dim", line)
        for line in ["Hello!", "", "I couldn't find that", "Got it, I'll remember that!"]: self.assertEqual(gui.tag_for(line), "bot", line)

    def test_themes_are_complete_and_fonts_sane(self):
        self.assertEqual(set(gui.THEMES["dark"]), set(gui.THEMES["light"]))
        for theme in gui.THEMES.values():
            for key, value in theme.items(): self.assertRegex(value, r"^#[0-9a-f]{6}$", key)
        self.assertTrue(gui.font_family() and gui.mono_family())


class WindowTests(unittest.TestCase):
    def test_bot_text_gets_one_label_per_reply_and_tokens_stream_into_it(self):
        w = make_window()
        w.append("Hey", "bot"); w.append(" there", "bot"); w.append("!\n", "bot")
        self.assertEqual(inserts(w), [("Jarvis\n", "who_bot"), ("Hey", "bot"), (" there", "bot"), ("!\n", "bot")])
        w.append("[mode 1]\n", "dim"); w.append("next", "bot")
        self.assertEqual(inserts(w)[-2:], [("Jarvis\n", "who_bot"), ("next", "bot")])
        states = [c.kwargs.get("state") for c in w.chat.configure.call_args_list if "state" in c.kwargs]
        self.assertEqual(states[-2:], ["normal", "disabled"]); w.chat.see.assert_called_with("end")        # read-only again, scrolled down

    def test_user_bubble(self):
        w = make_window(); w.append("hello\nworld", "user")
        self.assertEqual(inserts(w), [("You\n", "who_user"), (" hello \n world ", "user"), ("\n", "user_end")])

    def test_code_blocks_get_their_own_style(self):
        w = make_window(); w.append("Here you go:\n```python\nprint(1)\n```\nDone.", "bot")
        self.assertEqual(inserts(w), [("Jarvis\n", "who_bot"), ("Here you go:\n", "bot"), ("\nprint(1)\n", "code"), ("\nDone.", "bot")])

    def test_notices_and_empty_text(self):
        w = make_window(); w.append("", "bot"); w.append("\n\u23f0 Reminder: stretch\n", "notice")
        self.assertEqual(inserts(w), [("\n\u23f0 Reminder: stretch\n", "notice")])

    def test_send_queues_the_message_and_shows_it(self):
        w = make_window(); w.entry.get.return_value = "  hi there \n"
        w.send()
        self.assertEqual(w.jobs_q.get_nowait(), "hi there"); w.entry.delete.assert_called_with("1.0", "end")
        self.assertIn((" hi there ", "user"), inserts(w))
        w.entry.get.return_value = "   \n"; w.send()
        self.assertTrue(w.jobs_q.empty())                                                                  # blank input is ignored

    def test_enter_sends_without_a_newline_and_shift_enter_is_left_to_tk(self):
        w = make_window(); w.entry.get.return_value = "go"
        self.assertEqual(w._on_return(None), "break"); self.assertEqual(w.jobs_q.get_nowait(), "go")
        bound = {c.args[0]: c.args[1] for c in w.entry.bind.call_args_list}
        self.assertIn("<Shift-Return>", bound); self.assertIsNone(bound["<Shift-Return>"](None))          # returns nothing -> Tk adds the newline

    def test_quick_buttons(self):
        w = make_window()
        buttons = {b.kw.get("text"): b for b in w.made if "text" in b.kw}
        self.assertTrue({"Help", "My facts", "Reminders", "Screenshot", "Tools", "Send"} <= set(buttons))
        self.assertNotIn("Speak", buttons)                                                                 # voice is off by default
        buttons["My facts"].kw["command"](); self.assertEqual(w.jobs_q.get_nowait(), "/facts")
        self.assertIn(("/facts", "user"), [(t.strip(), tag) for t, tag in inserts(w)])
        self.assertIn("Speak", {b.kw.get("text") for b in make_window(voice={"enabled": True}).made})

    def test_up_and_down_recall_earlier_messages(self):
        w = make_window()
        for text in ("one", "two"):
            w.entry.get.return_value = text; w.send()
        w.entry.get.return_value = ""
        self.assertEqual(w._on_up(None), "break"); w.entry.insert.assert_called_with("1.0", "two")
        w._on_up(None); w.entry.insert.assert_called_with("1.0", "one")
        w._on_down(None); w.entry.insert.assert_called_with("1.0", "two")
        n = w.entry.insert.call_count; self.assertEqual(w._on_down(None), "break"); self.assertEqual(w.entry.insert.call_count, n)   # back to empty
        w.entry.get.return_value = "half-typed"; self.assertIsNone(w._on_up(None))                         # Up still moves the cursor while typing
        self.assertIsNone(make_window()._on_up(None))                                                      # nothing sent yet

    def test_theme_toggle_recolours_everything(self):
        w = make_window()
        self.assertEqual((w.theme_name, w.theme_btn.configure.call_args.kwargs["text"]), ("dark", "Light"))
        w.toggle_theme()
        self.assertEqual((w.theme_name, w.theme_btn.configure.call_args.kwargs["text"]), ("light", "Dark"))
        bot = [c for c in w.chat.tag_configure.call_args_list if c.args[0] == "bot"]
        self.assertEqual([c.kwargs["foreground"] for c in bot], [gui.THEMES["dark"]["text"], gui.THEMES["light"]["text"]])
        w.root.configure.assert_called_with(bg=gui.THEMES["light"]["bg"])
        self.assertEqual(make_window(gui={"theme": "light"}).theme_name, "light")                          # config.json picks the starting theme

    def test_font_size_comes_from_config_and_never_gets_unreadable(self):
        self.assertEqual(make_window(gui={"font_size": 14}).fonts()["body"][1], 14)
        f = make_window(gui={"font_size": 8}).fonts(); self.assertTrue(all(v[1] >= 8 for v in f.values()))

    def test_status_light(self):
        w = make_window(); w.set_status("Working..."); self.assertEqual(w.status_var.get(), "Working...")
        self.assertEqual(w.dot.configure.call_args.kwargs["fg"], gui.THEMES["dark"]["busy"])
        w.set_status("Ready"); self.assertEqual(w.dot.configure.call_args.kwargs["fg"], gui.THEMES["dark"]["ok"])

    def test_events_from_the_worker(self):
        w = make_window()
        self.assertTrue(w.handle_event(("text", "hi", "bot"))); self.assertTrue(w.handle_event(("text", "old style")))
        self.assertTrue(w.handle_event(("status", "Thinking...")))
        import threading
        box = {"event": threading.Event(), "answer": False}
        self.assertTrue(w.handle_event(("confirm", "Lock now?", box)))
        self.assertEqual((w.asked, box["answer"], box["event"].is_set()), (["Lock now?"], True, True))     # the worker is released with the answer
        self.assertFalse(w.handle_event(("quit",))); self.assertEqual(w.closed, [1])

    def test_clear_and_welcome(self):
        w = make_window(); w.welcome(); self.assertIn("Jarvis is ready", inserts(w)[1][0])
        w.clear(); w.chat.delete.assert_called_with("1.0", "end"); self.assertIsNone(w.last_kind)
        w.append("again", "bot"); self.assertEqual(inserts(w)[-2], ("Jarvis\n", "who_bot"))                # a fresh label after clearing


class RunWiringTests(unittest.TestCase):
    """gui.run() against a fake tkinter module: start-up, the passphrase dialog, the close button, and a failed start."""

    def fake_tkinter(self):
        root = mock.MagicMock()
        mk = lambda *a, **k: mock.MagicMock()
        mod = SimpleNamespace(Tk=lambda: root, Frame=mk, Label=mk, Button=mk, Text=mk, StringVar=FakeVar, TclError=Exception,
                              messagebox=mock.MagicMock(), simpledialog=mock.MagicMock(), ttk=SimpleNamespace(Style=mk, Scrollbar=mk))
        mod.simpledialog.askstring.return_value = "secret"
        return mod, root

    def run_gui(self, start_session):
        mod, root = self.fake_tkinter()
        with mock.patch.dict(sys.modules, {"tkinter": mod, "tkinter.messagebox": mod.messagebox, "tkinter.simpledialog": mod.simpledialog, "tkinter.ttk": mod.ttk}):
            gui.run(config.defaults(), start_session)
        return mod, root

    def test_normal_start_and_close(self):
        got, closed = {}, []
        class S:
            def handle(self, text): pass
            def close(self): closed.append(1)
        def start(ask_pw, io):
            got["pw"] = ask_pw("Memory passphrase: "); got["io"] = io; return S()
        mod, root = self.run_gui(start)
        self.assertEqual(got["pw"], "secret"); self.assertEqual(mod.simpledialog.askstring.call_args.kwargs["show"], "*")      # typed passphrase is hidden
        root.withdraw.assert_called_once(); root.deiconify.assert_called_once(); root.mainloop.assert_called_once()
        on_close = [c.args[1] for c in root.protocol.call_args_list if c.args[0] == "WM_DELETE_WINDOW"][0]
        on_close()
        self.assertEqual(closed, [1]); root.destroy.assert_called_once(); self.assertTrue(got["io"].closed)

    def test_failed_start_shows_a_dialog_and_never_opens_the_window(self):
        def start(ask_pw, io): raise SystemExit("Wrong passphrase or damaged memory.json. File left untouched.")
        mod, root = self.run_gui(start)
        self.assertIn("Wrong passphrase", mod.messagebox.showerror.call_args.args[1]); root.destroy.assert_called_once()
        root.deiconify.assert_not_called(); root.mainloop.assert_not_called()

    def test_poll_delivers_worker_messages_to_the_window(self):
        holder = {}
        class S:
            def handle(self, text): holder["io"].write("pong"); holder["io"].status("Ready")
            def close(self): pass
        def start(ask_pw, io): holder["io"] = io; return S()
        mod, root = self.run_gui(start)
        holder["io"].write("hello from the worker")
        poll = root.after.call_args.args[1]                 # the function the window re-schedules every 50 ms
        poll()                                              # drains the queue into the (fake) chat widget; must not raise
        root.after.assert_called_with(50, poll)

    def test_without_tkinter(self):
        with mock.patch.dict(sys.modules, {"tkinter": None}):
            with self.assertRaises(SystemExit) as cm: gui.run(config.defaults(), lambda a, i: None)
        self.assertIn("python jarvis.py", str(cm.exception))


class ConfigTests(unittest.TestCase):
    def test_gui_settings_are_validated(self):
        with self.assertRaises(config.ConfigError) as cm: config.validate({"gui": {"theme": "neon", "font_size": 99}})
        self.assertIn("gui.theme", str(cm.exception)); self.assertIn("gui.font_size", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
