"""Semantic memory: embeddings from a small local Ollama model, searched by cosine similarity (NumPy).

Vectors are cached inside the memory dict (mem["emb"]), which Jarvis saves in the ENCRYPTED memory file, so nothing about
your memories sits on disk in readable form. If embeddings aren't available, callers fall back to keyword matching.
"""
import base64
import hashlib
import json
import time
import urllib.error
import urllib.request

try:
    import numpy as np
except ImportError:  # optional dependency: Jarvis still works, just without semantic search
    np = None


class EmbedError(Exception):
    pass


BATCH = 32
BACKOFF_SECONDS = 60  # after a failure, don't retry for a minute, so chat isn't delayed by a dead embedding service


class Embedder:
    def __init__(self, base_url, model, timeout=30, query_prefix="", doc_prefix="", opener=urllib.request.urlopen, clock=time.time):
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        self.query_prefix, self.doc_prefix, self.opener, self.clock = query_prefix, doc_prefix, opener, clock
        self.disabled_until, self.last_error = 0.0, None

    @property
    def configured(self):
        return bool(self.model) and np is not None

    def available(self):
        return self.configured and self.clock() >= self.disabled_until

    def embed(self, texts, kind="doc"):
        """List of unit-length float32 vectors, one per text. Raises EmbedError (and backs off) if it can't."""
        if np is None:
            raise EmbedError("semantic search needs NumPy: pip install numpy")
        if not self.model:
            raise EmbedError("no embedding model is configured")
        if self.clock() < self.disabled_until:
            raise EmbedError(self.last_error or "embeddings are temporarily off")
        prefix = self.query_prefix if kind == "query" else self.doc_prefix
        body = json.dumps({"model": self.model, "input": [prefix + t for t in texts]}).encode()
        req = urllib.request.Request(self.base_url + "/api/embed", body, {"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as r:
                data = json.load(r)
            vecs = data["embeddings"]
            if len(vecs) != len(texts) or not all(vecs):
                raise ValueError("wrong number of embeddings")
            arr = np.asarray(vecs, dtype=np.float32)
        except urllib.error.HTTPError as e:
            self._fail(f"the embedding model '{self.model}' isn't installed. Run: ollama pull {self.model}" if e.code == 404
                       else f"Ollama returned HTTP {e.code} for embeddings")
        except (urllib.error.URLError, OSError):
            self._fail("I couldn't reach Ollama for embeddings")
        except (ValueError, KeyError, TypeError) as e:
            self._fail(f"Ollama sent back something unexpected for embeddings ({e})")
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return list(arr / norms)

    def _fail(self, message):
        self.last_error = message
        self.disabled_until = self.clock() + BACKOFF_SECONDS
        raise EmbedError(message)


def encode(vec):
    return base64.b64encode(vec.astype(np.float16).tobytes()).decode("ascii")


def decode(s):
    v = np.frombuffer(base64.b64decode(s), dtype=np.float16).astype(np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


class SemanticMemory:
    def __init__(self, embedder, min_similarity=0.5, top_k=5, max_indexed=300):
        self.embedder, self.min_similarity, self.top_k, self.max_indexed = embedder, min_similarity, top_k, max_indexed
        self.cache = {}  # key -> decoded vector, so we don't decode on every search

    @property
    def enabled(self):
        return self.embedder.available()

    def key(self, text):
        e = self.embedder
        return hashlib.sha1(f"{e.model}|{e.doc_prefix}|{text}".encode("utf-8")).hexdigest()[:20]  # a new model = new keys = re-embed

    def _vectors(self, mem, texts):
        store = mem.setdefault("emb", {})
        missing = []
        for t in dict.fromkeys(texts):
            k = self.key(t)
            if k in self.cache:
                continue
            if k in store:
                self.cache[k] = decode(store[k])
            else:
                missing.append(t)
        for i in range(0, len(missing), BATCH):
            batch = missing[i:i + BATCH]
            for t, v in zip(batch, self.embedder.embed(batch, "doc")):
                self.cache[self.key(t)] = v
                store[self.key(t)] = encode(v)
        return {t: self.cache[self.key(t)] for t in texts}

    def search(self, mem, query, candidates, top_k=None, min_similarity=None):
        """candidates: [(text, meta)]. Returns [(score, text, meta)] best first, only those above the similarity threshold.
        Raises EmbedError if embeddings aren't available (the caller falls back to keyword search)."""
        if not candidates or not query.strip():
            return []
        vecs = self._vectors(mem, [t for t, _ in candidates])
        q = self.embedder.embed([query], "query")[0]
        floor = self.min_similarity if min_similarity is None else min_similarity
        scored = [(float(vecs[t] @ q), t, meta) for t, meta in candidates]
        hits = sorted((s for s in scored if s[0] >= floor), key=lambda s: -s[0])
        return hits[: top_k or self.top_k]

    def prune(self, mem, keep_texts):
        """Drop cached vectors for text that no longer exists (deleted facts, trimmed history)."""
        store = mem.get("emb")
        if not store:
            return 0
        keep = {self.key(t) for t in keep_texts}
        stale = [k for k in store if k not in keep]
        for k in stale:
            del store[k]
            self.cache.pop(k, None)
        return len(stale)
