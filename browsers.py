"""Open sites and searches in a chosen browser (Brave, Chrome, Edge, Firefox, Opera) or the system default.

Safety: URLs are validated and built with URL-encoding, and browsers are started with an argument list (never a shell
string), so a message like "search lofi & calc.exe on youtube" can only ever become a harmless search query.
"""
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
import webbrowser


class BrowserError(Exception):
    pass


class BrowserNotFound(BrowserError):
    def __init__(self, name, available):
        self.name, self.available = name, sorted(available)
        have = ", ".join(self.available) or "none that I could find"
        super().__init__(f"I couldn't find {name} on this computer (found: {have})")


_PF, _PF86, _LOCAL = "%PROGRAMFILES%", "%PROGRAMFILES(X86)%", "%LOCALAPPDATA%"
BROWSERS = {
    "brave": {"exe": "brave.exe", "which": ["brave-browser", "brave"],
              "win": [_PF + r"\BraveSoftware\Brave-Browser\Application\brave.exe", _PF86 + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
                      _LOCAL + r"\BraveSoftware\Brave-Browser\Application\brave.exe"]},
    "chrome": {"exe": "chrome.exe", "which": ["google-chrome", "chromium", "chromium-browser"],
               "win": [_PF + r"\Google\Chrome\Application\chrome.exe", _PF86 + r"\Google\Chrome\Application\chrome.exe",
                       _LOCAL + r"\Google\Chrome\Application\chrome.exe"]},
    "edge": {"exe": "msedge.exe", "which": ["microsoft-edge"],
             "win": [_PF86 + r"\Microsoft\Edge\Application\msedge.exe", _PF + r"\Microsoft\Edge\Application\msedge.exe"]},
    "firefox": {"exe": "firefox.exe", "which": ["firefox"], "win": [_PF + r"\Mozilla Firefox\firefox.exe", _PF86 + r"\Mozilla Firefox\firefox.exe"]},
    "opera": {"exe": "opera.exe", "which": ["opera"], "win": [_LOCAL + r"\Programs\Opera\opera.exe", _PF + r"\Opera\opera.exe"]},
}
ALIASES = {"brave": "brave", "brave browser": "brave", "chrome": "chrome", "google chrome": "chrome", "edge": "edge",
           "microsoft edge": "edge", "ms edge": "edge", "firefox": "firefox", "opera": "opera",
           "default": "default", "default browser": "default", "my browser": "default"}

SITE_HOMES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
              "github": "https://github.com", "netflix": "https://www.netflix.com", "spotify": "https://open.spotify.com",
              "reddit": "https://www.reddit.com", "whatsapp": "https://web.whatsapp.com", "maps": "https://maps.google.com",
              "wikipedia": "https://en.wikipedia.org", "amazon": "https://www.amazon.com", "duckduckgo": "https://duckduckgo.com"}
SEARCH_URLS = {"youtube": "https://www.youtube.com/results?search_query={q}", "google": "https://www.google.com/search?q={q}",
               "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}", "github": "https://github.com/search?q={q}",
               "reddit": "https://www.reddit.com/search/?q={q}", "amazon": "https://www.amazon.com/s?k={q}",
               "duckduckgo": "https://duckduckgo.com/?q={q}", "spotify": "https://open.spotify.com/search/{q}"}


def _registry_lookup(exe):
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                return winreg.QueryValue(k, None) or None
        except OSError:
            continue
    return None


def detect_browsers(env=None, exists=os.path.isfile, registry=_registry_lookup, which=shutil.which, system=None):
    """name -> path of every supported browser found. Everything is injectable so it can be tested without Windows."""
    env = os.environ if env is None else env
    system = system or platform.system()
    found = {}
    for name, info in BROWSERS.items():
        candidates = []
        if system == "Windows":
            reg = registry(info["exe"])
            if reg:
                candidates.append(reg.strip('"'))
            for tmpl in info["win"]:
                path = re.sub(r"%([^%]+)%", lambda m: env.get(m.group(1), ""), tmpl)
                if not path.startswith("\\"):  # skip templates whose variable isn't set
                    candidates.append(path)
        for exe in [info["exe"]] + info["which"]:
            hit = which(exe)
            if hit:
                candidates.append(hit)
        for c in candidates:
            if c and exists(c):
                found[name] = c
                break
    return found


def validate_url(url):
    u = (url or "").strip()
    if not u or len(u) > 2048 or re.search(r"[\s\x00-\x1f\x7f\"'<>`\\^{}|]", u):
        raise BrowserError("that doesn't look like a safe web address")
    p = urllib.parse.urlparse(u)
    host = p.hostname or ""
    if p.scheme not in ("http", "https") or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+|localhost", host, re.I):
        raise BrowserError("I only open normal http/https web addresses")
    return u


def build_search_url(site, query):
    q = " ".join((query or "").split())
    if not q:
        raise BrowserError("what should I search for?")
    if len(q) > 200:
        raise BrowserError("that search is too long (200 characters max)")
    if site not in SEARCH_URLS:
        raise BrowserError(f"I don't know how to search {site}")
    return validate_url(SEARCH_URLS[site].format(q=urllib.parse.quote_plus(q) if site != "spotify" else urllib.parse.quote(q)))


_SITES_RX = "|".join(sorted(SEARCH_URLS, key=len, reverse=True))
_BROWSER_RX = "|".join(sorted((re.escape(a) for a in ALIASES), key=len, reverse=True))
_WITH = r"(?:\s+(?:in|using|with|on)\s+(?:the\s+)?(" + _BROWSER_RX + r")(?:\s+browser)?)?"

_SEARCH = re.compile(r"^(?:please\s+)?(?:can you\s+)?(?:search|find|look up|play|watch)\s+(?:for\s+)?(.+?)\s+on\s+(" + _SITES_RX + r")" + _WITH + r"[\s.!?]*$", re.I)
_OPEN_AND_SEARCH = re.compile(r"^(?:open|launch|go to|start)\s+(?:up\s+)?(?:(?:the|a)\s+)?(" + _SITES_RX + r")\s+(?:and|then)\s+(?:play|search(?:\s+for)?|find|look up)\s+(?:for\s+)?(.+?)" + _WITH + r"[\s.!?]*$", re.I)
_OPEN = re.compile(r"^(?:please\s+)?(?:can you\s+)?(?:open|launch|go to|visit)\s+(?:up\s+)?(.+?)\s+(?:in|using|with|on)\s+(?:the\s+)?(" + _BROWSER_RX + r")(?:\s+browser)?[\s.!?]*$", re.I)
_DOMAIN = re.compile(r"^(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?$", re.I)


def parse_command(text):
    """'search lofi on youtube in brave' / 'open youtube and play chill music' -> {'kind','site','query','url','browser'} or None."""
    t = text.strip()
    m = _SEARCH.match(t)
    if m:
        query, site, browser = m.groups()
        return {"kind": "search", "site": site.lower(), "query": query.strip(), "url": None,
                "browser": ALIASES[browser.lower()] if browser else None}
                
    m = _OPEN_AND_SEARCH.match(t)
    if m:
        site, query, browser = m.groups()
        return {"kind": "search", "site": site.lower(), "query": query.strip(), "url": None,
                "browser": ALIASES[browser.lower()] if browser else None}

    m = _OPEN.match(t)
    if m:
        target, browser = m.groups()
        target = target.strip().rstrip("/") if target.lower() in SITE_HOMES else target.strip()
        if target.lower() in SITE_HOMES:
            return {"kind": "open", "site": target.lower(), "query": None, "url": SITE_HOMES[target.lower()], "browser": ALIASES[browser.lower()]}
        if _DOMAIN.match(target) and not re.search(r"\.(py|js|ts|txt|pdf|docx?|exe|bat|md|json)$", target, re.I):
            url = target if target.lower().startswith("http") else "https://" + target
            return {"kind": "open", "site": None, "query": None, "url": url, "browser": ALIASES[browser.lower()]}
    return None


def open_url(url, browser, detected, popen=None, default_open=None, sleep=None):
    """Open `url` in `browser` ('default'/None = system default). Returns a message ONLY after it really started;
    raises BrowserError / BrowserNotFound otherwise. The three helpers are looked up when called, so tests can replace them."""
    popen, default_open, sleep = popen or subprocess.Popen, default_open or webbrowser.open, sleep or time.sleep
    url = validate_url(url)
    if browser in (None, "default"):
        try:
            ok = default_open(url, new=2)
        except Exception as e:
            raise BrowserError(f"I couldn't open your default browser: {e}")
        if not ok:
            raise BrowserError("your system didn't open a default browser")
        return "your default browser"
    exe = detected.get(browser)
    if not exe:
        raise BrowserNotFound(browser, detected)
    try:
        proc = popen([exe, url])  # a list, never a shell string
    except OSError as e:
        raise BrowserError(f"I couldn't start {browser}: {e}")
    sleep(0.4)
    code = proc.poll()
    if code not in (None, 0):  # still running (None) or handed over to an open window (0) both count as success
        raise BrowserError(f"{browser} closed straight away (exit code {code})")
    return browser


def run_command(cmd, preferred, detected, fallback_to_default, confirm, **injected):
    """Carry out a parsed command and return a message describing what actually happened."""
    url = build_search_url(cmd["site"], cmd["query"]) if cmd["kind"] == "search" else validate_url(cmd["url"])
    browser = cmd["browser"] or preferred
    what = f"{cmd['site']} search for '{cmd['query']}'" if cmd["kind"] == "search" else (cmd["site"] or url)
    try:
        where = open_url(url, browser, detected, **injected)
    except BrowserNotFound as e:
        if not fallback_to_default:
            raise
        if not confirm(f"{e}. Open it in your default browser instead?"):
            return "Okay, I won't open it."
        where = open_url(url, "default", detected, **injected)
    return f"Opened {what} in {where}."