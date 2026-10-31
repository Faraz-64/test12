"""Documents for /ctx: read TXT/MD/PDF/DOCX (OCR for scanned PDFs), split into labelled chunks, retrieve the relevant ones.

Instead of cutting a document off after 10,000 characters, the whole file is chunked and only the chunks that matter
for your question go to the model, each labelled with its page or section so answers can cite them.
"""
import io
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import semantic

OCR_MAX_PAGES = 40
DOC_FLOOR = 0.3  # chunks less similar than this to the question are not sent to the model
STOP = set("the a an and or of to in on for with is are was were be been it this that as at by from what which who how when where why do does did i you your my".split())


class DocError(Exception):
    pass


@dataclass
class Chunk:
    label: str
    text: str


def parse_args(rest):
    """'"C:/my docs/a.pdf" what is the deadline' -> (path, question). Quotes allow spaces in the path."""
    rest = rest.strip()
    if rest[:1] in ("'", '"'):
        end = rest.find(rest[0], 1)
        if end == -1:
            raise DocError("the quote around the file name isn't closed")
        return rest[1:end], rest[end + 1:].strip()
    path, _, question = rest.partition(" ")
    return path, question.strip()


def check_file(path, cfg):
    p = Path(path).expanduser()
    if not p.is_file():
        raise DocError(f"I can't find the file '{path}'. If the path has spaces, put it in quotes.")
    allowed = [e.lower() for e in cfg["allowed_extensions"]]
    if p.suffix.lower() not in allowed:
        raise DocError(f"I can read {', '.join(allowed)} files, not '{p.suffix or 'files without an extension'}'.")
    mb = p.stat().st_size / 1_048_576
    if mb > cfg["max_file_mb"]:
        raise DocError(f"that file is {mb:.1f} MB; the limit is {cfg['max_file_mb']} MB (documents.max_file_mb in config.json).")
    if p.stat().st_size == 0:
        raise DocError("that file is empty.")
    return p


def _by_headings(lines, heading_of):
    """Group (text) lines into (label, text) segments using a heading detector; headings with no body are skipped."""
    segs, label, buf = [], None, []
    for line in lines:
        h = heading_of(line)
        if h is not None:
            if "".join(buf).strip():
                segs.append((label or "start", "\n".join(buf)))
            label, buf = f"section: {h}", []
        else:
            buf.append(line)
    if "".join(buf).strip():
        segs.append((label or "start", "\n".join(buf)))
    if len(segs) == 1 and segs[0][0] == "start":
        return _parts(segs[0][1])
    return segs


def _parts(text, size=3000):
    """No headings: label blocks 'part 1', 'part 2', ... so there is still something to cite."""
    paras, blocks, cur = [p for p in re.split(r"\n\s*\n", text) if p.strip()], [], ""
    for p in paras:
        if cur and len(cur) + len(p) > size:
            blocks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + p
    if cur:
        blocks.append(cur)
    return [(f"part {i}", b) for i, b in enumerate(blocks, 1)]


def _read_txt(p):
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    if p.suffix.lower() == ".md":
        return _by_headings(text.splitlines(), lambda l: re.sub(r"^#+\s*", "", l).strip() if re.match(r"^#{1,6}\s+\S", l) else None)
    return _parts(text)


def _read_docx(p):
    try:
        import docx
    except ImportError:
        raise DocError("reading .docx files needs python-docx: pip install python-docx")
    try:
        d = docx.Document(str(p))
    except Exception as e:
        raise DocError(f"I couldn't open that Word file ({e}).")
    items = []
    for para in d.paragraphs:
        style = (para.style.name or "").lower() if para.style is not None else ""
        items.append(("H", para.text.strip()) if style.startswith(("heading", "title")) and para.text.strip() else ("P", para.text))
    for i, table in enumerate(d.tables, 1):
        rows = [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
        items.append(("H", f"table {i}"))
        items.extend(("P", r) for r in rows)
    lines = [("\x00" + t) if kind == "H" else t for kind, t in items]
    return _by_headings(lines, lambda l: l[1:] if l.startswith("\x00") else None)


def _ocr_page(page, cfg):
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        raise DocError("this PDF looks scanned. To read it I need OCR: pip install pytesseract pillow, and install Tesseract "
                       "(Windows: winget install UB-Mannheim.TesseractOCR).")
    if cfg["tesseract_cmd"]:
        pytesseract.pytesseract.tesseract_cmd = cfg["tesseract_cmd"]
    out = []
    for img in page.images:
        try:
            pil = img.image if getattr(img, "image", None) is not None else Image.open(io.BytesIO(img.data))
            out.append(pytesseract.image_to_string(pil))
        except pytesseract.TesseractNotFoundError:
            raise DocError("Tesseract isn't installed or isn't on PATH. Windows: winget install UB-Mannheim.TesseractOCR "
                           "(or set documents.tesseract_cmd in config.json).")
        except Exception:
            continue
    return "\n".join(out)


def _read_pdf(p, cfg, notes):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocError("reading PDFs needs pypdf: pip install pypdf")
    try:
        reader = PdfReader(str(p))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocError("that PDF is password-protected.")
        pages = list(reader.pages)
    except DocError:
        raise
    except Exception as e:
        raise DocError(f"I couldn't open that PDF ({type(e).__name__}).")
    if len(pages) > cfg["max_pages"]:
        notes.append(f"only the first {cfg['max_pages']} of {len(pages)} pages were read (documents.max_pages)")
        pages = pages[: cfg["max_pages"]]
    texts = []
    for pg in pages:
        try:
            texts.append((pg.extract_text() or "").strip())
        except Exception:
            texts.append("")
    blank = [i for i, t in enumerate(texts) if len(t) < 20]
    if blank:
        if not cfg["ocr"]:
            if len(blank) == len(texts):
                raise DocError("this PDF has no selectable text (it looks scanned) and OCR is turned off (documents.ocr in config.json).")
            notes.append(f"{len(blank)} page(s) have no selectable text and were skipped because OCR is off")
        else:
            todo = blank[:OCR_MAX_PAGES]
            for i in todo:
                texts[i] = _ocr_page(pages[i], cfg).strip()
            notes.append(f"OCR was used on {len(todo)} page(s); scanned text can contain mistakes")
            if len(blank) > len(todo):
                notes.append(f"{len(blank) - len(todo)} more scanned page(s) were skipped (OCR limit {OCR_MAX_PAGES})")
    return [(f"page {i}", t) for i, t in enumerate(texts, 1) if t]


def load_segments(path, cfg):
    """Returns ([(label, text)], notes). Raises DocError with a readable reason."""
    p = check_file(path, cfg)
    notes = []
    ext = p.suffix.lower()
    if ext in (".txt", ".md"):
        segs = _read_txt(p)
    elif ext == ".docx":
        segs = _read_docx(p)
    elif ext == ".pdf":
        segs = _read_pdf(p, cfg, notes)
    else:
        raise DocError(f"I don't know how to read '{ext}' files.")
    segs = [(l, t) for l, t in segs if t.strip()]
    if not segs:
        raise DocError("I couldn't find any readable text in that file.")
    return segs, notes


def chunk_segments(segments, size, overlap):
    """Split each segment into chunks of about `size` characters at paragraph/sentence edges, carrying a little overlap."""
    chunks = []
    for label, text in segments:
        text = re.sub(r"[ \t]+", " ", text).strip()
        pieces = []
        for para in re.split(r"\n\s*\n|\n", text):
            para = para.strip()
            while len(para) > size:  # one huge paragraph: cut at a sentence end or space
                cut = max(para.rfind(". ", 0, size), para.rfind(" ", 0, size))
                cut = cut + 1 if cut > size // 2 else size
                pieces.append(para[:cut].strip())
                para = para[cut:].strip()
            if para:
                pieces.append(para)
        cur = ""
        for piece in pieces:
            if cur and len(cur) + len(piece) + 1 > size:
                chunks.append(Chunk(label, cur))
                cur = (cur[-overlap:].split(" ", 1)[-1] + " " if overlap else "") + piece
            else:
                cur = f"{cur}\n{piece}" if cur else piece
        if cur.strip():
            chunks.append(Chunk(label, cur))
    return chunks


def _tokens(s):
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1]


def bm25_rank(chunks, question, k, k1=1.5, b=0.75):
    q = set(_tokens(question))
    docs = [Counter(_tokens(c.text)) for c in chunks]
    n = len(docs)
    avg = (sum(sum(d.values()) for d in docs) / n) or 1
    scores = []
    for i, d in enumerate(docs):
        length, s = sum(d.values()), 0.0
        for term in q:
            f = d.get(term, 0)
            if f:
                df = sum(1 for x in docs if term in x)
                s += math.log(1 + (n - df + 0.5) / (df + 0.5)) * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg))
        scores.append((s, i))
    return [i for s, i in sorted(scores, key=lambda x: (-x[0], x[1]))[:k] if s > 0]


class DocumentStore:
    """Loads and chunks documents once (cached until the file changes) and answers 'which parts matter for this question?'."""

    def __init__(self, cfg, embedder=None):
        self.cfg, self.embedder = cfg, embedder
        self.cache = {}  # path -> (signature, chunks, notes, vectors-or-None)

    def _load(self, path):
        p = check_file(path, self.cfg)
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
        hit = self.cache.get(str(p))
        if hit and hit[0] == sig:
            return hit
        segs, notes = load_segments(p, self.cfg)
        chunks = chunk_segments(segs, self.cfg["chunk_chars"], self.cfg["chunk_overlap"])
        entry = [sig, chunks, notes, None]
        self.cache[str(p)] = entry
        return entry

    def _semantic_rank(self, entry, question, k):
        chunks = entry[1]
        if entry[3] is None:
            vecs = []
            for i in range(0, len(chunks), 32):
                vecs.extend(self.embedder.embed([c.text for c in chunks[i:i + 32]], "doc"))
            entry[3] = semantic.np.vstack(vecs)
        q = self.embedder.embed([question], "query")[0]
        sims = entry[3] @ q
        order = sorted(range(len(chunks)), key=lambda i: -float(sims[i]))
        return [i for i in order if sims[i] >= DOC_FLOOR][:k]

    def relevant(self, path, question):
        """Returns (chunks_in_document_order, notes). Hybrid ranking (meaning + keywords) when embeddings work, else keywords."""
        entry = self._load(path)
        chunks, notes = entry[1], list(entry[2])
        k = self.cfg["top_k"]
        kw = bm25_rank(chunks, question, k * 2)
        picked = kw[:k]
        if self.embedder is not None and self.embedder.available():
            try:
                sem = self._semantic_rank(entry, question, k * 2)
                fused = Counter()
                for ranking in (sem, kw):  # reciprocal rank fusion: a chunk ranked well by either method rises
                    for r, i in enumerate(ranking):
                        fused[i] += 1 / (60 + r)
                picked = [i for i, _ in sorted(fused.items(), key=lambda x: (-x[1], x[0]))][:k]
            except semantic.EmbedError as e:
                notes.append(f"used keyword matching only ({e})")
        elif self.embedder is not None and self.embedder.configured:
            notes.append("used keyword matching only (the embedding model wasn't available)")
        return [chunks[i] for i in sorted(picked)], notes

    def context_for(self, path, question):
        """(context_text, sources, notes) ready to hand to the model. context_text is '' if nothing relevant was found."""
        chosen, notes = self.relevant(path, question)
        if not chosen:
            return "", [], notes
        text = "\n\n".join(f"[{c.label}]\n{c.text}" for c in chosen)
        return text, list(dict.fromkeys(c.label for c in chosen)), notes
