import io, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, documents as D, semantic as S
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_semantic import make as make_sem, TOPICS
from pdfmaker import make_pdf

import logging
logging.getLogger("pypdf").addHandler(logging.NullHandler())  # the damaged-PDF test makes pypdf complain; that's expected
CFG = config.defaults()["documents"]


def _ocr_ready():
    """OCR needs the optional pytesseract package AND the Tesseract program. Without them the OCR test is skipped, not failed."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def cfg(**kw):
    c = dict(CFG); c.update(kw); return c


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.d = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def pdf(self, name, pages):
        p = self.d / name; p.write_bytes(make_pdf(pages)); return p

    def docx(self, name):
        import docx
        d = docx.Document()
        d.add_heading("Introduction", 1); d.add_paragraph("This handbook explains the hostel rules for first-year students.")
        d.add_heading("Fees", 1); d.add_paragraph("The annual fee is 85,000 rupees, payable in two instalments.")
        t = d.add_table(rows=2, cols=2); t.cell(0, 0).text = "Item"; t.cell(0, 1).text = "Amount"; t.cell(1, 0).text = "Laundry"; t.cell(1, 1).text = "1,200"
        p = self.d / name; d.save(str(p)); return p


class ReadTests(Base):
    def test_txt_and_markdown(self):
        t = self.d / "a.txt"; t.write_text("First para.\n\nSecond para about batteries.", encoding="utf-8")
        segs, _ = D.load_segments(t, CFG)
        self.assertEqual(segs[0][0], "part 1"); self.assertIn("batteries", segs[0][1])
        m = self.d / "b.md"; m.write_text("# Setup\nInstall it.\n## Usage\nRun the tool daily.\n", encoding="utf-8")
        self.assertEqual([l for l, _ in D.load_segments(m, CFG)[0]], ["section: Setup", "section: Usage"])

    def test_docx_headings_and_tables(self):
        segs, _ = D.load_segments(self.docx("h.docx"), CFG)
        labels = [l for l, _ in segs]
        self.assertEqual(labels, ["section: Introduction", "section: Fees", "section: table 1"])
        self.assertIn("85,000", dict(segs)["section: Fees"]); self.assertIn("Laundry | 1,200", dict(segs)["section: table 1"])

    def test_pdf_pages_are_labelled(self):
        p = self.pdf("p.pdf", ["Chapter one is about motors.", "Page two covers the warranty: 24 months.", "Page three: contact support."])
        segs, notes = D.load_segments(p, CFG)
        self.assertEqual([l for l, _ in segs], ["page 1", "page 2", "page 3"]); self.assertEqual(notes, [])
        self.assertIn("24 months", segs[1][1])

    def scanned_pdf(self):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (1200, 400), "white")
        ImageDraw.Draw(img).text((40, 120), "The invoice total is 4500 rupees", fill="black", font=ImageFont.load_default(size=56))
        scanned = self.d / "scan.pdf"; img.save(str(scanned), "PDF"); return scanned

    @unittest.skipUnless(_ocr_ready(), "OCR extras not installed (pytesseract + the Tesseract program): this is optional")
    def test_scanned_pdf_uses_ocr(self):
        segs, notes = D.load_segments(self.scanned_pdf(), CFG)
        self.assertIn("invoice", segs[0][1].lower()); self.assertTrue(any("OCR was used" in n for n in notes))

    def test_scanned_pdf_with_ocr_off_says_so(self):
        with self.assertRaises(D.DocError) as cm:                                      # OCR switched off: say so, don't guess
            D.load_segments(self.scanned_pdf(), cfg(ocr=False))
        self.assertIn("OCR is turned off", str(cm.exception))

    def test_scanned_pdf_without_ocr_packages_gives_install_help(self):
        from unittest import mock
        with mock.patch.dict(sys.modules, {"pytesseract": None}):                      # as if pytesseract isn't installed
            with self.assertRaises(D.DocError) as cm: D.load_segments(self.scanned_pdf(), CFG)
        self.assertIn("pip install pytesseract", str(cm.exception)); self.assertIn("Tesseract", str(cm.exception))

    def test_limits_and_errors(self):
        with self.assertRaises(D.DocError) as cm: D.load_segments(self.d / "missing.txt", CFG)
        self.assertIn("can't find", str(cm.exception))
        exe = self.d / "x.exe"; exe.write_bytes(b"MZ")
        with self.assertRaises(D.DocError) as cm: D.load_segments(exe, CFG)
        self.assertIn("not '.exe'", str(cm.exception))
        big = self.d / "big.txt"; big.write_text("x" * 3_000_000)
        with self.assertRaises(D.DocError) as cm: D.load_segments(big, cfg(max_file_mb=1))
        self.assertIn("limit is 1 MB", str(cm.exception))
        empty = self.d / "e.txt"; empty.write_text("")
        with self.assertRaises(D.DocError): D.load_segments(empty, CFG)
        blank = self.d / "blank.txt"; blank.write_text("   \n\n  ")
        with self.assertRaises(D.DocError): D.load_segments(blank, CFG)
        bad = self.d / "bad.pdf"; bad.write_bytes(b"not a pdf at all")
        with self.assertRaises(D.DocError) as cm: D.load_segments(bad, CFG)
        self.assertIn("couldn't open that PDF", str(cm.exception))
        with self.assertRaises(D.DocError): D.load_segments(self.d, CFG)             # a folder is not a file
        many = self.pdf("many.pdf", [f"page number {i} text here" for i in range(5)])
        segs, notes = D.load_segments(many, cfg(max_pages=2))
        self.assertEqual(len(segs), 2); self.assertIn("only the first 2 of 5 pages", notes[0])

    def test_parse_args(self):
        self.assertEqual(D.parse_args('"C:/my docs/a.pdf" what is the deadline?'), ("C:/my docs/a.pdf", "what is the deadline?"))
        self.assertEqual(D.parse_args("notes.txt summarize it"), ("notes.txt", "summarize it"))
        self.assertEqual(D.parse_args("notes.txt"), ("notes.txt", ""))
        with self.assertRaises(D.DocError): D.parse_args('"unclosed.txt what')


class ChunkTests(unittest.TestCase):
    def test_chunk_sizes_labels_and_overlap(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
        chunks = D.chunk_segments([("page 4", text)], 900, 120)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(c.label == "page 4" for c in chunks))
        self.assertTrue(all(len(c.text) <= 900 + 125 for c in chunks))
        self.assertTrue(all(c.text.strip() for c in chunks))

    def test_one_giant_paragraph_is_split(self):
        chunks = D.chunk_segments([("part 1", "This is a sentence. " * 400)], 500, 50)
        self.assertGreater(len(chunks), 10); self.assertTrue(all(len(c.text) <= 560 for c in chunks))

    def test_nothing_is_lost(self):
        words = [f"w{i}" for i in range(2000)]
        got = " ".join(c.text for c in D.chunk_segments([("p", " ".join(words))], 400, 0)).split()
        self.assertEqual(got, words)


class RetrievalTests(Base):
    def long_doc(self):
        paras = [f"Section {i}: routine notes about the department, number {i}, nothing important here." * 2 for i in range(300)]
        paras.append("The final exam is on 14 December in Hall B, and calculators are not allowed.")   # far beyond 10,000 characters
        p = self.d / "long.txt"; p.write_text("\n\n".join(paras), encoding="utf-8"); return p

    def test_answer_beyond_the_old_10000_character_limit_is_found(self):
        p = self.long_doc()
        self.assertGreater(p.stat().st_size, 40_000)
        store = D.DocumentStore(CFG)
        ctx, sources, _ = store.context_for(p, "When is the final exam and where?")
        self.assertIn("14 December in Hall B", ctx)
        self.assertLess(len(ctx), 4 * 1100 + 200)                                      # only a few chunks go to the model, not the file

    def test_pdf_retrieval_cites_the_page(self):
        p = self.pdf("w.pdf", ["Motor overview and torque tables.", "The warranty lasts 24 months from purchase.", "Contact support by email."])
        ctx, sources, _ = D.DocumentStore(CFG).context_for(p, "How long is the warranty?")
        self.assertEqual(sources[0], "page 2"); self.assertIn("[page 2]", ctx)

    def test_nothing_relevant_returns_empty(self):
        p = self.d / "a.txt"; p.write_text("Bananas are yellow and grow in bunches.")
        self.assertEqual(D.DocumentStore(CFG).context_for(p, "quantum chromodynamics lagrangian")[0], "")

    def test_cache_and_reload_when_file_changes(self):
        p = self.d / "c.txt"; p.write_text("The password policy requires twelve characters.")
        store = D.DocumentStore(CFG)
        self.assertIn("twelve", store.context_for(p, "password policy")[0])
        first = store.cache[str(p)]
        store.context_for(p, "characters")
        self.assertIs(store.cache[str(p)], first)                                      # second question reuses the loaded document
        p.write_text("The password policy now requires sixteen characters, updated.")
        self.assertIn("sixteen", store.context_for(p, "password policy")[0])           # the edit is noticed

    def test_semantic_ranking_finds_paraphrases_and_hybrid_still_matches_keywords(self):
        p = self.d / "s.txt"
        p.write_text("\n\n".join(["Lunch menu: cheese pizza and food every day." * 3, "The student must complete the degree course in engineering." * 3,
                                  "GPU and laptop requirements for the lab computer." * 3]), encoding="utf-8")
        sem, fake = make_sem()
        store = D.DocumentStore(cfg(top_k=1, chunk_chars=300), sem.embedder)
        ctx, src, notes = store.context_for(p, "What does the college course require?")   # shares only meaning-words with the text
        self.assertIn("degree course", ctx); self.assertEqual(notes, [])
        n = len(fake.calls)
        store.context_for(p, "which gpu is needed")
        self.assertEqual(len(fake.calls), n + 1)                                       # chunk vectors were cached: only the question was embedded

    def test_falls_back_to_keywords_when_embeddings_fail(self):
        p = self.d / "f.txt"; p.write_text("The warranty lasts 24 months.\n\nUnrelated text about sailing.")
        sem, fake = make_sem(); fake.fail = OSError("down")
        ctx, src, notes = D.DocumentStore(CFG, sem.embedder).context_for(p, "warranty months")
        self.assertIn("24 months", ctx); self.assertTrue(any("keyword matching only" in n for n in notes))


class Bm25Tests(unittest.TestCase):
    def test_ranks_the_matching_chunk_first(self):
        chunks = [D.Chunk("a", "cats and dogs"), D.Chunk("b", "invoice number 4821 is overdue"), D.Chunk("c", "weather today")]
        self.assertEqual(D.bm25_rank(chunks, "overdue invoice 4821", 2), [1])
        self.assertEqual(D.bm25_rank(chunks, "zzz", 2), [])


if __name__ == "__main__":
    unittest.main()
