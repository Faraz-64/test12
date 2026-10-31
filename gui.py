"""Desktop chat window (Tkinter, which ships with Python on Windows).

All the thinking lives in jarvis.Session. This file only draws the window and passes messages between the window thread
and a worker thread, so long tasks (model replies, web lookups, documents) never freeze the window.

Look: dark theme (light one on the button), blue bubbles for you, clean text for Jarvis, code in a monospace box, quick buttons,
a multi-line input (Enter sends, Shift+Enter = new line, Up = previous message, Ctrl+L = clear the screen).
There is no system-tray icon: Tkinter has none, and adding one needs another package (pystray) that I haven't included.
"""
import platform
import queue
import sys
import threading
import traceback

THEMES = {
    "dark": dict(bg="#0b1220", panel="#0f172a", header="#0f172a", text="#e5e7eb", dim="#7c8aa5", user_bg="#2563eb", user_fg="#ffffff",
                 accent="#38bdf8", accent_fg="#04202e", chip="#1b2742", chip_fg="#cbd5e1", chip_hover="#26365a",
                 notice_bg="#78350f", notice_fg="#fef3c7", code_bg="#0a1020", code_fg="#e2e8f0", entry_bg="#111a2e", entry_fg="#e5e7eb",
                 border="#26324d", ok="#34d399", busy="#fbbf24"),
    "light": dict(bg="#eef2f7", panel="#ffffff", header="#ffffff", text="#111827", dim="#6b7280", user_bg="#2563eb", user_fg="#ffffff",
                  accent="#0284c7", accent_fg="#ffffff", chip="#e5eaf2", chip_fg="#334155", chip_hover="#d6dfec",
                  notice_bg="#fef3c7", notice_fg="#78350f", code_bg="#f1f5f9", code_fg="#0f172a", entry_bg="#ffffff", entry_fg="#111827",
                  border="#d3dbe7", ok="#059669", busy="#d97706"),
}
CHIPS = [("Help", "/help"), ("My facts", "/facts"), ("Reminders", "/reminders"), ("Screenshot", "take a screenshot"), ("Tools", "/tools")]


def font_family():
    return {"Windows": "Segoe UI", "Darwin": "Helvetica Neue"}.get(platform.system(), "DejaVu Sans")


def mono_family():
    return {"Windows": "Consolas", "Darwin": "Menlo"}.get(platform.system(), "DejaVu Sans Mono")


def split_code(text):
    """'Here:\\n```python\\nprint(1)\\n```\\ndone' -> [(False,'Here:\\n'), (True,'print(1)'), (False,'\\ndone')]. The language label is dropped."""
    parts, out = text.split("```"), []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            first, sep, rest = part.partition("\n")
            if sep and first.strip() and " " not in first.strip() and len(first.strip()) <= 20:
                part = rest
            out.append((True, part.strip("\n")))
        elif part:
            out.append((False, part))
    return out


def tag_for(line):
    """Which style a line written by Jarvis gets: dim small print for housekeeping lines, normal text otherwise."""
    return "dim" if line.startswith(("[mode", "Results:", "From:", "(note:", "(heard:", "(treating")) else "bot"


def bmp_only(text):
    """Some Tk builds can't take characters above U+FFFF (most emoji) and raise an error. Drop them rather than break the window."""
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


class GuiIO:
    """The Session talks to this object. It may be called from any thread, so everything goes through a queue."""

    def __init__(self, events):
        self.events, self.pending, self.closed = events, [], False
        self._lock = threading.Lock()

    def write(self, s=""):
        self.events.put(("text", s + "\n", tag_for(s)))

    def token(self, s):
        self.events.put(("text", s, "bot"))

    def status(self, s):
        self.events.put(("status", s))

    def notify(self, s):
        self.events.put(("text", "\n" + s + "\n", "notice"))

    def quit(self):
        self.events.put(("quit",))

    def confirm(self, question):
        """Blocks the calling (worker) thread until the person answers in a dialog on the window thread."""
        if self.closed:
            return False
        box = {"event": threading.Event(), "answer": False}
        with self._lock:
            self.pending.append(box)
        self.events.put(("confirm", question, box))
        box["event"].wait()
        with self._lock:
            if box in self.pending:
                self.pending.remove(box)
        return box["answer"]

    def close(self):
        """The window is closing: answer 'no' to anything still waiting so no thread is left hanging."""
        self.closed = True
        with self._lock:
            waiting = list(self.pending)
        for box in waiting:
            box["answer"] = False
            box["event"].set()


def run_worker(session, jobs, io):
    """Background thread: take one message at a time and let the Session handle it."""
    while True:
        text = jobs.get()
        if text is None:
            return
        io.status("Working...")
        try:
            session.handle(text)
        except Exception as e:  # Session.handle already guards itself; this is the last safety net
            io.write(f"Oops, something went wrong ({type(e).__name__}: {e}).")
        io.status("Ready")


class ChatWindow:
    """All the widgets. `tk` and `ttk` are passed in so the logic can be tested with stand-in widgets."""

    def __init__(self, tk, ttk, root, cfg, jobs, ask_yes_no, on_close):
        self.tk, self.ttk, self.root, self.jobs, self.ask_yes_no, self.on_close = tk, ttk, root, jobs, ask_yes_no, on_close
        self.theme_name, self.size = cfg["gui"]["theme"], cfg["gui"]["font_size"]
        self.voice = cfg["voice"]["enabled"]
        self.last_kind, self.sent, self.sent_pos = None, [], None
        self._build()
        self.apply_theme(self.theme_name)

    # ---- layout ----
    def _build(self):
        tk, ttk, root = self.tk, self.ttk, self.root
        root.title("Jarvis")
        root.geometry("860x660")
        root.minsize(540, 500)
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.header = tk.Frame(root)
        self.header.pack(fill="x")
        self.title = tk.Label(self.header, text="Jarvis", anchor="w")
        self.title.pack(side="left", padx=(18, 8), pady=12)
        self.theme_btn = tk.Button(self.header, command=self.toggle_theme, relief="flat", bd=0, padx=12, pady=3, cursor="hand2")
        self.theme_btn.pack(side="right", padx=14)
        self.status_var = tk.StringVar(value="Ready")
        self.status = tk.Label(self.header, textvariable=self.status_var, anchor="e")
        self.status.pack(side="right", padx=(0, 6))
        self.dot = tk.Label(self.header, text="\u25cf")
        self.dot.pack(side="right")
        self.rule = tk.Frame(root, height=1)
        self.rule.pack(fill="x")

        # Bottom widgets packed first so they don't get pushed out by the expanding chat area
        self.inputbar = tk.Frame(root)
        self.inputbar.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        self.entry = tk.Text(self.inputbar, height=3, wrap="word", relief="solid", bd=1, padx=12, pady=10)
        self.send_btn = tk.Button(self.inputbar, text="Send", command=self.send, relief="flat", bd=0, padx=22, pady=8, cursor="hand2")
        self.entry.pack(side="left", fill="x", expand=True)
        self.send_btn.pack(side="left", padx=(10, 0), fill="y")
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Shift-Return>", lambda e: None)  # no handler result: Tk inserts the newline as usual
        self.entry.bind("<Up>", self._on_up)
        self.entry.bind("<Down>", self._on_down)

        self.chips = tk.Frame(root)
        self.chips.pack(side="bottom", fill="x", padx=16, pady=(8, 0))
        self.chip_buttons = []
        items = list(CHIPS) + ([("Speak", "/listen")] if self.voice else [])
        for label, command in items:
            b = tk.Button(self.chips, text=label, command=lambda c=command: self.quick(c), relief="flat", bd=0, padx=12, pady=4, cursor="hand2")
            b.pack(side="left", padx=(0, 6))
            self.chip_buttons.append(b)

        # Body takes the remaining space
        self.body = tk.Frame(root)
        self.body.pack(fill="both", expand=True)
        self.chat = tk.Text(self.body, wrap="word", state="disabled", relief="flat", bd=0, padx=20, pady=14, cursor="arrow", highlightthickness=0)
        self.scroll = ttk.Scrollbar(self.body, command=self.chat.yview)
        self.chat.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        
        root.bind("<Control-l>", self.clear)

    # ---- look ----
    def fonts(self):
        f, m, s = font_family(), mono_family(), self.size
        return dict(body=(f, s), small=(f, max(8, s - 2)), bold=(f, s, "bold"), tiny_bold=(f, max(8, s - 3), "bold"), mono=(m, max(8, s - 1)), title=(f, s + 5, "bold"))

    def apply_theme(self, name):
        c, fo = THEMES[name], self.fonts()
        self.theme_name = name
        self.root.configure(bg=c["bg"])
        for w in (self.header, self.title, self.status, self.dot):
            w.configure(bg=c["header"])
        self.title.configure(fg=c["text"], font=fo["title"])
        self.status.configure(fg=c["dim"], font=fo["small"])
        self.rule.configure(bg=c["border"])
        self.theme_btn.configure(text="Light" if name == "dark" else "Dark", bg=c["chip"], fg=c["chip_fg"], activebackground=c["chip_hover"],
                                 activeforeground=c["chip_fg"], font=fo["small"])
        for w in (self.body, self.chips, self.inputbar):
            w.configure(bg=c["bg"] if w is not self.body else c["panel"])
        self.chat.configure(bg=c["panel"], fg=c["text"], font=fo["body"], selectbackground=c["accent"], selectforeground=c["accent_fg"])
        for b in self.chip_buttons:
            b.configure(bg=c["chip"], fg=c["chip_fg"], activebackground=c["chip_hover"], activeforeground=c["chip_fg"], font=fo["small"])
        
        # Entry is now a solid box with a visible border
        self.entry.configure(bg=c["entry_bg"], fg=c["entry_fg"], insertbackground=c["accent"], font=fo["body"], 
                             highlightbackground=c["border"], highlightcolor=c["accent"], 
                             selectbackground=c["accent"], selectforeground=c["accent_fg"],
                             relief="solid", bd=1, highlightthickness=0)
        self.send_btn.configure(bg=c["accent"], fg=c["accent_fg"], activebackground=c["accent"], activeforeground=c["accent_fg"], font=fo["bold"])
        
        t = self.chat.tag_configure
        t("bot", foreground=c["text"], font=fo["body"], lmargin1=4, lmargin2=4, rmargin=70, spacing1=2, spacing3=2)
        t("user", foreground=c["user_fg"], background=c["user_bg"], font=fo["body"], justify="right", lmargin1=150, lmargin2=150, rmargin=6, spacing1=2, spacing3=2)
        t("user_end", justify="right", lmargin1=150, lmargin2=150, rmargin=6, font=fo["small"])
        t("who_user", foreground=c["dim"], font=fo["tiny_bold"], justify="right", rmargin=6, spacing1=14)
        t("who_bot", foreground=c["accent"], font=fo["tiny_bold"], lmargin1=4, spacing1=14)
        t("dim", foreground=c["dim"], font=fo["small"], lmargin1=4, lmargin2=4, spacing1=1)
        t("notice", foreground=c["notice_fg"], background=c["notice_bg"], font=fo["bold"], lmargin1=4, lmargin2=4, spacing1=6, spacing3=6)
        t("code", foreground=c["code_fg"], background=c["code_bg"], font=fo["mono"], lmargin1=18, lmargin2=18, rmargin=50, spacing1=4, spacing3=4)
        try:
            self.style.configure("Vertical.TScrollbar", background=c["chip"], troughcolor=c["panel"], bordercolor=c["panel"],
                                 arrowcolor=c["dim"], lightcolor=c["chip"], darkcolor=c["chip"])
        except self.tk.TclError:
            pass
        self.set_status(self.status_var.get())

    def toggle_theme(self):
        self.apply_theme("light" if self.theme_name == "dark" else "dark")

    # ---- messages ----
    def _insert(self, text, tag):
        if not text:
            return
        try:
            self.chat.insert("end", text, tag)
        except self.tk.TclError:  # an emoji this Tk can't show: retry without it instead of failing
            self.chat.insert("end", bmp_only(text), tag)

    def append(self, text, kind):
        """Add text to the conversation. kind: bot | user | dim | notice."""
        if not text:
            return
        self.chat.configure(state="normal")
        try:
            if kind == "user":
                self._insert("You\n", "who_user")
                lines = text.split("\n")
                self._insert("\n".join(f" {l} " for l in lines), "user")
                self._insert("\n", "user_end")
            else:
                if kind == "bot" and self.last_kind != "bot":
                    self._insert("Jarvis\n", "who_bot")
                if kind == "bot" and "```" in text:
                    for is_code, piece in split_code(text):
                        self._insert(("\n" + piece + "\n") if is_code else piece, "code" if is_code else "bot")
                else:
                    self._insert(text, kind)
            self.last_kind = kind
        finally:
            self.chat.configure(state="disabled")  # read-only again, even if something above failed
        self.chat.see("end")

    def welcome(self):
        self.append("Hey! Jarvis is ready. Ask me anything, or tap a button below. Type /help for everything I can do.\n", "bot")

    def set_status(self, text):
        self.status_var.set(text)
        c = THEMES[self.theme_name]
        self.dot.configure(fg=c["ok"] if text == "Ready" else c["busy"])

    def clear(self, _event=None):
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.last_kind = None

    # ---- input ----
    def send(self, _event=None):
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        self.entry.delete("1.0", "end")
        self.sent.append(text)
        self.sent_pos = None
        self.append(text, "user")
        self.jobs.put(text)

    def quick(self, command):
        self.append(command, "user")
        self.jobs.put(command)

    def _on_return(self, _event):
        self.send()
        return "break"  # don't also type a newline

    def _recall(self, pos):
        self.entry.delete("1.0", "end")
        if pos is not None:
            self.entry.insert("1.0", self.sent[pos])
        self.sent_pos = pos

    def _on_up(self, _event):
        if not self.sent or (self.sent_pos is None and self.entry.get("1.0", "end").strip()):
            return None  # typing something: let Up move the cursor
        self._recall(len(self.sent) - 1 if self.sent_pos is None else max(0, self.sent_pos - 1))
        return "break"

    def _on_down(self, _event):
        if self.sent_pos is None:
            return None
        self._recall(self.sent_pos + 1 if self.sent_pos + 1 < len(self.sent) else None)
        return "break"

    # ---- events from the worker thread ----
    def handle_event(self, event):
        """Returns False when the window should stop polling (it was closed)."""
        kind, *rest = event
        if kind == "text":
            self.append(rest[0], rest[1] if len(rest) > 1 else "bot")
        elif kind == "status":
            self.set_status(rest[0])
        elif kind == "confirm":
            question, box = rest
            box["answer"] = bool(self.ask_yes_no(question))
            box["event"].set()
        elif kind == "quit":
            self.on_close()
            return False
        return True

    def report_problem(self, error):
        """Show a small grey note (and print the details to the console) instead of letting one bad message stop the window."""
        traceback.print_exc(file=sys.stderr)
        try:
            self.append(f"(display problem: {type(error).__name__}: {error})\n", "dim")
        except Exception:
            pass


def _dpi_aware():
    """Crisp text on high-resolution Windows screens."""
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def run(cfg, start_session):
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog, ttk
    except ImportError:
        raise SystemExit("The window needs Tkinter, which is included with the python.org installer for Windows. "
                         "The terminal version works without it: python jarvis.py")
    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    events, jobs = queue.Queue(), queue.Queue()
    io = GuiIO(events)
    try:
        session = start_session(lambda prompt: simpledialog.askstring("Jarvis", prompt, show="*", parent=root), io)
    except SystemExit as e:
        messagebox.showerror("Jarvis", str(e) or "Couldn't start.")
        root.destroy()
        return

    def on_close():
        io.close()
        jobs.put(None)
        session.close()
        root.destroy()

    window = ChatWindow(tk, ttk, root, cfg, jobs, lambda q: messagebox.askyesno("Jarvis", q, parent=root), on_close)

    def poll():
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            try:
                if window.handle_event(event) is False:
                    return  # the window was closed
            except Exception as e:  # one bad message must never stop the window from updating (it used to, for good)
                window.report_problem(e)
                if event[0] == "confirm":  # don't leave the worker thread waiting for an answer that will never come
                    event[2]["answer"] = False
                    event[2]["event"].set()
        root.after(50, poll)

    threading.Thread(target=run_worker, args=(session, jobs, io), daemon=True, name="jarvis-worker").start()
    root.protocol("WM_DELETE_WINDOW", on_close)
    window.welcome()
    window.entry.focus_set()
    root.deiconify()
    poll()
    root.mainloop()