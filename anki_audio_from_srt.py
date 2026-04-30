import csv
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tkinter import Tk, filedialog, messagebox, simpledialog


DEFAULT_MAX_FILENAME_LENGTH = 48
DEFAULT_PADDING_MS = 150


@dataclass
class SubtitleEntry:
    index: int
    start_seconds: float
    end_seconds: float
    text: str


def parse_srt_timestamp(value: str) -> float:
    hours, minutes, seconds_millis = value.split(":")
    seconds, millis = seconds_millis.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000
    )


def parse_srt(srt_path: Path) -> list[SubtitleEntry]:
    content = srt_path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\r?\n+", content.strip())
    entries: list[SubtitleEntry] = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue

        timing_line = lines[1]
        match = re.match(
            r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2},\d{3})",
            timing_line,
        )
        if not match:
            continue

        text = " ".join(lines[2:]).strip()
        if not text:
            continue

        entries.append(
            SubtitleEntry(
                index=len(entries) + 1,
                start_seconds=parse_srt_timestamp(match.group("start")),
                end_seconds=parse_srt_timestamp(match.group("end")),
                text=text,
            )
        )

    if not entries:
        raise ValueError("No valid subtitle entries were found in the SRT file.")

    return entries


def slugify_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized)
    return slug.strip("-") or "card"


def build_filename(
    text: str,
    max_length: int,
    used_names: set[str],
    index: int,
) -> str:
    base_slug = slugify_text(text)
    prefix = f"{index:03d}-"
    available_length = max(8, max_length - len(prefix))
    candidate_base = base_slug[:available_length].strip("-") or "card"
    candidate = f"{prefix}{candidate_base}"
    suffix = 2

    while candidate in used_names:
        suffix_text = f"-{suffix}"
        trimmed_base = candidate_base[: max(1, available_length - len(suffix_text))].strip("-")
        candidate = f"{prefix}{trimmed_base or 'card'}{suffix_text}"
        suffix += 1

    used_names.add(candidate)
    return f"{candidate}.mp3"


def export_clip(
    ffmpeg_executable: str,
    source_mp3: Path,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
) -> None:
    command = [
        ffmpeg_executable,
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(source_mp3),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def resolve_ffmpeg(root: Tk) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    messagebox.showinfo(
        "Locate ffmpeg",
        "ffmpeg was not found on PATH. Please choose ffmpeg.exe so audio clips can be created.",
        parent=root,
    )
    selected = filedialog.askopenfilename(
        parent=root,
        title="Select ffmpeg.exe",
        filetypes=[("ffmpeg executable", "ffmpeg.exe"), ("Executables", "*.exe")],
    )
    return selected or None


def choose_file(root: Tk, title: str, filetypes: list[tuple[str, str]]) -> Path | None:
    selected = filedialog.askopenfilename(parent=root, title=title, filetypes=filetypes)
    return Path(selected) if selected else None


def choose_output_directory(root: Tk, default_dir: Path) -> Path | None:
    selected = filedialog.askdirectory(
        parent=root,
        title="Choose an output folder",
        initialdir=str(default_dir),
    )
    return Path(selected) if selected else None


def main() -> int:
    root = Tk()
    root.withdraw()

    mp3_path = choose_file(root, "Choose the source MP3", [("MP3 files", "*.mp3")])
    if not mp3_path:
        return 1

    srt_path = choose_file(root, "Choose the subtitle SRT", [("SRT files", "*.srt")])
    if not srt_path:
        return 1

    output_dir = choose_output_directory(root, mp3_path.parent)
    if not output_dir:
        return 1

    max_filename_length = simpledialog.askinteger(
        "Filename length",
        "Maximum number of characters for generated MP3 filenames:",
        parent=root,
        initialvalue=DEFAULT_MAX_FILENAME_LENGTH,
        minvalue=12,
        maxvalue=120,
    )
    if max_filename_length is None:
        return 1

    ffmpeg_executable = resolve_ffmpeg(root)
    if not ffmpeg_executable:
        messagebox.showerror("Missing ffmpeg", "No ffmpeg executable was selected.", parent=root)
        return 1

    try:
        subtitles = parse_srt(srt_path)
    except Exception as exc:
        messagebox.showerror("SRT parse error", str(exc), parent=root)
        return 1

    audio_dir = output_dir / "anki_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "anki_cards.csv"
    used_names: set[str] = set()
    rows: list[list[str]] = []

    for entry in subtitles:
        start = max(0.0, entry.start_seconds - DEFAULT_PADDING_MS / 1000)
        end = max(start, entry.end_seconds + DEFAULT_PADDING_MS / 1000)
        duration = max(0.05, end - start)
        filename = build_filename(entry.text, max_filename_length, used_names, entry.index)
        output_path = audio_dir / filename

        try:
            export_clip(ffmpeg_executable, mp3_path, output_path, start, duration)
        except subprocess.CalledProcessError as exc:
            error_text = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            messagebox.showerror(
                "ffmpeg error",
                f"Failed to export clip for subtitle {entry.index}.\n\n{error_text}",
                parent=root,
            )
            return 1

        rows.append([entry.text, f"[sound:{filename}]"])

    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["Front", "Audio"])
        writer.writerows(rows)

    messagebox.showinfo(
        "Finished",
        f"Created {len(rows)} cards.\n\nCSV: {csv_path}\nAudio folder: {audio_dir}",
        parent=root,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
