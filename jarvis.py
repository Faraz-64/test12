#!/usr/bin/env python3
"""Jarvis - a friendly local helper.

Requirements: see requirements.txt and README.md.
  python jarvis.py            terminal chat
  python jarvis.py --gui      desktop window
  python jarvis.py --selftest quick checks (full tests: python -m unittest discover -s tests)
"""
import atexit
import base64, difflib, hashlib, json, logging, os, platform, re, shlex, shutil, subprocess, sys, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import browsers
import calculator
import config as config_mod
import documents
import facts
import pc
import reminders
import semantic
import toolreg
import voice as voice_mod

HERE = Path(__file__).resolve().parent
log = logging.getLogger("jarvis")
log.addHandler(logging.NullHandler())

# ---- settings (all overridden from config.json by apply_config; these are just the built-in defaults) ----
_D = config_mod.defaults()
MODEL = _D["ollama"]["model"]
BASE_URL = _D["ollama"]["base_url"]
URL = BASE_URL + "/api/chat"
NUM_CTX, KEEP_ALIVE, THINK, TIMEOUT = _D["ollama"]["num_ctx"], _D["ollama"]["keep_alive"], _D["ollama"]["think"], _D["ollama"]["timeout_seconds"]
MAX_OUT = _D["model_params"]["max_output_tokens"]
MEMORY_FILE = HERE / _D["memory"]["file"]  # sits next to this script; delete it to wipe memory
FACT_BUDGET, HISTORY_BUDGET, RECALL_BUDGET = _D["memory"]["fact_budget_tokens"], _D["memory"]["history_budget_tokens"], _D["memory"]["recall_budget_tokens"]
MAX_HISTORY = _D["memory"]["max_history_messages"]

# The friendly tone lives in modes 1, 3, 7 and 8; modes 2 and 4 must output raw code / raw JSON.
BASE = "You are Jarvis, a friendly, down-to-earth helper who talks like a smart friend."

MODES = {
    1: dict(temp=0.6, prompt="""Chat like a warm, casual friend: plain words, contractions, short replies (usually 1 to 3 sentences). A light emoji now and then is fine.
Be honest. Never guess or make things up. If you don't know, say so in a friendly way, like: Hmm, I'm not sure about that one.
If the question needs live, online or very recent information, reply exactly: That one needs a web lookup. Try: /web <your question>"""),
    2: dict(temp=0.2, prompt="""Output ONLY one Markdown code block. No text before or after it.
Default to Python 3 unless another language is requested.
Add short inline comments only for non-obvious logic."""),
    3: dict(temp=0.1, prompt="""Answer factual questions ONLY from the text inside <context>. Use no outside knowledge for facts.
You may use the 'Known about the user' facts for tone and personal references, but never as a source for factual answers.
If the answer is not in the context, reply exactly: Hmm, I couldn't find that in what I looked up.
Otherwise answer like a friendly friend would: warm, casual, plain words, 1 to 3 short sentences."""),
    4: dict(temp=0.0, prompt="""Output ONLY one raw JSON object, no commentary.
Schema: {"name": string, "action": string, "parameters": [string], "urgent": boolean}
name = target system, tool or entity. action = one verb phrase. parameters = list of strings, [] if none.
urgent = true only if the user states urgency, else false. Missing field: "" / [] / false.
Example input: urgently restart the web server on port 8080
Example output: {"name": "web server", "action": "restart", "parameters": ["port 8080"], "urgent": true}"""),
    7: dict(temp=0.1, prompt="""From the papers inside <context>, list the ones that match the request as a numbered list.
For each: title (year), first author et al., then one casual sentence on what it is about, taken from the abstract.
Start with one friendly line, like: Here's what I found!
Use only the context. Never invent papers or details. You may use the 'Known about the user' facts for tone only.
If nothing matches, reply exactly: Hmm, none of these look like a match."""),
    8: dict(temp=0.1, prompt="""You are answering questions about a document. The excerpts are inside <context>, each starting with a label like [page 3] or [section: Fees].
Answer ONLY from those excerpts, and put the label of the excerpt after each fact you use, like: The fee is 85,000 rupees [section: Fees].
If the excerpts don't contain the answer, reply exactly: Hmm, I couldn't find that in the document.
If you add anything that is NOT from the document (general knowledge or a guess), put it on a separate last line that starts with: Not from the document:
Keep it friendly, plain and short. You may use the 'Known about the user' facts for tone only."""),
}
GROUNDED = (3, 7, 8)  # modes that answer only from <context>: they get your facts for tone, never chat history or recall
CHATTY = (1, 5, 6, 7, 8)  # history entries from these modes are replayed / recalled (7 and 8 are stored but see recent())

SCHEMA = {"name": "", "action": "", "parameters": [], "urgent": False}

STOP = set("the and for are was were what whats who whos how hows when where why does did you your this that with from have has had can will about tell give know remember told said please dont".split())
REMEMBER = re.compile(r"(?i)^(?:/remember|remember that)\s+(.+)")
LINK = re.compile(r"https?://[^\s<>\")']+")  # only http(s): never file:// or other schemes
PAGE_CHARS, LINK_CHARS = 1800, 3000  # text kept per page: search-result pages / pages you link; sized to fit the 6144-token window

# Code-intent detection: verb+language, filename extension, or a strong noun. Language alone stays chat.
CODE_KW = re.compile(r"\b(code|script|function|program|snippet|algorithm|regex|refactor|debug|unit[ -]?tests?|endpoint)\b")
CODE_LANG = r"(python|javascript|typescript|rust|golang|java|c\+\+|c#|bash|html|css|sql|yaml|json)"
CODE_VERB = re.compile(
    r"\b(write|create|make|build|generate|implement|fix|refactor|debug|optimize)\b.*\b" + CODE_LANG + r"\b"
    r"|\bhow (do|can|would|should) (i|you)\b.*\b" + CODE_LANG + r"\b"
)
CODE_FILE = re.compile(r"\.(py|js|ts|tsx|jsx|mjs|rs|go|java|cpp|cc|cxx|cs|sh|bash|ps1|html?|css|sql|json|ya?ml|toml|ini)\b", re.I)

# Domains we treat as URLs without requiring https://. Local file extensions (.py, .pdf, ...) are excluded.
COMMON_TLDS = "com|org|net|edu|gov|mil|int|io|dev|app|ai|co|uk|us|in|de|fr|jp|cn|au|ca|br|ru|nl|se|es|it|ch|at|be|dk|fi|no|nz|za|mx|ar|cl|pl|cz|gr|pt|tr|ua|vn|th|ph|my|sg|id|hk|tw|kr|xyz|site|online|store|tech|space"


def apply_config(cfg):
    """Copy config.json settings into the module-level values the rest of the code reads."""
    global MODEL, BASE_URL, URL, NUM_CTX, KEEP_ALIVE, THINK, TIMEOUT, MAX_OUT, MEMORY_FILE
    global FACT_BUDGET, HISTORY_BUDGET, RECALL_BUDGET, MAX_HISTORY
    o, p, m = cfg["ollama"], cfg["model_params"], cfg["memory"]
    MODEL, BASE_URL = o["model"], o["base_url"].rstrip("/")
    URL = BASE_URL + "/api/chat"
    NUM_CTX, KEEP_ALIVE, THINK, TIMEOUT = o["num_ctx"], o["keep_alive"], o["think"], o["timeout_seconds"]
    MAX_OUT = p["max_output_tokens"]
    MEMORY_FILE = Path(m["file"])
    FACT_BUDGET, HISTORY_BUDGET, RECALL_BUDGET = m["fact_budget_tokens"], m["history_budget_tokens"], m["recall_budget_tokens"]
    MAX_HISTORY = m["max_history_messages"]
    for mode, key in ((1, "temperature_chat"), (2, "temperature_code"), (3, "temperature_grounded"), (7, "temperature_grounded"),
                      (8, "temperature_grounded"), (4, "temperature_json")):
        MODES[mode]["temp"] = p[key]


def setup_logging(cfg):
    """Log tool use and errors to jarvis.log (never passphrases; chat text is only logged at DEBUG)."""
    lg = logging.getLogger("jarvis")
    lg.setLevel(getattr(logging, cfg["logging"]["level"]))
    for h in list(lg.handlers):
        if not isinstance(h, logging.NullHandler):
            h.close()
            lg.removeHandler(h)
    path = cfg["logging"]["file"]
    if path:
        try:
            h = logging.FileHandler(path, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            lg.addHandler(h)
        except OSError as e:
            print(f"(couldn't open the log file {path}: {e})", file=sys.stderr)
    lg.propagate = False


def _is_code_request(t):
    # A bare language name ("what is python") stays chat; only verb+language, an actual filename, or a strong noun counts.
    return bool(CODE_KW.search(t) or CODE_VERB.search(t) or CODE_FILE.search(t))


def route(text):
    # First match wins: context > extraction > code > link > papers > live-info keywords > chat (the default).
    # shortcut: keyword routing misfires on e.g. "what is a function in maths"; upgrade to a classifier call if it bites.
    t = text.lower()
    if "<context>" in t:
        return 3
    if re.search(r"\b(extract|parse|as json)\b", t):
        return 4
    if _is_code_request(LINK.sub(" ", t)):  # links removed first, so "page.html" in a URL isn't read as a code file
        return 2
    if LINK.search(text):
        return 6  # you pasted a link: read that page, then answer from it
    if re.search(r"\b(papers?|arxiv|journal)\b", t):
        return 7  # research papers: arXiv search. "/papers <topic>" forces it.
    if re.search(r"\bwhat('s| is)?( the)? (date|time|day)\b|\bwhat (time|day)\b", t):
        return 1  # the laptop clock is already in the prompt, no search needed
    explicit = re.search(r"\b(search|look up|lookup|google|browse|online|internet|find me|near me|nearby|reviews?)\b", t)
    if not explicit and re.search(r"\b(my|mine|i told you|you said|earlier|remember|we talked|last time)\b", t):
        return 1  # questions about you or what you said earlier are answered from memory, not the web
    # Search only when the question needs fresh or hard-to-remember facts, or you ask for it. Everything else is chat.
    # "/web <question>" forces a search, "/chat <text>" forces chat.
    if (re.search(r"\b(search|look up|lookup|google|browse|online|internet|find me|near me|nearby|reviews?)\b", t)
            or re.search(r"\b(latest|recent|recently|news|today|tonight|yesterday|tomorrow|right now|currently|current|this (week|month|year))\b", t)
            or re.search(r"\b(weather|forecast|price|prices|stock|stocks|score|scores|who won|trending|release date)\b", t)
            or re.search(r"\b(ceo|president|prime minister|chief minister)\b", t)
            or re.search(r"\b20(2[5-9]|[3-9]\d)\b", t)):
        return 5
    return 1


CMDS = ["/remember", "/memory", "/facts", "/forget", "/update", "/apps", "/ctx", "/web", "/papers", "/chat", "/calc",
        "/reminders", "/cancel", "/done", "/voice", "/listen", "/tools", "/help", "/exit", "/quit"]


def fix_command(text, say=None):
    # Forgive small typos in slash commands, e.g. /rembemer -> /remember.
    say = say or safe_print
    first, _, rest = text.partition(" ")
    if first.startswith("/") and first.lower() not in CMDS:
        near = difflib.get_close_matches(first.lower(), CMDS, n=1, cutoff=0.75)
        if near:
            say(f"(treating {first} as {near[0]})")
            return (near[0] + " " + rest).strip()
    return text


def tidy(raw):
    # Small models drop keys; force the schema so downstream code never breaks.
    try:
        d = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(d, dict):
        return raw
    return json.dumps({k: d.get(k, v) for k, v in SCHEMA.items()})


def only_code(out):
    # Keep just the first fenced code block; drop any chatter before or after it. No fence: leave the text alone.
    m = re.search(r"```.*?```", out, re.S)
    return m.group(0) if m else out


def run(cmd, timeout=90):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None  # command missing or too slow


def check_updates(say=None):
    # Check only: nothing is installed or changed. Needs an internet connection.
    say = say or safe_print
    say("Checking for updates (this can take a minute)...")
    try:
        pkgs = json.loads(run([sys.executable, "-m", "pip", "list", "--outdated", "--format=json"]))
    except (TypeError, ValueError):
        pkgs = None
    if pkgs is None:
        say("Python packages: couldn't check (no internet?).")
    elif not pkgs:
        say("Python packages: all up to date.")
    else:
        say(f"Python packages: {len(pkgs)} can be updated:")
        for p in pkgs[:8]:
            say(f"  {p['name']} {p['version']} -> {p['latest_version']}")
        say("  Update one with: pip install -U <name>")
    system = platform.system()
    cmd = {"Windows": ["winget", "upgrade"], "Linux": ["apt", "list", "--upgradable"],
           "Darwin": ["softwareupdate", "-l"]}.get(system)
    out = run(cmd, 120) if cmd else None
    if out is None:
        say(f"{system} updates: couldn't check{'' if cmd else ' (unsupported system)'}.")
    else:
        lines = [l for l in out.splitlines() if len(l.strip()) > 2]  # drop spinner noise
        say(f"{system} updates (via {cmd[0]}):")
        say("\n".join("  " + l for l in lines[:12]) or "  nothing reported.")
        # shortcut: output is shown as the tool prints it; on Linux run 'sudo apt update' first for a fresh list.
    say(f"Model: run `ollama pull {MODEL}`. It only downloads if there's a newer version.")


APPS_FILE = HERE / "apps.json"  # optional: {"valorant": "C:\\path\\game.exe", "csgo": "steam://rungameid/730"}
SITES = {"youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
         "github": "https://github.com", "netflix": "https://www.netflix.com", "spotify": "https://open.spotify.com",
         "reddit": "https://www.reddit.com", "whatsapp": "https://web.whatsapp.com", "maps": "https://maps.google.com"}
LAUNCH = re.compile(r"^(?:please |can you |could you )?(open|launch|start|run|play)\s+(?:the |up )?(.+?)(?:\s+(?:for me|please|now))?[.!?]*$", re.I)
BROWSER = {"preferred": "default", "detected": {}}  # set by Session from config.json and browser detection


def _desktop_hidden(path):
    # Skip "NoDisplay=true" / "Hidden=true" entries so /apps doesn't list system plumbing.
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("NoDisplay", "Hidden") and v.strip().lower() == "true":
                return True
    except OSError:
        return False
    return False


def find_apps():
    # name -> thing to launch. Discovered from your Start Menu and Desktop (Windows), .desktop files (Linux) or
    # /Applications (macOS); then built-in sites; then your own apps.json, which wins on a clash.
    apps, system = {}, platform.system()
    if system == "Windows":
        roots = [os.environ.get("ProgramData"), os.environ.get("APPDATA")]
        found = [p for r in roots if r for p in (Path(r) / "Microsoft/Windows/Start Menu/Programs").rglob("*")]
        desktops = [Path.home() / "Desktop"]
        if os.environ.get("PUBLIC"):  # guard: Path("") / "Desktop" resolves to ./Desktop in the CWD
            desktops.append(Path(os.environ["PUBLIC"]) / "Desktop")
        for r in desktops:
            if r.is_dir():
                found += list(r.glob("*"))
        for p in found:
            if p.suffix.lower() in (".lnk", ".url") and not any(w in p.stem.lower() for w in ("uninstall", "readme")):
                apps.setdefault(p.stem.lower(), str(p))
    elif system == "Linux":
        for d in ("/usr/share/applications", str(Path.home() / ".local/share/applications")):
            for p in Path(d).glob("*.desktop"):
                if _desktop_hidden(p):
                    continue
                apps.setdefault(p.stem.lower(), p.stem)  # launched with gtk-launch <name>
    elif system == "Darwin":
        for p in Path("/Applications").glob("*.app"):
            apps.setdefault(p.stem.lower(), p.stem)
    apps.update(SITES)
    try:
        apps.update({str(k).lower(): str(v) for k, v in json.loads(APPS_FILE.read_text(encoding="utf-8")).items()})
    except (OSError, ValueError, AttributeError):
        pass  # no apps.json (or a broken one) is fine
    return apps


def pick(name, apps):
    # Exact name, else a whole-word or prefix match (shortest name wins), else a fuzzy guess that needs confirming.
    if name in apps:
        return name, True
    hits = [k for k in apps if name in k.split() or k.startswith(name)]
    if hits:
        return min(hits, key=len), True
    near = difflib.get_close_matches(name, list(apps), n=1, cutoff=0.7)
    return (near[0], False) if near else None


def open_target(t, label):
    try:
        system = platform.system()
        if t.startswith(("http://", "https://")):
            try:  # your preferred browser from config.json, else the system default
                browsers.open_url(t, BROWSER["preferred"], BROWSER["detected"])
            except browsers.BrowserNotFound:
                browsers.open_url(t, "default", {})
        elif system == "Windows":
            os.startfile(t)  # works for shortcuts, .exe files and steam:// links
        elif system == "Darwin":
            subprocess.Popen(["open", "-a", t])
        else:
            cmd = shlex.split(t)
            subprocess.Popen(cmd if shutil.which(cmd[0]) else ["gtk-launch", t], start_new_session=True)
    except (OSError, ValueError, browsers.BrowserError) as e:
        return f"Hmm, I couldn't open {label}: {e}"
    return f"Opening {label}!"


def launch(text, apps, confirm=None):
    # Open a site or app you asked for. Returns a message if handled, or None if this isn't a launch request
    # (then normal chat/search carries on). Only your typed words are used: never model output or web page text.
    m = LAUNCH.match(text.strip())
    if not m:
        return None
    shown = m.group(2).strip()
    target = shown.lower()
    yt = re.fullmatch(r"(?:some |the )?(.+?) on youtube", target)  # "play lofi on youtube"
    if yt:
        return open_target("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(yt.group(1)),
                           f"a YouTube search for {yt.group(1)}")
    if re.fullmatch(r"(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:" + COMMON_TLDS + r")(?:/\S*)?", target):
        return open_target(shown if target.startswith("http") else "https://" + shown, shown)
    found = pick(target, apps) if len(target) >= 3 else None
    if not found:
        return None
    key, sure = found
    if not sure:
        ok = confirm(f"Did you mean '{key}'?") if confirm else input(f"Did you mean '{key}'? (y/n) ").strip().lower() in ("y", "yes")
        if not ok:
            return "Okay, I won't open anything."
    return open_target(apps[key], key)


def est_tokens(s):
    return len(s) // 4 + 1  # rough: ~4 characters per token in English; avoids a tokenizer dependency


def fit(items, budget, text=str):
    # Keep the newest items that fit the token budget, so the prompt stays small no matter how big memory grows.
    kept, used = [], 0
    for it in reversed(items):
        used += est_tokens(text(it))
        if used > budget:
            break
        kept.append(it)
    return kept[::-1]


# scrypt strength: new files use 2**16 (~64MB, ~100ms). Old files were 2**14 and are still accepted on load.
SCRYPT_N_NEW, SCRYPT_N_OLD = 2**16, 2**14


def _fernet(pw, salt, n):
    from cryptography.fernet import Fernet  # pip install cryptography
    # maxmem is required: n=2**16 needs 64MB, and Python's default limit is 32MB (it raises "memory limit exceeded").
    key = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=8, p=1, dklen=32, maxmem=2**27)  # passphrase -> 256-bit key
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(pw, data):
    salt = os.urandom(16)  # fresh salt each save; file = salt + Fernet token (AES-128-CBC + HMAC)
    return salt + _fernet(pw, salt, SCRYPT_N_NEW).encrypt(data)


def _decrypt(pw, raw):
    from cryptography.fernet import InvalidToken
    if len(raw) < 17:  # 16-byte salt + at least 1 byte of token
        raise ValueError("file too short to be encrypted memory")
    salt, tok = raw[:16], raw[16:]
    for n in (SCRYPT_N_NEW, SCRYPT_N_OLD):
        try:
            return _fernet(pw, salt, n).decrypt(tok)
        except InvalidToken:
            continue
    raise ValueError("wrong passphrase or damaged file")


def _looks_plaintext(raw):
    # True only for a JSON object, so the "confirm passphrase" prompt doesn't fire on a 1-in-256 salt collision.
    if not raw or raw[:1] != b"{":
        return False
    try:
        return isinstance(json.loads(raw), dict)
    except (ValueError, UnicodeDecodeError):
        return False


def load_memory(path=None, pw=None):
    path = path or MEMORY_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"facts": [], "history": [], "emb": {}}
    try:
        d = json.loads(raw)  # plaintext; also how an old unencrypted file gets migrated on the next save
    except ValueError:
        if not pw:
            sys.exit("memory.json is encrypted or damaged, and no passphrase was given. File left untouched.")
        try:
            d = json.loads(_decrypt(pw, raw))
        except Exception:  # wrong passphrase or damaged file; exit so we never overwrite it with empty memory
            sys.exit("Wrong passphrase or damaged memory.json. File left untouched.")
    if not isinstance(d, dict):
        sys.exit("memory.json has an unexpected format. File left untouched.")
    emb = d.get("emb")
    return {"facts": list(d.get("facts", [])), "history": list(d.get("history", [])), "emb": emb if isinstance(emb, dict) else {}}


def save_memory(mem, path=None, pw=None):
    # shortcut: rewrites the whole file each time and keeps the last MAX_HISTORY messages; upgrade to SQLite if it grows.
    path = path or MEMORY_FILE
    mem["history"] = mem["history"][-MAX_HISTORY:]
    data = json.dumps(mem, indent=2, ensure_ascii=False).encode("utf-8")
    if pw:
        data = _encrypt(pw, data)
    tmp = path.with_suffix(".tmp")  # write-then-rename so a crash can't leave a half-written encrypted file
    tmp.write_bytes(data)
    tmp.replace(path)


def open_memory(ask_pw, say):
    """Unlock (or create) the encrypted memory. ask_pw(prompt) returns the typed passphrase, or '' / None to cancel."""
    path = MEMORY_FILE
    pw = ask_pw("Memory passphrase: ")
    if not pw:
        sys.exit("A passphrase is required.")
    # Confirm on first use (no file yet, or an old plaintext one) so a typo can't lock you out of your own memory.
    plain = path.exists() and _looks_plaintext(path.read_bytes())
    if (not path.exists() or plain) and ask_pw("Confirm passphrase: ") != pw:
        sys.exit("Passphrases don't match.")
    mem = None
    for _ in range(3):  # retry wrong passphrases in place: a typo shouldn't exit the whole program
        try:
            mem = load_memory(path, pw=pw)
            break
        except SystemExit as e:
            msg = str(e).lower()
            if "wrong passphrase" in msg or "encrypted or damaged" in msg:
                pw = ask_pw("Try again: ")
                if not pw:
                    sys.exit("Cancelled.")
                continue
            raise
    if mem is None:
        sys.exit("Too many failed attempts.")
    save_memory(mem, path, pw=pw)  # encrypts immediately, including migrating an old plaintext file
    return mem, pw


def recent(mem):
    # Only chatty turns (modes 1, 5, 6, 7) are replayed; code/JSON/document turns are saved but not sent back.
    chat = [h for h in mem["history"] if h.get("mode") in (1, 5, 6, 7)]
    return fit(chat, HISTORY_BUDGET, lambda h: h["content"])


def words(s):
    return set(re.findall(r"[a-z0-9]{3,}", s.lower())) - STOP


def is_statement(h):
    """A saved message you said that states something (not a question or command, and not one of Jarvis's replies)."""
    c = h["content"].strip()
    return h.get("mode") in (1, 5, 6, 7) and h["role"] == "user" and not c.endswith("?") and not c.startswith("/") and len(c.split()) >= 3


def recall(mem, question, skip):
    # Only the newest turns fit HISTORY_BUDGET, so also pull older saved messages that share words with the question.
    # This is the keyword fallback; with an embedding model installed, semantic search is used instead (see ask()).
    q = words(question)
    if not q:
        return []
    scored = []
    for i in range(len(mem["history"]) - 1, -1, -1):  # newest first, so ties favour recent messages
        h = mem["history"][i]
        c = h["content"].strip()
        # Statements you made only: old questions and old (possibly wrong) replies would just repeat themselves.
        if id(h) in skip or h.get("mode") not in (1, 5, 6, 7) or h["role"] != "user" or c.endswith("?") or c.startswith("/"):
            continue
        n = len(q & words(c))
        if n:
            scored.append((n, i, h))
    top = sorted(scored, key=lambda x: -x[0])[:6]
    top.sort(key=lambda x: x[1])  # back to the order they were said
    return fit([h for _, _, h in top], RECALL_BUDGET, lambda h: h["content"])


class _Text(HTMLParser):
    # Minimal HTML -> readable text: drops scripts, styles, menus and footers.
    # shortcut: no JavaScript rendering, so pages that build their content in the browser come back empty.
    SKIP = {"script", "style", "nav", "footer", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html):
    p = _Text()
    p.feed(html)
    return " ".join(p.parts)


UA = {"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"}
ARXIV = "https://export.arxiv.org/api/query"  # free, no key. shortcut: arXiv only (CS, maths, physics, stats, etc.), not all journals.
FILLER = set("find me some a an the research paper papers on about regarding for please give show search get of to "
             "related latest recent can you i want need with and in any good top best arxiv journal".split())


def parse_arxiv(xml):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    clean = lambda s: " ".join((s or "").split())
    return [dict(title=clean(e.findtext("a:title", namespaces=ns)),
                 summary=clean(e.findtext("a:summary", namespaces=ns)),
                 year=clean(e.findtext("a:published", namespaces=ns))[:4],
                 authors=[clean(a.findtext("a:name", namespaces=ns)) for a in e.findall("a:author", ns)],
                 link=clean(e.findtext("a:id", namespaces=ns)))
            for e in ET.fromstring(xml).findall("a:entry", ns)]


def find_papers(text):
    # Returns (context, links). Needs a topic: filler words like "find some research papers on" are dropped first.
    terms = [w for w in re.findall(r"[a-z0-9-]+", text.lower()) if w not in FILLER][:4]
    if not terms:
        return "", []
    for op in (" AND ", " OR "):  # strict match first, looser if nothing comes back
        q = urllib.parse.urlencode({"search_query": op.join("all:" + w for w in terms), "max_results": 5, "sortBy": "relevance"})
        with urllib.request.urlopen(urllib.request.Request(f"{ARXIV}?{q}", headers=UA), timeout=15) as r:
            papers = parse_arxiv(r.read())
        if papers:
            break
    ctx = "\n\n".join(f"{p['title']} ({p['year']}) by {', '.join(p['authors'][:3])}\n{p['summary'][:400]}" for p in papers)
    return ctx, [p["link"] for p in papers]


def page_text(url, limit):
    # Fetch one page and return its first `limit` characters of text; "" on any failure (blocked, slow, not a web page).
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            if not r.headers.get_content_type().startswith("text/"):  # skip PDFs, images, downloads
                return ""
            html = r.read(500_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
        return html_to_text(html)[:limit]
    except Exception:
        return ""


def read_urls(urls):
    # Pages you linked directly. Returns (context, sources) for the pages that could be read.
    with ThreadPoolExecutor(2) as ex:
        pages = list(ex.map(lambda u: page_text(u, LINK_CHARS), urls))
    ok = [(u, t) for u, t in zip(urls, pages) if t]
    return "\n\n".join(f"{u}\n{t}" for u, t in ok), [u for u, _ in ok]


def web_search(query):
    # DuckDuckGo results; the top 3 pages are read (first PAGE_CHARS each), the rest contribute their snippet.
    # The second return value is a Google-style result list (title, link, snippet) printed under the answer.
    from ddgs import DDGS  # pip install ddgs
    hits = DDGS().text(query, max_results=6)
    with ThreadPoolExecutor(3) as ex:
        pages = list(ex.map(lambda h: page_text(h["href"], PAGE_CHARS), hits[:3]))
    pages += [""] * (len(hits) - len(pages))
    ctx = "\n\n".join(f"{h['title']}\n{p or h['body']}" for h, p in zip(hits, pages))
    results = [f"{i}. {h['title']}\n   {h['href']}\n   {' '.join(h['body'].split())[:150]}" for i, h in enumerate(hits, 1)]
    return ctx, results


def safe_print(s="", end="\n"):
    """print() that can't crash on a console that can't show emoji (e.g. an old Windows code page): shows '?' instead."""
    try:
        print(s, end=end, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(s.encode(enc, "replace").decode(enc, "replace"), end=end, flush=True)


def _print_token(s):
    safe_print(s, end="")


def ask(mode, text, mem=None, emit=None, sem=None):
    """One model call. `emit` receives streamed text (default: print). `sem` is an optional SemanticMemory for recall."""
    emit = emit or _print_token
    m = MODES[mode]
    system = BASE + f" Now: {datetime.now():%A %d %B %Y, %H:%M}.\n\n" + m["prompt"]
    history = []
    if mem is not None:  # chatty (1) and grounded (3, 7, 8) modes; code (2) and JSON (4) stay stateless
        all_facts = mem["facts"]
        hits = None
        if sem is not None and mode not in GROUNDED and sem.enabled:
            question = text.split("</context>")[-1]
            statements = [h for h in mem["history"] if is_statement(h)][-sem.max_indexed:] if sem.max_indexed else []
            cands = [(f, "fact") for f in all_facts] + [(h["content"].strip(), "msg") for h in statements if h["content"].strip() not in all_facts]
            try:
                hits = sem.search(mem, question, cands)
            except semantic.EmbedError as e:
                log.warning("semantic search unavailable: %s", e)
        shown_facts = fit(all_facts, FACT_BUDGET)
        if hits is not None and len(all_facts) > 8:  # lots of facts: send the relevant ones, not all of them
            shown_facts = fit([t for _, t, k in reversed(hits) if k == "fact"], FACT_BUDGET)
        if shown_facts:
            system += "\n\nKnown about the user:\n" + "\n".join("- " + f for f in shown_facts)
        if mode not in GROUNDED:  # grounded modes answer only from <context>; don't replay history or recall
            kept = recent(mem)
            history = [{"role": h["role"], "content": h["content"]} for h in kept]
            if hits is not None:  # meaning-based recall of older things you said
                kept_text = {h["content"].strip() for h in kept}
                old = [t for _, t, k in hits if k == "msg" and t not in kept_text]
                old = fit(old, RECALL_BUDGET, lambda t: t)
                if old:
                    system += "\n\nThings the user told you earlier:\n" + "\n".join(f"- you said: {t[:300]}" for t in old)
            else:  # keyword fallback
                old = recall(mem, text.split("</context>")[-1], {id(h) for h in kept})
                if old:
                    system += "\n\nThings the user told you earlier:\n" + "\n".join(f"- you said: {h['content'][:300]}" for h in old)
    options = {"temperature": m["temp"], "num_ctx": NUM_CTX}
    if MAX_OUT:
        options["num_predict"] = MAX_OUT
    body = {
        "model": MODEL, "stream": True, "think": THINK, "keep_alive": KEEP_ALIVE,  # think off by default: Qwen3.5 reasons first, which delays every reply
        "messages": [{"role": "system", "content": system}] + history + [{"role": "user", "content": text}],
        "options": options,  # num_ctx 6144 fits three web pages plus memory; Ollama's JSON mode makes mode 4 output valid JSON
    }
    if mode == 4:
        body["format"] = "json"
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    out = []
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # first call may need time to load the model
        for line in r:
            piece = json.loads(line).get("message", {}).get("content", "")
            out.append(piece)
            if mode not in (2, 4):  # modes 2 and 4 are cleaned up first, then printed
                emit(piece)
    return "".join(out).strip()


def ask_json(messages, schema, timeout=60):
    """A short, structured model call used to choose a tool. Returns the parsed JSON or None. Never raises."""
    body = {"model": MODEL, "stream": False, "think": False, "format": schema, "keep_alive": KEEP_ALIVE,
            "messages": messages, "options": {"temperature": 0, "num_ctx": NUM_CTX}}
    try:
        req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(json.load(r)["message"]["content"])
    except Exception as e:
        log.warning("tool-choice call failed: %s", e)
        return None


# ======================================================================================================================
# Session: one object that holds everything for a conversation, used by both the terminal and the window.
# ======================================================================================================================
class TerminalIO:
    done = False  # set by quit(); the chat loop stops when it becomes True

    def quit(self):
        self.done = True

    def write(self, s=""):
        safe_print(s)

    def token(self, s):
        safe_print(s, end="")

    def status(self, s):
        pass

    def confirm(self, q):
        return input(f"{q} (y/n) ").strip().lower() in ("y", "yes")

    def notify(self, s):  # background reminders arrive while you may be typing
        safe_print("\n" + s + "\n> ", end="")


HELP = """Things you can say or type:
  Chat, ask anything. Questions that need fresh facts search the web by themselves.
  calculations      what is 23 x 47?   20% of 150   sqrt(144)       (/calc <expression>)
  reminders         remind me in 10 minutes to stretch   set a 25-minute timer   list reminders   cancel reminder 2   done with reminder 2
  facts             /remember my name is Sam   /facts   /forget <number>   (I also notice things like "my name is ...")
  browser           open youtube in brave   search lofi on youtube   open github.com in chrome
  apps              open obs   play lofi on youtube   /apps [word]
  documents         /ctx <file.txt|pdf|docx> <question>        (put the path in quotes if it has spaces)
  PC                set volume to 40   volume up   mute   take a screenshot   lock my screen
  voice             /voice on|off|status   /listen
  other             /web <q>  /papers <topic>  /chat <text>  /memory [word]  /forget (clears chat history)  /update  /tools  /help
  quit              exit   (or quit, bye, /exit)"""


class Session:
    def __init__(self, cfg, mem, pw, io=None, apps=None):
        self.cfg, self.mem, self.pw, self.io = cfg, mem, pw, io or TerminalIO()
        self.apps = apps if apps is not None else find_apps()
        o = cfg["ollama"]
        self.embedder = semantic.Embedder(o["base_url"], o["embedding_model"], query_prefix=o["embedding_query_prefix"], doc_prefix=o["embedding_document_prefix"])
        s = cfg["semantic"]
        self.sem = semantic.SemanticMemory(self.embedder, s["min_similarity"], s["top_k"], s["max_indexed_messages"]) if s["enabled"] else None
        self.docs = documents.DocumentStore(cfg["documents"], self.embedder if self.sem is not None and o["embedding_model"] else None)
        self.voice = voice_mod.Voice(cfg["voice"])
        self.detected = browsers.detect_browsers()
        BROWSER.update(preferred=cfg["browser"]["preferred"], detected=self.detected)
        self.store = reminders.ReminderStore(cfg["reminders"]["file"])
        self.scheduler = reminders.ReminderScheduler(self.store, self._notify, cfg["reminders"]["poll_seconds"])
        self._volume = None
        self._sem_hint_shown = False
        self.registry = self._build_registry()
        atexit.register(self.close)

    # ---- plumbing ----
    def say(self, s=""):
        self.io.write(s)
        self.voice.speak(s)

    def save(self):
        if self.sem is not None and len(self.mem.get("emb", {})) > 300 + 2 * (len(self.mem["facts"]) + len(self.mem["history"])):
            keep = list(self.mem["facts"]) + [h["content"].strip() for h in self.mem["history"] if is_statement(h)]
            self.sem.prune(self.mem, keep)
        save_memory(self.mem, Path(self.cfg["memory"]["file"]), pw=self.pw)

    def start(self):
        if self.store.warning:
            self.io.write(self.store.warning)
        self.scheduler.start()

    def close(self):
        try:
            self.scheduler.stop()
            self.voice.shutdown()
        except Exception:
            pass

    def _notify(self, item):
        late = " (this went off while Jarvis was closed or the PC was asleep)" if item.get("late") else ""
        label = "Timer" if item["kind"] == "timer" else "Reminder"
        self.io.notify(f"⏰ {label}: {item['text']}{late}")
        if self.cfg["reminders"]["beep"]:
            try:
                import winsound
                winsound.MessageBeep()
            except Exception:
                pass
        self.voice.speak(f"{label}: {item['text']}")

    # ---- tools (the only things the model may ask for) ----
    def _build_registry(self):
        P, T = toolreg.Param, toolreg.Tool
        r = toolreg.ToolRegistry(log)
        r.register(T("calculator", "Exact maths. Use for any calculation.", self._t_calc, {"expression": P("string", "e.g. 23*47", max_length=200)}))
        r.register(T("memory_search", "Look up something the user told you before (their saved facts and past statements).", self._t_memory,
                     {"query": P("string", "what to look for", max_length=200)}))
        r.register(T("set_reminder", "Remind the user of something, or start a timer, after some minutes.", self._t_set_reminder,
                     {"text": P("string", "what to remind", max_length=200), "minutes": P("number", "from now", minimum=0.1, maximum=525600),
                      "kind": P("string", "reminder or timer", required=False, enum=("reminder", "timer"))}))
        r.register(T("list_reminders", "Show the active reminders and timers.", self._t_list_reminders))
        r.register(T("cancel_reminder", "Cancel a reminder or timer by its number.", self._t_cancel, {"id": P("integer", minimum=1, maximum=10**9)}))
        r.register(T("complete_reminder", "Mark a reminder as done by its number.", self._t_complete, {"id": P("integer", minimum=1, maximum=10**9)}))
        r.register(T("open_website", "Open a website, or search a site, in a browser.", self._t_website,
                     {"site": P("string", "site name", required=False, enum=tuple(sorted(browsers.SITE_HOMES))),
                      "url": P("string", "a full https:// address", required=False, max_length=500, pattern=r"https?://\S+"),
                      "query": P("string", "search text (needs site)", required=False, max_length=200),
                      "browser": P("string", "which browser", required=False, enum=("default", "brave", "chrome", "edge", "firefox", "opera"))}))
        r.register(T("open_app", "Open an installed app or game by name.", self._t_open_app, {"name": P("string", max_length=80)}))
        r.register(T("ask_document", "Answer a question about a document file (txt, pdf, docx).", self._t_doc,
                     {"path": P("string", max_length=500), "question": P("string", max_length=300)}))
        r.register(T("voice_control", "Turn voice input/output on or off, or report its status.", self._t_voice,
                     {"action": P("string", enum=("on", "off", "status"))}))
        r.register(T("set_volume", "Get, set, raise, lower, mute or unmute the PC volume.", self._t_volume,
                     {"action": P("string", enum=("get", "set", "up", "down", "mute", "unmute")),
                      "level": P("integer", "0-100, for set", required=False, minimum=0, maximum=100)}))
        r.register(T("take_screenshot", "Save a screenshot of the screen.", self._t_screenshot))
        r.register(T("lock_screen", "Lock the PC screen.", self._t_lock, sensitive=True,
                     confirm_text=lambda a: "Lock your screen now? (you'll need your password to get back in)"))
        return r

    def run_tool(self, name, args):
        self.io.status(f"Running {name}...")
        res = self.registry.run(name, args, self.io.confirm, self.cfg["tools"]["timeout_seconds"])
        log.info("tool %s %s -> %s", name, args, "ok" if res.ok else res.message)
        self.say(res.message if res.ok else f"Sorry, that didn't work: {res.message}")
        return res.ok

    def _t_calc(self, a):
        try:
            expr = calculator.normalize(a["expression"])
            return f"{calculator.display(a['expression'])} = {calculator.evaluate(expr)}"
        except calculator.CalcError as e:
            raise toolreg.ToolError(f"I couldn't calculate that: {e}")

    def _memory_hits(self, query):
        cands = [(f, "fact") for f in self.mem["facts"]] + [(h["content"].strip(), "msg") for h in self.mem["history"] if is_statement(h)][-300:]
        if self.sem is not None and self.sem.enabled:
            try:
                return [t for _, t, _ in self.sem.search(self.mem, query, cands)]
            except semantic.EmbedError:
                pass
        q = words(query)
        return [t for t, _ in cands if q & words(t)][:5]

    def _t_memory(self, a):
        hits = self._memory_hits(a["query"])
        return "Here's what I have:\n" + "\n".join(f"  - {t}" for t in hits) if hits else "I don't have anything saved about that."

    def _create_reminder(self, kind, text, due):
        item = self.store.add(kind, text, due)
        return f"Okay! {kind.capitalize()} #{item['id']} set for {due:%a %H:%M}: {text}."

    def _t_set_reminder(self, a):
        from datetime import timedelta
        return self._create_reminder(a.get("kind", "reminder"), a["text"], datetime.now() + timedelta(minutes=a["minutes"]))

    def _list_text(self):
        items = self.store.active()
        lines = [reminders.describe(i) for i in items] or ["You have no active reminders or timers."]
        if items:
            lines.append("(Reminders only ring while Jarvis is running; anything missed shows up when it's next open.)")
        return "\n".join(lines)

    def _t_list_reminders(self, a):
        return self._list_text()

    def _t_cancel(self, a):
        try:
            return f"Cancelled #{a['id']}: {self.store.cancel(a['id'])['text']}."
        except reminders.ReminderError as e:
            raise toolreg.ToolError(str(e))

    def _t_complete(self, a):
        try:
            return f"Marked #{a['id']} as done: {self.store.complete(a['id'])['text']}."
        except reminders.ReminderError as e:
            raise toolreg.ToolError(str(e))

    def _t_website(self, a):
        if a.get("query"):
            if not a.get("site"):
                raise toolreg.ToolError("tell me which site to search (youtube, google, wikipedia...)")
            cmd = {"kind": "search", "site": a["site"], "query": a["query"], "url": None, "browser": a.get("browser")}
        elif a.get("site"):
            cmd = {"kind": "open", "site": a["site"], "query": None, "url": browsers.SITE_HOMES[a["site"]], "browser": a.get("browser")}
        elif a.get("url"):
            cmd = {"kind": "open", "site": None, "query": None, "url": a["url"], "browser": a.get("browser")}
        else:
            raise toolreg.ToolError("I need a site or a web address")
        return self._browser(cmd)

    def _browser(self, cmd):
        try:
            return browsers.run_command(cmd, self.cfg["browser"]["preferred"], self.detected, self.cfg["browser"]["fallback_to_default"], self.io.confirm)
        except browsers.BrowserError as e:
            raise toolreg.ToolError(f"I couldn't open that: {e}")

    def _t_open_app(self, a):
        found = pick(a["name"].lower(), self.apps) if len(a["name"]) >= 3 else None
        if not found or not found[1]:
            raise toolreg.ToolError(f"I don't have an app called '{a['name']}'. Try /apps to see what I can open.")
        msg = open_target(self.apps[found[0]], found[0])
        if msg.startswith("Hmm"):
            raise toolreg.ToolError(msg)
        return msg

    def _t_doc(self, a):
        self._doc_answer(a["path"], a["question"])
        return "(answered from the document)"

    def _t_voice(self, a):
        if a["action"] == "on":
            self.voice.enabled = True
        elif a["action"] == "off":
            self.voice.enabled = False
        return self.voice.status()

    def _t_volume(self, a):
        try:
            if self._volume is None:
                self._volume = pc.WindowsVolume()
            v, act = self._volume, a["action"]
            step = self.cfg["pc"]["volume_step"]
            if act == "get":
                return f"The volume is {pc.get_volume(v)}%" + (" (muted)." if v.get_mute() else ".")
            if act == "set":
                if "level" not in a:
                    raise toolreg.ToolError("what volume (0 to 100)?")
                return f"Volume is now {pc.set_volume(v, a['level'])}%."
            if act in ("up", "down"):
                return f"Volume is now {pc.change_volume(v, step if act == 'up' else -step)}%."
            pc.set_mute(v, act == "mute")
            return "Muted." if act == "mute" else "Unmuted."
        except pc.PCError as e:
            raise toolreg.ToolError(str(e))

    def _t_screenshot(self, a):
        try:
            return f"Saved a screenshot to {pc.screenshot(self.cfg['pc']['screenshot_dir'])}"
        except pc.PCError as e:
            raise toolreg.ToolError(str(e))

    def _t_lock(self, a):
        try:
            return pc.lock_screen()
        except pc.PCError as e:
            raise toolreg.ToolError(str(e))

    # ---- one message in ----
    def handle(self, text):
        text = (text or "").strip()
        if not text:
            return
        text = fix_command(text, self.io.write)
        if text.lower() in ("exit", "quit", "bye", "/exit", "/quit", "/bye"):
            self.io.write("Bye for now!")
            getattr(self.io, "quit", lambda: None)()
            return
        try:
            if self._command(text) or self._natural(text):
                return
            self._learn(text)
            if self._model_tools(text):
                return
            self._chat(text, text)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:  # last resort: one bad message must never end the session
            log.exception("unhandled error while handling a message")
            self.io.write(f"Oops, something went wrong ({type(e).__name__}: {e}). It's in jarvis.log if you want details.")

    # ---- slash commands ----
    def _command(self, text):
        m = REMEMBER.match(text)
        if m:
            self._remember(m.group(1))
            return True
        if not text.startswith("/"):
            return False
        cmd, _, rest = text.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        usage = {"/remember": "/remember <something about you>", "/web": "/web <question>", "/papers": "/papers <topic>", "/chat": "/chat <text>",
                 "/ctx": "/ctx <file> <question>", "/calc": "/calc <expression>", "/cancel": "/cancel <reminder number>", "/done": "/done <reminder number>"}
        if cmd in usage and not rest:
            self.io.write(f"Usage: {usage[cmd]}")
            return True
        if cmd == "/facts":
            self.io.write("\n".join(facts.listing(self.mem)) if self.mem["facts"] else "I haven't saved any facts yet. Try: /remember my name is Sam")
        elif cmd == "/forget":
            self._forget(rest)
        elif cmd == "/memory":
            self._memory_cmd(rest)
        elif cmd == "/update":
            check_updates(self.io.write)
        elif cmd == "/apps":
            names = sorted(k for k in self.apps if rest.lower() in k)
            self.io.write(f"{len(names)} apps and sites I can open" + (f" matching '{rest}'" if rest else "") + ":")
            self.io.write("  " + ", ".join(names[:60]) + (" ..." if len(names) > 60 else ""))
        elif cmd == "/calc":
            self.run_tool("calculator", {"expression": rest})
        elif cmd == "/reminders":
            self.io.write(self._list_text())
        elif cmd in ("/cancel", "/done"):
            if not rest.lstrip("#").isdigit():
                self.io.write(f"Usage: {usage[cmd]}")
            else:
                self.run_tool("cancel_reminder" if cmd == "/cancel" else "complete_reminder", {"id": int(rest.lstrip("#"))})
        elif cmd == "/ctx":
            self._ctx(rest)
        elif cmd == "/voice":
            self._voice_cmd(rest)
        elif cmd == "/listen":
            self._listen()
        elif cmd == "/tools":
            self.io.write("Tools the model can ask me to use (each is checked first):\n" + self.registry.prompt())
        elif cmd == "/help":
            self.io.write(HELP)
        else:
            return False  # /web, /papers, /chat carry on into normal chat handling
        return True

    def _remember(self, text):
        fact = facts.clean(text)
        if fact.endswith("?"):
            self.io.write("That looks like a question. To save something, tell me, like: /remember my name is Sam")
            return
        verdict = facts.classify(fact)
        if verdict == "never":
            self.io.write("I won't save that: it looks like a password, ID or card number.")
            return
        if verdict == "sensitive" and not self.io.confirm("This looks personal. Save it anyway?"):
            self.io.write("Okay, I won't save it.")
            return
        status, old = facts.add_fact(self.mem, fact)
        self.io.write({"added": "Got it, I'll remember that!", "duplicate": "I already have that one.",
                       "updated": f"Updated: '{old}' is now '{fact}'.", "empty": "There's nothing to save there."}[status])
        if status in ("added", "updated"):
            self.save()

    def _forget(self, rest):
        if rest in ("", "history"):
            self.mem["history"] = []
            self.save()
            self.io.write("Done, I cleared the saved chat history. Your /remember facts are still here (see /facts).")
        elif rest.lstrip("#").isdigit():
            n = int(rest.lstrip("#"))
            if not 1 <= n <= len(self.mem["facts"]):
                self.io.write(f"There's no fact number {n}. You have {len(self.mem['facts'])} (see /facts).")
            elif self.io.confirm(f"Delete fact {n}: '{self.mem['facts'][n - 1]}'?"):
                gone = facts.delete_fact(self.mem, n)
                self.save()
                self.io.write(f"Deleted: {gone}")
            else:
                self.io.write("Okay, I kept it.")
        else:
            self.io.write("Usage: /forget <fact number>  (see /facts)    or    /forget   to clear the chat history")

    def _memory_cmd(self, arg):
        arg = arg.lower()
        shown = [h for h in self.mem["history"] if arg in h["content"].lower()][-10:] if arg else self.mem["history"][-6:]
        self.io.write(f"{len(self.mem['facts'])} saved facts, {len(self.mem['history'])} saved messages (memory.json is encrypted, so this is how to read it).")
        for f in self.mem["facts"]:
            self.io.write(f"  fact: {f}")
        for h in shown:
            self.io.write(f"  {h['role']}: {h['content'][:100]}")

    def _voice_cmd(self, arg):
        a = arg.lower()
        if a == "on":
            self.voice.enabled = True
        elif a == "off":
            self.voice.enabled = False
        elif a == "speak on":
            self.voice.speak_replies = True
        elif a == "speak off":
            self.voice.speak_replies = False
        elif a not in ("", "status"):
            self.io.write("Usage: /voice on | off | status | speak on | speak off")
            return
        self.io.write(self.voice.status())

    def _listen(self):
        try:
            heard = self.voice.listen()
        except voice_mod.VoiceError as e:
            self.io.write(f"Voice: {e}")
            return
        heard = voice_mod.spoken_to_text(heard)  # "twenty five" -> "25", so timers, volume and sums work by voice
        self.io.write(f"(heard: {heard})")
        self.handle(heard)

    # ---- natural-language actions (deterministic, run before the model sees anything) ----
    def _natural(self, text):
        if text.startswith("/"):
            return False
        ok, reply = calculator.try_calculate(text)
        if ok:
            self.say(reply)
            return True
            
        # --- NEW: Intercept "open [platform] and play/search [query]" ---
        m = re.search(r"(?:open|launch|go to|start)\s+([a-z0-9]+)\s+(?:and|then)\s+(?:play|search(?:\s+for)?|find|look up)\s+(.+)", text, re.I)
        if m:
            site = m.group(1).lower()
            query = m.group(2).strip()
            # If it's a known site we can search, do it directly!
            if site in ("youtube", "spotify", "google", "github", "reddit", "netflix"):
                cmd = {"kind": "search", "site": site, "query": query, "url": None, "browser": None}
                try:
                    self.say(self._browser(cmd))
                except toolreg.ToolError as e:
                    self.say(str(e))
                return True
        # ----------------------------------------------------------------

        try:
            mg = reminders.parse_management(text)
            if mg:
                if mg[0] == "list":
                    self.say(self._list_text())
                else:
                    self.run_tool("cancel_reminder" if mg[0] == "cancel" else "complete_reminder", {"id": mg[1]})
                return True
            rem = reminders.parse(text)
        except reminders.ReminderError as e:
            self.say(f"I couldn't set that: {e}.")
            return True
        if rem:
            self.say(self._create_reminder(rem["kind"], rem["text"], rem["due"]))
            return True
        cmd = browsers.parse_command(text)
        if cmd:
            try:
                self.say(self._browser(cmd))
            except toolreg.ToolError as e:
                self.say(str(e))
            return True
        pcc = pc.parse_command(text)
        if pcc:
            self.run_tool(*pcc)
            return True
        msg = launch(text, self.apps, self.io.confirm)
        if msg:
            self.say(msg)
            return True
        return False

    def _learn(self, text):
        if facts.learn(self.mem, text, self.cfg["memory"]["auto_save_facts"], self.io.confirm, self.io.write):
            self.save()

    def _model_tools(self, text):
        mode = self.cfg["tools"]["model_routing"]
        if mode == "off" or text.startswith("/") or route(text) not in (1, 5):
            return False  # code, JSON, document and link requests have their own handling
        if mode == "auto" and not toolreg.looks_actionable(text):
            return False
        t = self.cfg["tools"]
        self.io.status("Choosing a tool...")
        return toolreg.run_model_tools(self.registry, text, lambda m, s: ask_json(m, s, t["timeout_seconds"]), self.io.confirm, self.say,
                                       t["max_tool_calls"], t["chain"], t["timeout_seconds"], log)

    # ---- documents ----
    def _ctx(self, rest):
        try:
            path, question = documents.parse_args(rest)
            if not question:
                self.io.write("Usage: /ctx <file> <question>   (put the file name in quotes if it has spaces)")
                return
            self._doc_answer(path, question, raw=f"/ctx {rest}")
        except documents.DocError as e:
            self.io.write(f"Document: {e}")

    def _doc_answer(self, path, question, raw=None):
        try:
            self.io.status("Reading the document...")
            ctx, sources, notes = self.docs.context_for(path, question)
        except documents.DocError as e:
            raise toolreg.ToolError(f"Document: {e}") if raw is None else e
        for n in notes:
            self.io.write(f"(note: {n})")
        if not ctx:
            self.io.write("Hmm, I couldn't find anything about that in the document.")
            return
        self.io.write("[mode 8: answering from the document]")
        out = self._call_model(8, f"<context>{ctx}</context> {question}")
        if out is None:
            return
        self.io.write()
        self.io.write("From: " + ", ".join(f"[{s}]" for s in sources))
        self._remember_turn(raw or f"/ctx {path} {question}", out, 8)

    # ---- normal chat (modes 1-7) ----
    def _call_model(self, mode, text, use_mem=True):
        try:
            return ask(mode, text, self.mem if use_mem else None, emit=self.io.token, sem=self.sem if mode not in GROUNDED else None)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.io.write(f"Model '{MODEL}' not found. Pull it with: ollama pull {MODEL}")
            else:
                self.io.write(f"Ollama returned HTTP {e.code}: {e.reason}")
        except urllib.error.URLError:
            self.io.write("Ollama is not reachable. Start it, then run: ollama pull " + MODEL)
        except Exception as e:  # streaming parse errors, timeouts, anything else: keep going
            log.exception("model call failed")
            self.io.write(f"Oops, the model call failed ({e}).")
        return None

    def _remember_turn(self, raw, out, mode):
        self.mem["history"] += [{"role": "user", "content": raw, "mode": mode}, {"role": "assistant", "content": out, "mode": mode}]
        self.save()
        self._semantic_hint()

    def _semantic_hint(self):
        if self.sem is not None and not self._sem_hint_shown and self.embedder.last_error:
            self._sem_hint_shown = True
            self.io.write(f"(Memory search is using plain keywords for now: {self.embedder.last_error})")

    def _chat(self, text, raw):
        forced = next((m for p, m in (("/web ", 5), ("/papers ", 7), ("/chat ", 1)) if text.startswith(p)), None)
        if forced:
            text = text.split(maxsplit=1)[1]
        mode = forced or route(text)
        self.io.write(f"[mode {mode}]")
        orig, sources = mode, []
        if mode in (5, 6, 7):
            try:
                self.io.status("Looking that up...")
                if mode == 5:
                    ctx, sources = web_search(text)
                elif mode == 6:
                    ctx, sources = read_urls([u.rstrip(".,;:!?") for u in LINK.findall(text)[:2]])
                else:
                    ctx, sources = find_papers(text)
            except ImportError:
                self.io.write("Run: pip install ddgs")
                return
            except Exception as e:  # network down, rate-limited, etc.
                self.io.write(f"Oops, I couldn't fetch that ({e}). Check your internet connection?")
                return
            if not sources:
                self.io.write("I couldn't find anything for that. For papers, try giving me a topic, like /papers model calibration.")
                return
            # answer strictly from what was fetched; papers get their own list-style prompt (mode 7)
            text, mode = f"<context>{ctx}</context> {text}", (7 if mode == 7 else 3)
        self.io.status("Thinking...")
        out = self._call_model(mode, text, use_mem=orig in CHATTY)
        if out is None:
            return
        if mode in (2, 4):
            out = tidy(out) if mode == 4 else only_code(out)
            self.io.write(out)
        else:
            self.io.write()
        self.voice.speak(out)
        self._remember_turn(raw, out, orig)
        if sources:
            self.io.write("Results:\n" + "\n".join(sources))


def selftest():
    assert route("<context>x</context> hi") == 3
    assert route("Extract as JSON: restart") == 4
    assert route("write a function to ping") == 2
    assert route("what is the capital of france") == 1  # common knowledge is answered directly
    assert route("what is 2+2") == 1
    assert route("tell me a joke") == 1
    assert route("what is the date today") == 1  # clock is in the prompt
    assert route("who is the current president of india") == 5
    assert route("latest news on AI") == 5
    assert route("search for a good laptop") == 5
    assert route("find the derivative of x squared") == 1
    assert route("what's the weather in Bengaluru") == 5
    assert route("who won the match last night") == 5
    assert route("write a function to search files") == 2  # code beats web
    assert route("summarize https://example.com/a.") == 6
    assert route("summarize https://example.com/page.html") == 6  # a .html in a URL is a page, not a code file
    assert route("write a script to download https://example.com") == 2  # code beats link
    assert route("find some research papers on model calibration") == 7
    assert route("find me a good pizza place") == 5
    assert route("hello there") == 1
    assert route("what is my name") == 1  # personal: uses saved facts
    assert route("good morning jarvis") == 1
    assert route("how do I read a file in python") == 2  # verb+language
    assert route("what is python") == 1                   # bare language name stays chat
    assert route("refactor this function") == 2           # strong noun
    assert route("fix my main.py") == 2                   # filename extension
    assert route("open example.xyz") == 1                 # would be handled by launch() first
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.04599v2</id>'
           '<published>2017-06-14T00:00:00Z</published><title>On Calibration\n of Modern Neural Networks</title>'
           '<summary> Confidence calibration matters. </summary><author><name>Chuan Guo</name></author></entry></feed>')
    p = parse_arxiv(xml)[0]
    assert (p["title"], p["year"], p["authors"], p["link"]) == ("On Calibration of Modern Neural Networks", "2017", ["Chuan Guo"], "http://arxiv.org/abs/1706.04599v2")
    assert html_to_text("<nav>menu</nav><script>x=1</script><p>Hello <b>world</b></p><footer>f</footer>") == "Hello world"
    assert tidy('{"name":"a"}') == '{"name": "a", "action": "", "parameters": [], "urgent": false}'
    assert fit(["a" * 40, "b" * 40, "c" * 40], 25) == ["b" * 40, "c" * 40]  # newest kept, oldest dropped
    assert REMEMBER.match("/remember my laptop has a 3050").group(1) == "my laptop has a 3050"
    assert REMEMBER.match("remember that I like tea")
    assert not REMEMBER.match("remember what I said")
    assert _looks_plaintext(b'{"facts":[],"history":[]}')
    assert not _looks_plaintext(b"\x00\x01salt") and not _looks_plaintext(b"not json") and not _looks_plaintext(b"")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "m.json"
        assert load_memory(p)["facts"] == [] and load_memory(p)["history"] == []  # missing file is fine
        mem = {"facts": ["x"], "history": [{"role": "user", "content": "hi", "mode": 1}], "emb": {}}
        save_memory(mem, p)
        assert load_memory(p) == mem
        try:
            import cryptography  # noqa: F401
        except ImportError:
            print("(skipped encryption test: pip install cryptography)")
        else:
            save_memory(mem, p, "secret")
            assert b'"facts"' not in p.read_bytes()  # ciphertext, not readable JSON
            assert load_memory(p, "secret") == mem
            try:
                load_memory(p, "wrong")
                assert False, "wrong passphrase must not load"
            except SystemExit:
                pass
            assert load_memory(p, "secret") == mem  # file untouched after the failed attempt
            salt = os.urandom(16)  # backward compat: a file written with the old scrypt n=2**14 must still load
            p.write_bytes(salt + _fernet("secret", salt, SCRYPT_N_OLD).encrypt(json.dumps(mem).encode("utf-8")))
            assert load_memory(p, "secret") == mem
    assert only_code("Sure!\n```python\nprint(1)\n```\nThis prints 1.\n```") == "```python\nprint(1)\n```"
    assert only_code("no fence here") == "no fence here"
    old_mem = {"facts": [], "history": [{"role": "user", "content": "My project is about calibration of neural networks", "mode": 1}]
               + [{"role": "user", "content": f"filler message number {i} with padding text here", "mode": 1} for i in range(200)]}
    kept = recent(old_mem)
    assert all("calibration" not in h["content"] for h in kept)  # pushed out of the replay window
    found = recall(old_mem, "what is my project about?", {id(h) for h in kept})
    assert found and "calibration" in found[0]["content"]  # ...but recalled from the saved file
    assert route("what did I tell you about my current project") == 1  # memory questions skip the web
    assert route("weather near me") == 5
    assert fix_command("/rembemer my name is Sam", lambda s: None) == "/remember my name is Sam"
    assert fix_command("/hello there", lambda s: None) == "/hello there"  # unrelated slashes are left alone
    apps = {"obs studio": "a", "steam": "b", "valorant": "c", "jobs app": "d"}
    assert pick("obs", apps) == ("obs studio", True) and pick("steam", apps) == ("steam", True)
    assert pick("valorent", apps) == ("valorant", False) and pick("zzzz", apps) is None  # fuzzy: needs your confirmation
    m = LAUNCH.match("can you launch OBS for me!")
    assert (m.group(1), m.group(2)) == ("launch", "OBS")
    assert launch("tell me a joke", apps) is None and launch("start with python basics", apps) is None
    assert calculator.try_calculate("what is 23 x 47?") == (True, "23 x 47 = 1081")
    print("selftest ok")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Jarvis, a friendly local helper")
    ap.add_argument("--gui", action="store_true", help="open the desktop window instead of the terminal chat")
    ap.add_argument("--selftest", action="store_true", help="run quick built-in checks and exit")
    ap.add_argument("--config", default=str(HERE / "config.json"), help="path to config.json")
    ap.add_argument("--write-config", action="store_true", help="write a config.json containing every default, then exit")
    args = ap.parse_args(argv)
    if args.selftest:
        selftest()
        return
    if args.write_config:
        config_mod.write_template(args.config)
        print(f"Wrote {args.config}")
        return
    try:
        cfg, warnings = config_mod.load_config(args.config, HERE)
    except config_mod.ConfigError as e:
        sys.exit(f"Config problem: {e}")
    apply_config(cfg)
    setup_logging(cfg)
    for w in warnings:
        print(f"(config: {w})")
    try:
        import cryptography  # noqa: F401
    except ImportError:
        sys.exit("Run: pip install cryptography")
    if args.gui:
        import gui
        return gui.run(cfg, lambda ask_pw, io: _start_session(cfg, ask_pw, io))
    import getpass
    io = TerminalIO()
    session = _start_session(cfg, getpass.getpass, io)
    io.write(f"Hey! Jarvis is ready ({MODEL}). Type /help to see what I can do, or exit to quit.")
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        try:
            session.handle(text)
        except KeyboardInterrupt:  # Ctrl+C while Jarvis is busy stops that reply, not the whole program
            io.write("\n(stopped)")
        if io.done:
            break
    session.close()


def _start_session(cfg, ask_pw, io):
    mem, pw = open_memory(ask_pw, io.write)
    session = Session(cfg, mem, pw, io)
    session.start()
    return session


if __name__ == "__main__":
    main()