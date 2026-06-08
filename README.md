# book-translate

Translate a whole book (PDF → translated PDF) using AI. Reads a scanned or
digital PDF, transcribes each page, groups the text into stories/chapters,
translates each one with full context, then assembles a styled English (or
any-target-language) HTML and PDF.

Works for any language pair (German → English, French → English, Russian →
Spanish, …) by editing one config file per book.

---

## What you need before you start (macOS one-time setup)

You'll do this once, ever. Open the **Terminal** app (⌘-Space → "Terminal").
Every line in a grey box below should be copy-pasted into Terminal and the
**Return** key pressed.

### 1. Install Homebrew (a package manager)

Skip if you already have it. Paste this whole thing as one line:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

When it finishes, it usually tells you to run two extra lines to "add brew to
your PATH". Copy and run whatever it says. (If you skip this, the `brew`
command won't be found later.)

### 2. Install Python 3, Poppler (for PDF pages), and Google Chrome

```bash
brew install python poppler
brew install --cask google-chrome
```

Poppler provides `pdftoppm` (turns PDF pages into images) and `pdfinfo`.
Chrome is used at the end to render the final book PDF.

### 3. Get one or more API keys

You need a key from at least one of these providers:

- **Google Gemini** — cheapest for transcribing pages. Get a free key at
  https://aistudio.google.com/apikey.
- **Anthropic Claude** — best literary translator. Get a key at
  https://console.anthropic.com (you'll need to add a small amount of credit).
- **OpenAI** — also supported. https://platform.openai.com.

The default setup uses Gemini for transcribing the pages and Claude for the
translation, so the typical setup is to grab both keys.

### 4. Download this project

If a `book-translate` folder already lives somewhere on your computer, skip
this step. Otherwise:

```bash
cd ~/Documents
git clone <THE GITHUB URL FOR THIS REPO> book-translate
```

Then **always start each session by entering the project folder**:

```bash
cd ~/Documents/book-translate
```

(Adjust the path if you put it somewhere else, e.g. `cd ~/Downloads/book-translate`.)

### 5. One-time Python setup inside the project

From inside the `book-translate` folder (see step 4):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The first line creates a private Python workspace called `.venv`. The second
line "activates" it. The third installs all the libraries the scripts need.

**Every time you open a new Terminal** to use this project, you'll need to
re-activate it:

```bash
cd ~/Documents/book-translate
source .venv/bin/activate
```

You can tell it worked if your prompt now starts with `(.venv)`.

### 6. Save your API key(s)

Make a copy of the example file and edit it:

```bash
cp .env.example .env
open -e .env
```

That last command opens the file in TextEdit. Paste your key(s) after the `=`
signs. You only need to fill in the providers you actually use. Save and close.

The file should look like:

```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
OPENAI_API_KEY=
```

---

## Easy mode — the GUI

If you'd rather not type commands, there's a Mac/Windows/Linux desktop GUI
that wraps the whole pipeline. Launch it from the project folder:

```bash
python run_gui.py
```

You'll see a workflow board with one card per stage (render → transcribe →
segment → translate → assemble). The GUI:

- Lets you **start a new book** (pick the PDF, fill in metadata) or **open an
  existing book.yaml** to resume.
- Shows what's done and what isn't — each card is *Not started*, *Partial*,
  *Needs review*, *Complete*, or *Stale*. Re-running picks up where you left off.
- Has a **"Review titles"** step on the segmentation card: see every section
  the AI thinks it found, uncheck false-positive titles (they get merged into
  the previous section), edit titles inline. This step saves significant cost
  before the expensive translation stage.
- Stores API keys in the **system keychain** (macOS Keychain / Windows
  Credential Manager / Linux Secret Service) — no plain-text `.env` needed.
  If you already have a `.env`, the GUI offers to import it on first launch.
- Shows **live token usage** as the pipeline runs, with a per-stage breakdown.
- Has an **"Edit metadata"** button that opens a tabbed editor for the whole
  `book.yaml` (Book, Input, Languages, Prompts, Providers, Output, Assemble,
  Validate, plus a Raw YAML escape hatch). Comments in the file are preserved
  across edits.

The CLI workflow below continues to work unchanged; pick whichever feels
easier.

---

## Translating a book — the normal workflow

### Step 1: Put the PDF somewhere you can find it

Put the source PDF in your Downloads folder, or anywhere else. Note the full
path; you'll point the script at it next.

### Step 2: Generate a starter config file

```bash
python src/init_book.py --pdf ~/Downloads/your-book.pdf
```

This samples a few pages of the PDF, asks an AI to figure out the title,
author, language, era, and roughly where the body text starts and ends, and
writes a file called **`book.yaml`** into your current folder.

Optional flags:
- `--target-lang Spanish` — translate into Spanish instead of English (default).
- `--out my-book.yaml` — write to a different filename.

### Step 3: Open `book.yaml` and review it

```bash
open -e book.yaml
```

Look especially at:
- **`input.first_page` / `input.last_page`** — the page range the AI thinks is
  body text. If it's wrong (the AI sometimes includes the table of contents or
  cuts off too early), fix the numbers by hand.
- **`book.about_html`** — empty by default. Paste any HTML you want to appear
  on the "About this book" page of the final PDF.
- **`prompts.transcription_notes`** — if the AI didn't notice that the book is
  in Fraktur, or has unusual orthography, add a note here.

Save and close.

### Step 4: Run the whole pipeline

```bash
python src/run.py --config book.yaml
```

It will:
1. Render each PDF page to a PNG image (`pages/page_XXXX.png`).
2. Transcribe each page into segmented source text (`out/transcript/`).
3. Stitch segments into whole stories (`out/stories.json`).
4. Translate each story (`out/stories/`).
5. Assemble side-by-side HTML, book-styled HTML, and a final PDF (`out/`).

It also **validates the output after transcribing and after translating** and
automatically retries any pages or stories that look broken (empty
transcripts, suspiciously short translations, output that still contains
source-language characters, etc.). A summary of anything that couldn't be
auto-fixed is written to `out/validation_report.json`.

This will take a while — depending on the book size, anywhere from a few
minutes to a few hours. You can stop it (`Ctrl-C`) and restart it any time;
it picks up where it left off.

### Step 5: Find your outputs

Everything lands in the `out/` folder:

- `<book_name>.pdf` — the final translated book.
- `<book_name>.html` — the same as a web page.
- `<book_name>_review.html` and `<book_name>_review.md` — bilingual
  side-by-side document for spot-checking the translation.
- `validation_report.json` — list of anything the validator wasn't happy
  about. Empty file (or no `issues`) = clean run.

---

## Re-running just one stage

If you want to redo just one part (say, after editing `book.yaml` to change
the translation style and you want fresh translations only):

```bash
python src/run.py --config book.yaml --stage translate --force
```

Stages: `render`, `transcribe`, `segment`, `translate`, `assemble`, `all`
(default), `validate` (just runs the checker without doing any work).

`--force` re-does work even if outputs already exist. Without `--force`, each
stage skips items that are already done — so you can safely restart after a
crash.

---

## Troubleshooting

**"command not found: brew"** — Homebrew isn't in your PATH yet. Run the
two-line commands Homebrew printed at the end of step 1 of the setup.

**"command not found: python3"** — Python didn't install or isn't in your
PATH. Try `brew install python` again, then close and reopen Terminal.

**"PDF not found: …"** — the path in `book.yaml` (`input.pdf`) is wrong.
Open `book.yaml` and check that the path matches where the PDF actually is.
Use `~/Downloads/foo.pdf` style (the `~` means your home folder).

**"Chrome not found at …"** — you don't have Google Chrome installed, or it's
installed somewhere unusual. Either install it (`brew install --cask
google-chrome`) or edit `assemble.chrome_path` in `book.yaml` to point at
your Chromium/Chrome binary.

**"GEMINI_API_KEY" / "ANTHROPIC_API_KEY" missing** — your `.env` doesn't have
a key for the provider this stage uses. Either add the key, or change
`providers.transcribe.provider` / `providers.translate.provider` in
`book.yaml` to one whose key you do have.

**Some pages weren't transcribed well** — check `out/validation_report.json`.
You can manually delete a bad page's JSON in `out/transcript/` and re-run
`python src/run.py --config book.yaml --stage transcribe`, which will only
re-do the missing one.

**The translation has weird leftover German/French/etc. in it** — the model
got lazy. Delete the affected story's JSON in `out/stories/` and re-run with
`--stage translate`.

---

## Files in this project

```
book.example.yaml       fully annotated template — read this for help
book.yaml               your per-book config (you create this, either via
                        init_book.py or by copying book.example.yaml); gitignored
.env                    your API keys (you fill this in once)
LICENSE                 MIT license

run_gui.py              launch the desktop GUI (PySide6)
src/gui/                desktop GUI package
src/init_book.py        bootstrap a book.yaml from a source PDF
src/run.py              run the full pipeline
src/render.py           PDF → page PNGs
src/transcribe.py       page PNGs → segmented source text JSON
src/segment.py          stitch segments into whole stories
src/translate_stories.py translate each story
src/assemble.py         stories → HTML / PDF
src/validate.py         check outputs, flag failures
src/config.py           shared config loader
src/providers.py        Anthropic / Google / OpenAI backends

pages/                  generated: rendered PDF page images
out/transcript/         generated: per-page transcripts
out/stories.json        generated: stitched whole stories
out/stories/            generated: translated stories
out/<book_name>.{html,pdf}  generated: final outputs
out/validation_report.json  generated: post-run validator findings
```

---

## License

MIT — see [LICENSE](LICENSE).
