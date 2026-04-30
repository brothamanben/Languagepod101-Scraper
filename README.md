# LanguagePod101 Scraper Scripts

This folder contains several Python scripts for downloading and organizing LanguagePod101 / Innovative Language lesson content for Anki.

## Files

### cleanup.py

Cleans exported CSV files before importing them into Anki.

Typical uses:

- Removes messy text such as `nan`
- Fixes broken audio fields
- Cleans extra whitespace
- Helps prepare final CSV rows for Anki import

Use this after downloading or merging lesson CSV files.

---

### langpodlevel.py

Downloads lessons from one full level or lesson-library page.

Typical use:

- You paste a level URL, such as a KoreanClass101, ThaiPod101, or SpanishPod101 lesson library page
- The script finds lessons in that level
- It downloads dialogue audio, vocabulary audio, and example sentence audio when available
- It creates lesson folders
- It creates CSV files for Anki

Use this when you want one whole level.

---

### langpodlevel_lessonaudio.py

Downloads full lesson audio from a level.

This is different from dialogue/vocab audio.

Typical use:

- Downloads the main lesson MP3s
- Organizes them by lesson
- Useful if you want listening files, not just Anki card audio

Use this when you want the full lesson audio files.

---

### langpodmulti.py

Downloads multiple levels in one run.

Typical use:

- You enter several level URLs
- You give each one a language name and level label
- When finished, you type `done`
- The script downloads each level into separate folders
- It can create a master CSV combining all downloaded lessons

Use this for batch downloading many levels across one or more languages.

---

### langpodsingle.py

Downloads one individual lesson.

Typical use:

- You paste a single lesson URL
- The script downloads that lesson’s dialogue, vocabulary, example sentences, and audio
- It creates one lesson folder and one CSV

Use this when you only need one lesson instead of a full level.

---

### pod101.py

Main/general scraper script.

This is likely the base or older version of the scraper.

Typical use:

- Handles LanguagePod101 lesson pages
- Extracts lesson text and audio URLs
- Downloads MP3 files
- Creates Anki-ready CSV rows

Use this as the core script or reference file if the other scripts were built from it.

---

## Recommended Workflow

1. Use `langpodsingle.py` for testing one lesson.
2. Use `langpodlevel.py` once single-lesson downloading works.
3. Use `langpodmulti.py` when downloading many levels.
4. Use `langpodlevel_lessonaudio.py` only if you want full lesson MP3s.
5. Use `cleanup.py` at the end to clean CSV files before Anki import.

---

## Anki CSV Output

The scripts are meant to produce rows like:

```csv
Type,Front,Back,Audio
Korean - L3 - 001 - Lesson Name,안녕하세요,Hello,[sound:korean_l3_001_audio.mp3]
