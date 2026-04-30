# Anki Audio From SRT

There are two versions in this folder:

- `anki_audio_from_srt.py`
  Uses file picker dialogs.
- `improved_pimsleur_anki.py`
  Uses terminal prompts and is closer to your original script.

Both versions:

- ask for an `.mp3` and `.srt`
- split the source audio into one MP3 per subtitle line
- create an Anki-ready CSV
- write audio cells in the format `[sound:filename.mp3]`
- generate filenames from the front text with a length limit

## Run

Use the bundled Python runtime:

```powershell
& "C:\Users\foohe\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" ".\improved_pimsleur_anki.py"
```

## Notes

- `ffmpeg` must be installed and available on your PATH for `improved_pimsleur_anki.py`.
- The terminal version writes output to `pimsleur_anki_output\`.
- The CSV columns are `Front` and `Audio`.
