import io, json, os, sys, unittest, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import semantic as S

# A tiny stand-in for an embedding model with a known "meaning space": words from the same topic share a dimension.
TOPICS = [{"study", "studying", "doing", "b.tech", "btech", "electronics", "engineering", "degree", "college", "course"},
          {"pizza", "food", "eat", "lunch", "dinner", "hungry", "cheese"},
          {"gpu", "laptop", "rtx", "3050", "graphics", "computer"},
          {"name", "called", "sam", "alex"}]


def fake_vector(text):
    words = [w.strip("?.,!:'\"").lower() for w in text.split()]
    v = [float(sum(w in topic for w in words)) for topic in TOPICS]
    v.append(0.05)                       # a little shared "noise" so unrelated texts aren't exactly orthogonal
    return v


class FakeOllama:
    def __init__(self): self.calls, self.fail = [], None
    def __call__(self, req, timeout=0):
        if self.fail:
            raise self.fail
        texts = json.loads(req.data)["input"]
        self.calls.append(texts)
        body = json.dumps({"embeddings": [fake_vector(t.split(": ", 1)[-1]) for t in texts]}).encode()
        return io.BytesIO(body) if False else _Resp(body)


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def make(fake=None, clock=None, **kw):
    fake = fake or FakeOllama()
    emb = S.Embedder("http://x", "toy-model", query_prefix="search_query: ", doc_prefix="search_document: ", opener=fake, clock=clock or (lambda: 0.0))
    return S.SemanticMemory(emb, **kw), fake


FACTS = [("I am doing B.Tech Electronics", "fact"), ("my favourite food is cheese pizza", "fact"),
         ("my laptop has an RTX 3050 GPU", "fact"), ("my name is Sam", "fact")]


class SemanticTests(unittest.TestCase):
    def test_what_do_i_study_finds_the_btech_fact(self):
        sem, _ = make(min_similarity=0.5)
        hits = sem.search({}, "What do I study?", FACTS)
        self.assertEqual(hits[0][1], "I am doing B.Tech Electronics")      # no shared keyword except "study"-topic meaning
        self.assertEqual(len(hits), 1)                                     # unrelated memories are NOT returned

    def test_threshold_and_top_k(self):
        sem, _ = make(min_similarity=0.0, top_k=2)
        self.assertEqual(len(sem.search({}, "What do I study?", FACTS)), 2)
        sem2, _ = make(min_similarity=0.99)
        self.assertEqual(sem2.search({}, "weather forecast tomorrow", FACTS), [])   # nothing relevant -> nothing returned

    def test_other_questions(self):
        sem, _ = make()
        self.assertEqual(sem.search({}, "what is my name?", FACTS)[0][1], "my name is Sam")
        self.assertEqual(sem.search({}, "which gpu does my laptop have", FACTS)[0][1], "my laptop has an RTX 3050 GPU")
        self.assertEqual(sem.search({}, "what should I eat for lunch", FACTS)[0][1], "my favourite food is cheese pizza")

    def test_vectors_are_cached_in_memory_dict_and_reused(self):
        sem, fake = make()
        mem = {}
        sem.search(mem, "What do I study?", FACTS)
        self.assertEqual(len(mem["emb"]), 4)
        n = len(fake.calls)
        sem.search(mem, "what is my name?", FACTS)                          # facts are not re-embedded: only the new query
        self.assertEqual(len(fake.calls), n + 1)
        fresh, fake2 = make()                                              # a new session loads vectors from the saved dict
        fresh.search(json.loads(json.dumps(mem)), "What do I study?", FACTS)
        self.assertEqual(len(fake2.calls), 1)                               # just the query

    def test_prompt_prefixes_are_applied(self):
        sem, fake = make()
        sem.search({}, "hello name", [("my name is Sam", None)])
        sent = [t for call in fake.calls for t in call]
        self.assertIn("search_document: my name is Sam", sent); self.assertIn("search_query: hello name", sent)

    def test_changing_the_model_invalidates_cached_vectors(self):
        a, _ = make(); b = S.SemanticMemory(S.Embedder("http://x", "other-model"))
        self.assertNotEqual(a.key("same text"), b.key("same text"))

    def test_encode_decode_roundtrip(self):
        v = np.random.default_rng(1).normal(size=768).astype(np.float32); v /= np.linalg.norm(v)
        back = S.decode(S.encode(v))
        self.assertGreater(float(v @ back), 0.9999)
        self.assertLess(len(S.encode(v)), 2200)                              # ~2KB per memory: small enough for the encrypted file

    def test_failures_raise_and_back_off(self):
        now = [0.0]
        sem, fake = make(clock=lambda: now[0])
        fake.fail = urllib.error.URLError("refused")
        with self.assertRaises(S.EmbedError):
            sem.search({}, "q", FACTS)
        self.assertFalse(sem.enabled)
        fake.fail = None; calls = len(fake.calls)
        with self.assertRaises(S.EmbedError):                                # during back-off: fails instantly, no network call
            sem.search({}, "q", FACTS)
        self.assertEqual(len(fake.calls), calls)
        now[0] = 61
        self.assertTrue(sem.enabled)
        self.assertEqual(sem.search({}, "What do I study?", FACTS)[0][1], "I am doing B.Tech Electronics")   # recovers by itself

    def test_missing_model_gives_a_helpful_message(self):
        sem, fake = make()
        fake.fail = urllib.error.HTTPError("http://x", 404, "not found", {}, None)
        with self.assertRaises(S.EmbedError) as cm:
            sem.search({}, "q", FACTS)
        self.assertIn("ollama pull toy-model", str(cm.exception))

    def test_bad_responses(self):
        for payload in (b'{"embeddings": []}', b"not json", b'{"wrong": 1}', b'{"embeddings": [[]]}'):
            sem, _ = make(fake=lambda req, timeout=0, p=payload: _Resp(p))     # each case gets its own stand-in server
            with self.assertRaises(S.EmbedError, msg=payload):
                sem.search({}, "q", FACTS)

    def test_prune_removes_deleted_memories(self):
        sem, _ = make()
        mem = {}
        sem.search(mem, "x name", FACTS)
        self.assertEqual(sem.prune(mem, [t for t, _ in FACTS[:2]]), 2)
        self.assertEqual(len(mem["emb"]), 2)

    def test_empty_inputs(self):
        sem, fake = make()
        self.assertEqual(sem.search({}, "anything", []), [])
        self.assertEqual(sem.search({}, "   ", FACTS), [])
        self.assertEqual(fake.calls, [])

    def test_disabled_when_no_model(self):
        sem = S.SemanticMemory(S.Embedder("http://x", ""))
        self.assertFalse(sem.enabled)


if __name__ == "__main__":
    unittest.main()
