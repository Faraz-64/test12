# Jarvis: a friendly local assistant

Chat, search, remember, remind, calculate, read documents and control your PC. It runs on your own computer through
[Ollama](https://ollama.com); the only things that go online are web searches and pages you ask it to read.

```
python jarvis.py            terminal chat
python jarvis.py --gui      desktop window
python jarvis.py --selftest quick built-in checks
```
Type `/help` inside Jarvis for the list of things you can say, and `exit` to close it.

## Install (Windows PowerShell)

```powershell
# 1. Unzip. This creates a folder called "jarvis" inside the destination. Change the paths if your zip is elsewhere.
Expand-Archive "$HOME\Downloads\jarvis-upgraded.zip" -DestinationPath "$HOME\Downloads\model" -Force
cd "$HOME\Downloads\model\jarvis"          # every command below is run from THIS folder (it contains jarvis.py)
dir                                          # you should see jarvis.py, requirements.txt, tests ...

# 2. Python packages (a virtual environment is optional but tidy)
python -m venv .venv
.\.venv\Scripts\Activate.ps1                 # if PowerShell blocks it:  Set-ExecutionPolicy -Scope Process Bypass
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3. Models. Ollama must be up to date: if a pull says "requires a newer version", install the latest from https://ollama.com/download
ollama pull qwen3.5:latest            # the chat model (see "Model size" below if it is slow)
ollama pull nomic-embed-text          # small embedding model: memory search by meaning + smarter document search

# 4. Check it
python -m unittest discover -s tests -v    # automated tests (no Ollama needed). "OK" at the end = good; a skipped OCR test is normal without Tesseract
python test_live.py                        # checks that need real Ollama; add --web for internet checks
python jarvis.py                           # go (or: python jarvis.py --gui)
```
Already have an older Jarvis folder with a `memory.json` or `apps.json`? Copy those two files into the new folder to keep them.

Optional extras:
```powershell
pip install -r requirements-voice.txt                         # voice
Invoke-WebRequest https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -OutFile vosk.zip
Expand-Archive vosk.zip -DestinationPath models               # offline speech recognition model (creates models\vosk-model-small-en-us-0.15)
pip install -r requirements-ocr.txt                           # scanned PDFs ...
winget install UB-Mannheim.TesseractOCR                       # ... plus the Tesseract program (reopen PowerShell afterwards)
```
Jarvis asks for a **memory passphrase** each time. Your facts and chat history are saved in `memory.json`, encrypted with it.
There is no recovery if you forget it; delete `memory.json` to start fresh.

## What it can do

| Feature | Try saying | Notes |
|---|---|---|
| **Facts** | `/remember my name is Sam`, `/facts`, `/forget 2` | Also notices "My name is Sam" or "I study B.Tech Electronics" on its own. Asks before saving anything personal or unclear, never saves passwords/ID/card numbers. A new value for the same thing (a new name) updates the old fact after you confirm. |
| **Calculator** | `what is 23 x 47?`, `20% of 150`, `150 + 10%`, `sqrt(144)`, `/calc 2^10` | Exact (0.1 + 0.2 = 0.3), uses Python's `ast`, never `eval`. Answered by code, not the model. Trig uses radians. |
| **Reminders & timers** | `remind me in 10 minutes to stretch`, `remind me at 5pm to eat`, `set a 25-minute timer`, `list reminders`, `cancel reminder 2`, `done with reminder 2` | Saved to `reminders.json`, so they survive restarts. |
| **Browsers** | `open youtube in brave`, `search lofi on youtube`, `search cats on google in chrome` | Finds Brave, Chrome, Edge, Firefox, Opera. If the one you named isn't installed it offers your default browser. URLs are validated and searches encoded. |
| **Apps & sites** | `open obs`, `open github.com` | Scans your Start Menu and Desktop; add more in `apps.json`. |
| **Documents** | `/ctx "C:\docs\notes.pdf" when is the exam?` | TXT, MD, PDF, DOCX. Only the relevant chunks reach the model, and answers cite `[page 3]` / `[section: Fees]`. Anything the model adds from its own knowledge is labelled "Not from the document:". |
| **Semantic memory** | after `/remember I am doing B.Tech Electronics`: `what do I study?` | Matches by meaning using the embedding model; falls back to keywords if it isn't installed. |
| **PC controls** (Windows) | `set volume to 40`, `volume up`, `mute`, `what's the volume`, `take a screenshot`, `lock my screen` | Volume is read back after setting, screenshots are re-opened to check them, lock asks first. |
| **Voice** | `/voice on`, `/listen`, `/voice speak on` | Offline (Vosk + pyttsx3). Off by default. |
| **Model picks tools** | `could you set something to ring in a quarter hour` | See "How tool use stays safe". |
| **Web & research** | `latest news on AI`, `/web <q>`, `/papers model calibration`, paste a link | Unchanged from before. |

## Settings: `config.json`

Everything is optional; missing keys use defaults, and mistakes are reported all at once at start-up.
`python jarvis.py --write-config` writes a full template.

| Section | Controls |
|---|---|
| `ollama` | server address, chat model, embedding model, timeout, context size, thinking on/off |
| `model_params` | temperatures per mode, max output tokens (0 = model default) |
| `memory` | file name, token budgets, history size, `auto_save_facts` = `auto` / `ask` / `off` |
| `semantic` | on/off, `min_similarity` (0.5), `top_k`, how many past messages to index |
| `reminders` | file, poll interval, beep |
| `browser` | `preferred` (default/brave/chrome/edge/firefox/opera), `fallback_to_default` |
| `voice` | `enabled`, `speak_replies`, Vosk model path, listen seconds, speech rate |
| `documents` | max file MB, max pages, chunk size/overlap, `top_k`, OCR on/off, Tesseract path, allowed extensions |
| `tools` | `model_routing` = `auto` / `always` / `off`, `max_tool_calls`, `chain`, timeout |
| `pc` | screenshot folder, volume step |
| `gui` | `theme` (`dark` / `light`), `font_size` (8-20) |
| `logging` | level, log file (`jarvis.log`; chat text is not logged above DEBUG, passphrases never) |

## How tool use stays safe

The model never runs code. It can only name one of the registered tools (`/tools` lists them) and give arguments. Jarvis then
checks the name against the allowlist, checks every argument against a strict schema (wrong types, extra keys, out-of-range
numbers are refused), asks you first for disruptive actions (locking the screen), runs Jarvis's own function with a timeout, and
prints exactly what that function returned. If anything is wrong it falls back to normal chat. By default only one tool runs per
message and an identical repeated call stops the loop. Exact things (maths, reminders, volume, browser, apps) are matched by
fixed rules *before* the model is consulted, so they never depend on the model's choice. In `auto` mode the model is only asked
when a message contains an action-like word, so ordinary chat isn't slowed down.

## Limits you should know about

- **Reminders only ring while Jarvis is running.** If one comes due while Jarvis is closed or the PC is asleep, it is announced
  as "missed" the next time Jarvis is open or wakes. Timers use the clock, so a timer that expires during sleep fires on waking.
- **Model size.** `qwen3.5:latest` is probably a ~9B model. On a 4-6 GB laptop GPU that spills onto the CPU and is slow. Run
  `ollama ps`; if PROCESSOR isn't `100% GPU`, set `ollama.model` to `qwen3.5:4b` or `qwen3.5:2b`.
- **Semantic search quality** depends on the embedding model. Check `python test_live.py`: it prints the similarity scores so you
  can tune `semantic.min_similarity` (raise it if unrelated memories appear, lower it if relevant ones are missed).
- **Documents.** Scanned PDFs need the Tesseract program installed; OCR output can contain mistakes (Jarvis says when OCR was
  used). At most 40 scanned pages are OCR'd per file. Password-protected PDFs are refused.
- **PC controls are Windows-only.** Lock can't be verified beyond Windows accepting the request.
- **Voice** needs the Vosk model folder and a working microphone; each problem is reported in plain words (`/voice status`).
- **No system-tray icon.** Tkinter can't make one; it would need an extra package.
- **The window** (`--gui`) needs Tkinter, which comes with the python.org installer for Windows. It has a dark theme (a button
  switches to light; `gui.theme` sets the start-up one), blue bubbles for your messages, code in its own box, quick buttons
  (Help, My facts, Reminders, Screenshot, Tools, and Speak when voice is on) and a status light. Keys: **Enter** sends,
  **Shift+Enter** adds a line, **Up/Down** recall your earlier messages, **Ctrl+L** clears the screen. The window's logic is
  tested, but I could not view it on a real screen, so tell me if anything looks off.

## Troubleshooting

| You see | Do this |
|---|---|
| `Ollama is not reachable` | Start Ollama (it runs in the tray), then try again |
| `Model '...' not found` | `ollama pull qwen3.5:latest` (or the model in config.json) |
| `Config problem: ...` | Fix the listed keys in `config.json` (all problems are listed together) |
| `Wrong passphrase or damaged memory.json` | Try again (3 attempts). The file is never overwritten on failure |
| `Memory search is using plain keywords` | `ollama pull nomic-embed-text` |
| `Voice: I can't find the Vosk speech model` | Download it as shown in Install |
| `this PDF looks scanned` / `Tesseract isn't installed` | `winget install UB-Mannheim.TesseractOCR`, or set `documents.tesseract_cmd` |
| `volume control needs pycaw` | `pip install pycaw` |
| `reading .docx files needs python-docx` | `pip install python-docx` |
| `pip install` fails on one package | Install the lines one at a time (command at the top of `requirements.txt`); the rest still work |
| `requires a newer version of Ollama` | Install the latest from https://ollama.com/download, then pull again |
| `requirements.txt` / `tests` / `test_live.py` not found | You're not in the folder that contains `jarvis.py`. Run `dir`, then `cd` into it |
| Everything is slow | See "Model size" above; also keep `ollama.think` false |

## Files

`jarvis.py` (entry point, chat, session) · `config.py` · `calculator.py` · `facts.py` · `reminders.py` · `browsers.py` · `voice.py` ·
`semantic.py` · `documents.py` · `pc.py` · `toolreg.py` · `gui.py` · `config.json` · `requirements*.txt` · `apps.json` (optional) · `tests/` · `test_live.py`
#   t e s t 1 2  
 