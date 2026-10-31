"""Central configuration: defaults, loading, validation. Nothing else in Jarvis hardcodes these settings."""
import copy
import json
from pathlib import Path


class ConfigError(Exception):
    """Raised with a readable list of everything wrong in config.json."""


def S(default, typ, **rules):
    return {"default": default, "type": typ, **rules}


SCHEMA = {
    "ollama": {
        "base_url": S("http://localhost:11434", str, prefixes=("http://", "https://")),
        "model": S("qwen3.5:latest", str, nonempty=True),
        "embedding_model": S("nomic-embed-text", str),  # empty string turns semantic memory/retrieval embeddings off
        "embedding_query_prefix": S("search_query: ", str),
        "embedding_document_prefix": S("search_document: ", str),
        "timeout_seconds": S(300, (int, float), min=5, max=3600),
        "num_ctx": S(6144, int, min=512, max=131072),
        "keep_alive": S("30m", str),
        "think": S(False, bool),
    },
    "model_params": {
        "temperature_chat": S(0.6, (int, float), min=0, max=2),
        "temperature_code": S(0.2, (int, float), min=0, max=2),
        "temperature_grounded": S(0.1, (int, float), min=0, max=2),
        "temperature_json": S(0.0, (int, float), min=0, max=2),
        "max_output_tokens": S(0, int, min=0, max=32768),  # 0 = the model's own default
    },
    "memory": {
        "file": S("memory.json", str, nonempty=True),  # encrypted with your passphrase
        "fact_budget_tokens": S(400, int, min=50, max=4000),
        "history_budget_tokens": S(1200, int, min=0, max=8000),
        "recall_budget_tokens": S(500, int, min=0, max=4000),
        "max_history_messages": S(1000, int, min=10, max=100000),
        "auto_save_facts": S("auto", str, choices=("auto", "ask", "off")),
    },
    "semantic": {
        "enabled": S(True, bool),
        "min_similarity": S(0.5, (int, float), min=0, max=1),
        "top_k": S(5, int, min=1, max=20),
        "max_indexed_messages": S(300, int, min=0, max=5000),
    },
    "reminders": {
        "file": S("reminders.json", str, nonempty=True),
        "poll_seconds": S(1.0, (int, float), min=0.1, max=60),
        "beep": S(True, bool),
    },
    "browser": {
        "preferred": S("default", str, choices=("default", "brave", "chrome", "edge", "firefox", "opera")),
        "fallback_to_default": S(True, bool),
    },
    "voice": {
        "enabled": S(False, bool),
        "speak_replies": S(False, bool),
        "vosk_model_path": S("models/vosk-model-small-en-us-0.15", str, nonempty=True),
        "listen_seconds": S(8, (int, float), min=1, max=60),
        "tts_rate": S(175, int, min=80, max=400),
    },
    "documents": {
        "max_file_mb": S(25, (int, float), min=0.1, max=500),
        "max_pages": S(300, int, min=1, max=5000),
        "chunk_chars": S(900, int, min=200, max=4000),
        "chunk_overlap": S(120, int, min=0, max=1000),
        "top_k": S(4, int, min=1, max=12),
        "ocr": S(True, bool),
        "tesseract_cmd": S("", str),  # e.g. C:\\Program Files\\Tesseract-OCR\\tesseract.exe if it is not on PATH
        "allowed_extensions": S([".txt", ".md", ".pdf", ".docx"], list),
    },
    "tools": {
        "model_routing": S("auto", str, choices=("auto", "always", "off")),
        "max_tool_calls": S(1, int, min=1, max=5),
        "chain": S(False, bool),
        "timeout_seconds": S(60, (int, float), min=1, max=600),
    },
    "pc": {
        "screenshot_dir": S("screenshots", str, nonempty=True),
        "volume_step": S(10, int, min=1, max=50),
    },
    "gui": {
        "theme": S("dark", str, choices=("dark", "light")),
        "font_size": S(11, int, min=8, max=20),
    },
    "logging": {
        "level": S("INFO", str, choices=("DEBUG", "INFO", "WARNING", "ERROR")),
        "file": S("jarvis.log", str),  # empty string = no log file
    },
}

PATH_KEYS = [("memory", "file"), ("reminders", "file"), ("pc", "screenshot_dir"),
             ("logging", "file"), ("voice", "vosk_model_path")]


def defaults():
    return {sec: {k: copy.deepcopy(r["default"]) for k, r in rules.items()} for sec, rules in SCHEMA.items()}


def _check(section, key, value, rule, problems):
    where = f"{section}.{key}"
    typ = rule["type"]
    if isinstance(value, bool) and typ is not bool:
        problems.append(f"{where}: expected {_tname(typ)}, got true/false")
        return
    if not isinstance(value, typ):
        problems.append(f"{where}: expected {_tname(typ)}, got {type(value).__name__} ({value!r})")
        return
    if "choices" in rule and value not in rule["choices"]:
        problems.append(f"{where}: must be one of {', '.join(map(str, rule['choices']))} (got {value!r})")
    if rule.get("nonempty") and not str(value).strip():
        problems.append(f"{where}: must not be empty")
    if "prefixes" in rule and not value.startswith(rule["prefixes"]):
        problems.append(f"{where}: must start with {' or '.join(rule['prefixes'])} (got {value!r})")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "min" in rule and value < rule["min"]:
            problems.append(f"{where}: must be at least {rule['min']} (got {value})")
        if "max" in rule and value > rule["max"]:
            problems.append(f"{where}: must be at most {rule['max']} (got {value})")
    if typ is list and not all(isinstance(x, str) and x.startswith(".") for x in value):
        problems.append(f"{where}: must be a list of extensions like \".pdf\"")


def _tname(t):
    return "number" if t == (int, float) else getattr(t, "__name__", str(t))


def validate(user):
    """Merge user settings over the defaults. Returns (config, warnings). Raises ConfigError listing every problem."""
    cfg, problems, warnings = defaults(), [], []
    if not isinstance(user, dict):
        raise ConfigError("config.json must contain a JSON object like {\"ollama\": {...}}")
    for sec, values in user.items():
        if sec not in SCHEMA:
            warnings.append(f"unknown section '{sec}' ignored")
            continue
        if not isinstance(values, dict):
            problems.append(f"{sec}: expected an object")
            continue
        for key, value in values.items():
            if key not in SCHEMA[sec]:
                warnings.append(f"unknown setting '{sec}.{key}' ignored")
                continue
            _check(sec, key, value, SCHEMA[sec][key], problems)
            cfg[sec][key] = value
    if cfg["documents"]["chunk_overlap"] >= cfg["documents"]["chunk_chars"]:
        problems.append("documents.chunk_overlap: must be smaller than documents.chunk_chars")
    if problems:
        raise ConfigError("config.json has problems:\n  - " + "\n  - ".join(problems))
    return cfg, warnings


def resolve_paths(cfg, base):
    """Make relative file settings relative to the Jarvis folder (not the current directory)."""
    base = Path(base)
    for sec, key in PATH_KEYS:
        v = cfg[sec][key]
        if v:
            p = Path(v).expanduser()
            cfg[sec][key] = str(p if p.is_absolute() else base / p)
    return cfg


def load_config(path, base=None):
    """Read config.json (missing file = defaults). Returns (config, warnings)."""
    path = Path(path)
    base = base or path.parent
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return resolve_paths(defaults(), base), []
    except OSError as e:
        raise ConfigError(f"could not read {path}: {e}")
    try:
        user = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"{path.name} is not valid JSON ({e})")
    cfg, warnings = validate(user)
    return resolve_paths(cfg, base), warnings


def write_template(path):
    """Write a config.json containing every default, so there is something to edit."""
    Path(path).write_text(json.dumps(defaults(), indent=2), encoding="utf-8")
