#!/usr/bin/env python3
"""Checks that need a REAL Ollama (and optionally internet). Everything else is covered by: python -m unittest discover -s tests

    python test_live.py          # model, embeddings, chat, code, JSON, documents, memory, tool choice
    python test_live.py --web    # also web search, page reading and arXiv (needs internet)
"""
import contextlib, io, json, sys, tempfile, time, urllib.request
from pathlib import Path

import config, jarvis, semantic, toolreg

HERE = Path(__file__).resolve().parent
cfg, warnings = config.load_config(HERE / "config.json", HERE)
jarvis.apply_config(cfg)
fails = []


def quiet(fn, *args, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


def check(name, fn):
    t = time.time()
    try:
        ok, note = fn()
    except Exception as e:
        ok, note = False, f"{type(e).__name__}: {e}"
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({time.time() - t:.1f}s)" + (f"   {note}" if note else ""))
    if not ok:
        fails.append(name)


def installed_models():
    with urllib.request.urlopen(jarvis.BASE_URL + "/api/tags", timeout=5) as r:
        return [m["name"] for m in json.load(r)["models"]]


def chat_model():
    names = installed_models()
    ok = jarvis.MODEL in names
    return ok, "" if ok else f"{jarvis.MODEL} not found. Installed: {', '.join(names) or 'none'}. Run: ollama pull {jarvis.MODEL}"


def embedding_model():
    names = installed_models(); m = cfg["ollama"]["embedding_model"]
    ok = any(n == m or n.startswith(m + ":") for n in names)
    return ok, "" if ok else f"{m} not found. Run: ollama pull {m}  (without it, memory search uses plain keywords)"


def embeddings_rank_by_meaning():
    o = cfg["ollama"]
    sem = semantic.SemanticMemory(semantic.Embedder(o["base_url"], o["embedding_model"], query_prefix=o["embedding_query_prefix"], doc_prefix=o["embedding_document_prefix"]),
                                  min_similarity=0.0, top_k=10)
    cands = [("I am doing B.Tech Electronics", "a"), ("my favourite food is cheese pizza", "b"), ("my laptop has an RTX 3050 GPU", "c"), ("my name is Sam", "d")]
    hits = sem.search({}, "What do I study?", cands)
    scores = ", ".join(f"{s:.2f} {t[:22]}" for s, t, _ in hits)
    first_ok = hits[0][1] == "I am doing B.Tech Electronics"
    above = [s for s, t, _ in hits if t != "I am doing B.Tech Electronics" and s >= cfg["semantic"]["min_similarity"]]
    return first_ok and not above, f"min_similarity={cfg['semantic']['min_similarity']}; scores: {scores}"


def chat():
    out = quiet(jarvis.ask, 1, "Say hi in one short sentence.")
    return bool(out) and "<think>" not in out, repr(out[:80])


def code_only():
    out = jarvis.only_code(quiet(jarvis.ask, 2, "Write a Python function that adds two numbers."))
    return out.startswith("```") and out.endswith("```"), repr(out[:60])


def json_mode():
    out = jarvis.tidy(jarvis.ask(4, "urgently restart the web server on port 8080"))
    d = json.loads(out)
    return set(d) == set(jarvis.SCHEMA) and d["urgent"] is True, out[:100]


def document_answer():
    ctx = "[page 2]\nThe warranty lasts 24 months from the date of purchase."
    out = quiet(jarvis.ask, 8, f"<context>{ctx}</context> How long is the warranty?")
    miss = quiet(jarvis.ask, 8, f"<context>{ctx}</context> Who is the manufacturer?")
    return "24" in out and "[page 2]" in out and "couldn't find" in miss.lower(), f"{out[:60]!r} | {miss[:60]!r}"


def memory_fact():
    mem = {"facts": ["my laptop has an RTX 3050"], "history": [], "emb": {}}
    out = quiet(jarvis.ask, 1, "What GPU does my laptop have?", mem)
    return "3050" in out, repr(out[:80])


def tool_choice():
    with tempfile.TemporaryDirectory() as d:
        c = config.defaults(); c["memory"]["file"] = str(Path(d) / "m.json"); c["reminders"]["file"] = str(Path(d) / "r.json"); c["logging"]["file"] = ""
        s = jarvis.Session(c, {"facts": [], "history": [], "emb": {}}, "pw", apps={})
        picked = toolreg.choose(s.registry, "please start a countdown for three minutes called pasta", jarvis.ask_json)
        valid = picked is not None and s.registry.validate(*picked) is not None
        refuses = toolreg.choose(s.registry, "tell me a joke about cats", jarvis.ask_json) is None
        s.close()
        return valid and refuses, f"picked {picked}; joke -> {'no tool' if refuses else 'WRONGLY picked a tool'}"


def web_search():
    ctx, results = jarvis.web_search("python programming language")
    return bool(ctx) and len(results) > 0, f"{len(results)} results"


def read_page():
    ctx, sources = jarvis.read_urls(["https://example.com"])
    return "Example Domain" in ctx, f"{len(sources)} page(s) read"


def papers():
    ctx, links = jarvis.find_papers("model calibration")
    return len(links) > 0, f"{len(links)} papers"


def main():
    print(f"Testing {jarvis.MODEL} at {jarvis.URL}\n")
    for w in warnings:
        print(f"(config: {w})")
    check("Ollama reachable, chat model installed", chat_model)
    if fails:
        sys.exit("Fix that first, then run the tests again.")
    if cfg["ollama"]["embedding_model"]:
        check("Embedding model installed", embedding_model)
        if "Embedding model installed" not in fails:
            check("Embeddings rank 'What do I study?' correctly", embeddings_rank_by_meaning)
    for name, fn in [("Chat reply (no <think> leak)", chat), ("Code mode returns only a code block", code_only), ("JSON mode returns the schema", json_mode),
                     ("Document answer cites its page, and admits when it's missing", document_answer), ("Uses a saved fact", memory_fact),
                     ("Model picks a valid tool, and none for a joke", tool_choice)]:
        check(name, fn)
    if "--web" in sys.argv:
        for name, fn in [("Web search", web_search), ("Read a web page", read_page), ("arXiv paper search", papers)]:
            check(name, fn)
    ps = jarvis.run(["ollama", "ps"])
    print("\nGPU check (PROCESSOR should say 100% GPU):\n" + (ps or "  could not run `ollama ps`"))
    print(f"\n{'All passed' if not fails else str(len(fails)) + ' failed: ' + ', '.join(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
