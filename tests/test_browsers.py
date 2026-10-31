import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import browsers as B

ENV = {"PROGRAMFILES": r"C:\Program Files", "PROGRAMFILES(X86)": r"C:\Program Files (x86)", "LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


class DetectTests(unittest.TestCase):
    def test_finds_browsers_in_standard_locations(self):
        have = {BRAVE, EDGE}
        found = B.detect_browsers(ENV, exists=lambda p: p in have, registry=lambda e: None, which=lambda e: None, system="Windows")
        self.assertEqual(found, {"brave": BRAVE, "edge": EDGE})

    def test_registry_wins_and_quotes_are_stripped(self):
        custom = r"D:\Apps\Chrome\chrome.exe"
        found = B.detect_browsers(ENV, exists=lambda p: p == custom, registry=lambda e: f'"{custom}"' if e == "chrome.exe" else None,
                                  which=lambda e: None, system="Windows")
        self.assertEqual(found, {"chrome": custom})

    def test_nothing_installed_and_missing_env(self):
        self.assertEqual(B.detect_browsers({}, exists=lambda p: False, registry=lambda e: None, which=lambda e: None, system="Windows"), {})
        # with no PROGRAMFILES set, a template must not turn into a bogus path like "\BraveSoftware\..."
        seen = []
        B.detect_browsers({}, exists=lambda p: seen.append(p) or False, registry=lambda e: None, which=lambda e: None, system="Windows")
        self.assertEqual([p for p in seen if p.startswith("\\")], [])

    def test_path_lookup_on_linux(self):
        found = B.detect_browsers({}, exists=lambda p: True, registry=lambda e: None, which=lambda e: "/usr/bin/brave-browser" if e == "brave-browser" else None, system="Linux")
        self.assertEqual(found, {"brave": "/usr/bin/brave-browser"})


class ParseTests(unittest.TestCase):
    def test_search_commands(self):
        c = B.parse_command("Search lofi on YouTube")
        self.assertEqual((c["kind"], c["site"], c["query"], c["browser"]), ("search", "youtube", "lofi", None))
        c = B.parse_command("search lofi hip hop on youtube in brave")
        self.assertEqual((c["query"], c["browser"]), ("lofi hip hop", "brave"))
        self.assertEqual(B.parse_command("play arijit singh on youtube using Chrome")["browser"], "chrome")
        self.assertEqual(B.parse_command("look up python decorators on google with edge")["site"], "google")

    def test_open_commands(self):
        c = B.parse_command("Open YouTube in Brave")
        self.assertEqual((c["kind"], c["site"], c["url"], c["browser"]), ("open", "youtube", "https://www.youtube.com", "brave"))
        c = B.parse_command("open github.com in chrome")
        self.assertEqual((c["url"], c["browser"]), ("https://github.com", "chrome"))
        self.assertEqual(B.parse_command("open youtube in the default browser")["browser"], "default")

    def test_not_a_browser_command(self):
        for t in ["open youtube", "search for pizza", "open obs in my head", "open main.py in chrome", "open notepad in brave",
                  "search the best laptop", "tell me a joke"]:
            self.assertIsNone(B.parse_command(t), t)


class UrlTests(unittest.TestCase):
    def test_valid(self):
        for u in ["https://www.youtube.com", "http://example.com/a?b=c&d=e", "https://en.wikipedia.org/w/index.php?search=a+b", "http://localhost:8080/x"]:
            self.assertEqual(B.validate_url(u), u)

    def test_rejected(self):
        for u in ["javascript:alert(1)", "file:///C:/Windows/system32", "ftp://x.com", "https://", "http://a b.com", "https://x.com/\"; calc", "https://x.com/a'b",
                  "https://x.com/<script>", "calc.exe", "", "https://" + "a" * 3000 + ".com", "https://exa mple.com", "https://x.com/`id`", "//evil.com", "https://nodot",
                  "https://x.com/a|b", "http://evil.com\\@good.com"]:
            with self.assertRaises(B.BrowserError, msg=u):
                B.validate_url(u)

    def test_search_urls_are_encoded(self):
        self.assertEqual(B.build_search_url("youtube", "lofi beats"), "https://www.youtube.com/results?search_query=lofi+beats")
        u = B.build_search_url("youtube", 'lofi & calc.exe; "x" #hash')
        self.assertNotIn(" ", u); self.assertNotIn('"', u); self.assertNotIn(";", u); self.assertNotIn("#", u)
        self.assertIn("%26+calc.exe", u)                                       # the & became data, not a new URL parameter
        self.assertIn("%E0%A4%B9", B.build_search_url("google", "हिंदी"))        # non-English text is encoded
        for bad in ["", "   ", "x" * 201]:
            with self.assertRaises(B.BrowserError):
                B.build_search_url("youtube", bad)
        with self.assertRaises(B.BrowserError):
            B.build_search_url("nosuchsite", "x")


class FakeProc:
    def __init__(self, code=None): self.code = code
    def poll(self): return self.code


class OpenTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.popen = lambda args, **kw: (self.calls.append(args), FakeProc())[1]
        self.detected = {"brave": BRAVE}

    def test_browser_started_with_an_argument_list(self):
        msg = B.open_url("https://www.youtube.com", "brave", self.detected, popen=self.popen, sleep=lambda s: None)
        self.assertEqual(msg, "brave")
        self.assertEqual(self.calls, [[BRAVE, "https://www.youtube.com"]])    # list: no shell, so no injection

    def test_default_browser(self):
        opened = []
        self.assertEqual(B.open_url("https://a.com", None, {}, default_open=lambda u, new=0: opened.append(u) or True), "your default browser")
        with self.assertRaises(B.BrowserError):
            B.open_url("https://a.com", "default", {}, default_open=lambda u, new=0: False)   # the OS said no: don't claim success

    def test_unknown_browser_and_bad_exit(self):
        with self.assertRaises(B.BrowserNotFound) as cm:
            B.open_url("https://a.com", "chrome", self.detected, popen=self.popen)
        self.assertIn("found: brave", str(cm.exception))
        with self.assertRaises(B.BrowserError) as cm:
            B.open_url("https://a.com", "brave", self.detected, popen=lambda a, **k: FakeProc(1), sleep=lambda s: None)
        self.assertIn("exit code 1", str(cm.exception))
        def boom(*a, **k): raise OSError("access denied")
        with self.assertRaises(B.BrowserError) as cm:
            B.open_url("https://a.com", "brave", self.detected, popen=boom)
        self.assertIn("access denied", str(cm.exception))

    def test_run_command_and_fallback(self):
        cmd = B.parse_command("search lofi on youtube in brave")
        msg = B.run_command(cmd, "default", self.detected, True, lambda q: True, popen=self.popen, sleep=lambda s: None)
        self.assertIn("Opened youtube search for 'lofi' in brave", msg)
        self.assertEqual(self.calls[0][1], "https://www.youtube.com/results?search_query=lofi")
        # chrome isn't installed: offer the default browser; only open it if the user agrees
        opened, asked = [], []
        chrome = B.parse_command("open youtube in chrome")
        msg = B.run_command(chrome, "default", self.detected, True, lambda q: asked.append(q) or True, default_open=lambda u, new=0: opened.append(u) or True)
        self.assertIn("couldn't find chrome", asked[0]); self.assertEqual(opened, ["https://www.youtube.com"]); self.assertIn("default browser", msg)
        opened.clear()
        msg = B.run_command(chrome, "default", self.detected, True, lambda q: False, default_open=lambda u, new=0: opened.append(u) or True)
        self.assertEqual((opened, msg), ([], "Okay, I won't open it."))
        with self.assertRaises(B.BrowserNotFound):                              # fallback switched off: a clear error instead
            B.run_command(chrome, "default", self.detected, False, lambda q: True)

    def test_preferred_browser_used_when_none_named(self):
        B.run_command(B.parse_command("search cats on youtube"), "brave", self.detected, True, lambda q: True, popen=self.popen, sleep=lambda s: None)
        self.assertEqual(self.calls[0][0], BRAVE)


if __name__ == "__main__":
    unittest.main()
