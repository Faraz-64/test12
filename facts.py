"""Saved facts: detect, add (no duplicates, updates replace old values), list, delete.

Facts live in mem["facts"] (a list of short sentences) inside the encrypted memory file, so they persist and stay private.
"""
import re

# Never stored, even if asked: secrets that would hurt if memory.json ever leaked.
NEVER = re.compile(
    r"\b(password|passcode|passphrase|pin(?: number| code)?|otp|cvv|cvc|card number|credit card|debit card|"
    r"aadhaar|aadhar|passport(?: number)?|ssn|social security|pan card|bank account|account number|ifsc|api[ _-]?key|secret key|private key)\b"
    r"|\b\d(?:[ -]?\d){11,18}\b", re.I)

# Stored only after you confirm.
SENSITIVE = re.compile(
    r"\b(diagnos\w*|depress\w*|anxiety|medication|medicine|prescri\w*|disease|illness|cancer|diabet\w*|hiv|therapy|therapist|"
    r"religion|religious|muslim|hindu|christian|sikh|buddhist|jewish|atheist|politic\w*|party member|vote|voted|"
    r"gay|lesbian|bisexual|transgender|sexual\w*|pregnan\w*|salary|income|loan|debt|owe|mortgage|"
    r"phone number|mobile number|email address|home address|my address|live at|date of birth|birthday|dob)\b|\b[\w.+-]+@[\w-]+\.[\w.]+\b|\b\d{10}\b", re.I)

_VAL = r"([^,;!?\n]{1,80}?)"  # dots allowed inside values (B.Tech); sentence-ending dots are split off first
# (slot, confidence, regex, canonical-format). Order matters: first match wins for a clause.
PATTERNS = [
    ("name", "high", re.compile(r"\bmy name is " + _VAL + r"\s*$", re.I), "my name is {0}"),
    ("study", "high", re.compile(r"\bi(?: am|'m)? (?:currently )?(?:studying|doing|pursuing) " + _VAL + r"\s*$", re.I), "I study {0}"),
    ("study", "high", re.compile(r"\bi study " + _VAL + r"\s*$", re.I), "I study {0}"),
    ("location", "high", re.compile(r"\bi live in " + _VAL + r"\s*$", re.I), "I live in {0}"),
    ("hometown", "high", re.compile(r"\bi(?: am|'m) from " + _VAL + r"\s*$", re.I), "I am from {0}"),
    ("work", "high", re.compile(r"\bi work (at|for|as) " + _VAL + r"\s*$", re.I), "I work {0} {1}"),
    ("favourite {0}", "high", re.compile(r"\bmy fav(?:ou?rite) ([a-z ]{2,30}?) is " + _VAL + r"\s*$", re.I), "my favourite {0} is {1}"),
    ("likes {0}", "ambiguous", re.compile(r"\bi (?:really )?(like|love|hate|prefer|enjoy) " + _VAL + r"\s*$", re.I), "I {0} {1}"),
]
QUESTION_START = re.compile(r"^(what|who|where|when|why|how|do|does|did|is|are|can|could|would|will|should)\b", re.I)


def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", " ".join(text.lower().split()))


def _clauses(text):
    # "My name is Sam and I study B.Tech Electronics" -> two clauses. Dots inside B.Tech survive.
    parts = re.split(r"(?:\.(?=\s|$)|[;!\n]|,\s*|\band\s+(?=(?:i|my)\b))", text.strip(), flags=re.I)
    return [p.strip() for p in parts if p and p.strip()]


def _match(clause):
    for slot, conf, rx, fmt in PATTERNS:
        m = rx.search(clause)
        if m:
            groups = [g.strip() for g in m.groups()]
            return slot.format(*[g.lower() for g in groups]), conf, fmt.format(*groups)
    return None


def slot_of(fact):
    """The 'kind' of a stored fact (name, study, ...) so a new value can replace the old one. None if free-form."""
    for clause in _clauses(fact):
        hit = _match(clause)
        if hit:
            return hit[0]
    return None


def extract(text):
    """Facts a message states about the user: [(fact, slot, confidence)]. Questions and commands yield nothing."""
    t = text.strip()
    if not t or t.endswith("?") or t.startswith("/"):
        return []
    found = []
    for clause in _clauses(t):
        if QUESTION_START.match(clause):
            continue
        hit = _match(clause)
        if hit:
            slot, conf, fact = hit
            found.append((fact, slot, conf))
    return found


def classify(text):
    """'never' (refuse), 'sensitive' (ask first) or 'ok'."""
    if NEVER.search(text):
        return "never"
    if SENSITIVE.search(text):
        return "sensitive"
    return "ok"


def clean(text):
    return " ".join(text.strip().rstrip(".!").split())


def listing(mem):
    return [f"{i}. {f}" for i, f in enumerate(mem["facts"], 1)]


def add_fact(mem, text):
    """Add or update. Returns (status, old_text): status is 'added', 'updated', 'duplicate' or 'empty'."""
    fact = clean(text)
    if not fact:
        return "empty", None
    n = norm(fact)
    for existing in mem["facts"]:
        if norm(existing) == n:
            return "duplicate", existing
    slot = slot_of(fact)
    if slot:
        for i, existing in enumerate(mem["facts"]):
            if slot_of(existing) == slot:
                mem["facts"][i] = fact
                return "updated", existing
    mem["facts"].append(fact)
    return "added", None


def find_update(mem, text):
    """If saving this would replace an older fact, return that older fact (so the caller can ask first)."""
    fact = clean(text)
    slot = slot_of(fact)
    if slot:
        for existing in mem["facts"]:
            if slot_of(existing) == slot and norm(existing) != norm(fact):
                return existing
    return None


def delete_fact(mem, number):
    """Delete fact number `number` (1-based, as shown by /facts). Returns the deleted text; raises IndexError if invalid."""
    if not 1 <= number <= len(mem["facts"]):
        raise IndexError(f"There's no fact number {number}. You have {len(mem['facts'])}.")
    return mem["facts"].pop(number - 1)


def learn(mem, text, mode, confirm, say):
    """Look for facts in a chat message and save them according to `mode` ('auto' | 'ask' | 'off').
    Returns True if anything changed (the caller then saves the memory file)."""
    if mode == "off":
        return False
    changed = False
    for fact, slot, conf in extract(text):
        verdict = classify(fact)
        if verdict == "never":
            say("I won't save that: it looks like a password, ID or card number.")
            continue
        old = find_update(mem, fact)
        needs_ok = mode == "ask" or conf == "ambiguous" or verdict == "sensitive" or old is not None
        if needs_ok:
            q = f"Update '{old}' to '{fact}'?" if old else f"Save '{fact}' to memory?"
            if verdict == "sensitive":
                q += " (this looks personal)"
            if not confirm(q):
                continue
        status, _ = add_fact(mem, fact)
        if status in ("added", "updated"):
            changed = True
            say(f"(saved: {fact}. See /facts)" if status == "added" else f"(updated: {fact})")
    return changed
