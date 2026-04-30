import csv
import re
import shutil
import subprocess
from pathlib import Path


DEFAULT_OUTPUT_DIR = "pimsleur_anki_output"
DEFAULT_FILENAME_LIMIT = 48
DEFAULT_PADDING_SECONDS = 0.15


def prompt_path(prompt_text: str, expected_suffix: str) -> Path:
    while True:
        raw_value = input(prompt_text).strip().strip('"')
        path = Path(raw_value)

        if not raw_value:
            print("Please enter a file path.")
            continue

        if path.suffix.lower() != expected_suffix:
            print(f"Please choose a {expected_suffix} file.")
            continue

        if not path.exists():
            print("That file does not exist.")
            continue

        return path


def prompt_filename_limit() -> int:
    raw_value = input(
        f"Max filename length [{DEFAULT_FILENAME_LIMIT}]: "
    ).strip()
    if not raw_value:
        return DEFAULT_FILENAME_LIMIT

    try:
        limit = int(raw_value)
    except ValueError:
        print(f"Invalid number. Using default: {DEFAULT_FILENAME_LIMIT}")
        return DEFAULT_FILENAME_LIMIT

    return max(12, limit)


def clean_text(text: str) -> str:
    text = re.sub(r"<.*?>", "", text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def srt_time_to_seconds(value: str) -> float:
    hours, minutes, seconds_ms = value.strip().split(":")
    seconds, millis = seconds_ms.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000
    )


def parse_srt_blocks(srt_path: Path) -> list[dict]:
    content = srt_path.read_text(encoding="utf-8-sig").strip()
    blocks = re.split(r"\n\s*\n", content)
    rows: list[dict] = []

    for index, block in enumerate(blocks, start=1):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue

        time_line = lines[1]
        if "-->" not in time_line:
            continue

        start_text, end_text = [part.strip() for part in time_line.split("-->")]
        text = clean_text(" ".join(lines[2:]))
        if not text:
            continue

        rows.append(
            {
                "index": index,
                "text": text,
                "start_seconds": srt_time_to_seconds(start_text),
                "end_seconds": srt_time_to_seconds(end_text),
            }
        )

    return rows


def slugify(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "clip"


def make_clip_name(
    text: str,
    clip_index: int,
    max_length: int,
    used_names: set[str],
) -> str:
    prefix = f"{clip_index:04d}_"
    base_limit = max(8, max_length - len(prefix))
    base_name = slugify(text)[:base_limit].strip("-") or "clip"
    candidate = f"{prefix}{base_name}"
    duplicate_number = 2

    while f"{candidate}.mp3" in used_names:
        suffix = f"-{duplicate_number}"
        trimmed = base_name[: max(1, base_limit - len(suffix))].strip("-") or "clip"
        candidate = f"{prefix}{trimmed}{suffix}"
        duplicate_number += 1

    final_name = f"{candidate}.mp3"
    used_names.add(final_name)
    return final_name


def export_clip(
    ffmpeg_path: str,
    input_mp3: Path,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
) -> None:
    padded_start = max(0.0, start_seconds - DEFAULT_PADDING_SECONDS)
    padded_end = max(padded_start, end_seconds + DEFAULT_PADDING_SECONDS)
    duration = max(0.05, padded_end - padded_start)

    cmd = [
        ffmpeg_path,
        "-y",
        "-ss",
        f"{padded_start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(input_mp3),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def main() -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise SystemExit("ffmpeg was not found on PATH. Install it, then run this script again.")

    input_mp3 = prompt_path("Path to MP3 file: ", ".mp3")
    srt_file = prompt_path("Path to SRT file: ", ".srt")
    filename_limit = prompt_filename_limit()

    output_dir = Path(DEFAULT_OUTPUT_DIR)
    clips_dir = output_dir / "clips"
    csv_file = output_dir / "anki_import.csv"
    clips_dir.mkdir(parents=True, exist_ok=True)

    subtitle_rows = parse_srt_blocks(srt_file)
    if not subtitle_rows:
        raise SystemExit("No valid subtitle lines were found in the SRT file.")

    used_names: set[str] = set()
    csv_rows: list[list[str]] = []

    for row in subtitle_rows:
        clip_name = make_clip_name(
            text=row["text"],
            clip_index=row["index"],
            max_length=filename_limit,
            used_names=used_names,
        )
        clip_path = clips_dir / clip_name

        try:
            export_clip(
                ffmpeg_path=ffmpeg_path,
                input_mp3=input_mp3,
                output_path=clip_path,
                start_seconds=row["start_seconds"],
                end_seconds=row["end_seconds"],
            )
        except subprocess.CalledProcessError as exc:
            error_text = exc.stderr.strip() if exc.stderr else str(exc)
            print(f"Failed on subtitle {row['index']}: {row['text']}")
            print(error_text)
            continue

        csv_rows.append([row["text"], f"[sound:{clip_name}]"])

    with csv_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Front", "Audio"])
        writer.writerows(csv_rows)

    print("Done.")
    print(f"CSV created: {csv_file}")
    print(f"Audio clips folder: {clips_dir}")
    print(f"Cards created: {len(csv_rows)}")


if __name__ == "__main__":
    main()
