import os
import re
import html
import csv
import random
import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# -------------------------
# INPUT
# -------------------------
lesson_url = input("Paste lesson page URL: ").strip()
LANGUAGE = input("Enter language, e.g. Korean, Japanese: ").strip()
LEVEL = input("Enter level/tag, optional: ").strip()

language_safe = re.sub(r"[^a-zA-Z0-9]+", "_", LANGUAGE).strip("_").lower()
level_safe = re.sub(r"[^a-zA-Z0-9]+", "_", LEVEL).strip("_").lower()

OUTPUT_DIR = f"{language_safe}_lesson_audio"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# -------------------------
# HELPERS
# -------------------------
def clean(text):
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def safe_filename(text):
    text = clean(text)
    text = re.sub(
        r"[^\w가-힣ぁ-んァ-ン一-龯а-яА-ЯёЁà-ỹÀ-Ỹ]+",
        "_",
        text,
        flags=re.UNICODE
    )
    return text.strip("_")[:80] or "item"


TYPE_COL = 0
FRONT_COL = 1
BACK_COL = 2
CHOICES_COL = 3
AUDIO_COL = 4
LEVEL_TAG_COL = 5


def normalize_quotes(text):
    text = str(text)
    return (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("â€œ", '"')
        .replace("â€", '"')
        .replace("â€˜", "'")
        .replace("â€™", "'")
    )


def clean_speaker_labels(text):
    text = re.sub(r"(^|\s)[A-D]:\s*", " ", str(text))
    return re.sub(r"\s+", " ", text).strip()


def extract_quoted_english(text):
    return " / ".join(
        match.strip()
        for match in re.findall(r'"([^"]+)"', normalize_quotes(text))
        if match.strip()
    )


def remove_quoted_text(text):
    text = re.sub(r'"[^"]+"', " ", normalize_quotes(text))
    return re.sub(r"\s+", " ", text).strip()


def split_into_chunks(text, count=4):
    text = clean(text)
    words = text.split()

    if len(words) >= count:
        base_size, remainder = divmod(len(words), count)
        chunks = []
        start = 0
        for index in range(count):
            size = base_size + (1 if index < remainder else 0)
            chunks.append(" ".join(words[start:start + size]))
            start += size
    else:
        chars = list(re.sub(r"\s+", "", text))
        if len(chars) >= count:
            base_size, remainder = divmod(len(chars), count)
            chunks = []
            start = 0
            for index in range(count):
                size = base_size + (1 if index < remainder else 0)
                chunks.append("".join(chars[start:start + size]))
                start += size
        else:
            chunks = chars[:]

    while len(chunks) < count:
        chunks.append("")

    return chunks[:count]


def make_choices(text):
    chunks = split_into_chunks(text, 4)
    random.shuffle(chunks)
    return " ".join(f"{chr(65 + i)}) {chunk}".rstrip() for i, chunk in enumerate(chunks)).strip()


def looks_like_audio(text):
    value = str(text).strip().lower()
    return value.startswith("[sound:") or value.endswith((".mp3", ".m4a", ".wav", ".ogg"))


def has_existing_choices_column(row):
    return len(row) > CHOICES_COL and str(row[CHOICES_COL]).strip().lower() in {"d", "choices", "choice"}


def extract_level_tag(type_value):
    text = str(type_value).strip()
    language_match = re.match(r"\s*([^-]+?)\s*-\s*", text)
    level_match = re.search(r"\bL\s*([0-9]+)\b", text, flags=re.IGNORECASE)

    language = ""
    if language_match:
        language = re.sub(r"[^a-z0-9]+", "", language_match.group(1).lower())

    if not language:
        language = re.sub(r"[^a-z0-9]+", "", LANGUAGE.lower())

    level_number = ""
    if level_match:
        level_number = level_match.group(1)
    else:
        typed_level_match = re.search(r"([0-9]+)", LEVEL, flags=re.IGNORECASE)
        if typed_level_match:
            level_number = typed_level_match.group(1)

    if not language or not level_number:
        return ""

    return f"{language}pod101level{level_number}"


def is_header_row(row):
    normalized = [str(cell).strip().lower() for cell in row[:6]]
    return (
        len(normalized) >= 4
        and normalized[0] == "type"
        and normalized[1] == "front"
        and normalized[2] == "back"
        and "audio" in normalized
    )


def ensure_output_columns(row, header=False):
    row = ["" if value is None else str(value) for value in row]

    while len(row) < 4:
        row.append("")

    if len(row) == 4 and not has_existing_choices_column(row):
        row.insert(CHOICES_COL, "Choices" if header else "")

    while len(row) <= LEVEL_TAG_COL:
        row.append("")

    if looks_like_audio(row[BACK_COL]) and not looks_like_audio(row[AUDIO_COL]):
        row[AUDIO_COL] = row[BACK_COL]
        row[BACK_COL] = ""

    if looks_like_audio(row[CHOICES_COL]) and not looks_like_audio(row[AUDIO_COL]):
        row[AUDIO_COL] = row[CHOICES_COL]
        row[CHOICES_COL] = ""

    if header:
        row[TYPE_COL] = row[TYPE_COL] or "Type"
        row[FRONT_COL] = "Front"
        row[BACK_COL] = "Back"
        row[CHOICES_COL] = row[CHOICES_COL] or "Choices"
        row[AUDIO_COL] = "Audio"
        row[LEVEL_TAG_COL] = row[LEVEL_TAG_COL] or "LevelTag"

    return row


def clean_csv_row(row):
    row = ensure_output_columns(row)
    front = clean(row[FRONT_COL])
    back = clean(row[BACK_COL])
    choices = clean(row[CHOICES_COL])

    if front:
        front = clean_speaker_labels(normalize_quotes(front))
        extracted_english = extract_quoted_english(front)
        cleaned_front = remove_quoted_text(front) or front
        row[FRONT_COL] = cleaned_front

        if extracted_english and not back:
            row[BACK_COL] = extracted_english
        elif back:
            row[BACK_COL] = normalize_quotes(back).strip()

        if not choices:
            row[CHOICES_COL] = make_choices(cleaned_front)

    if not clean(row[LEVEL_TAG_COL]):
        row[LEVEL_TAG_COL] = extract_level_tag(row[TYPE_COL])

    return row


def finalize_csv(csv_path):
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    processed = []
    for index, row in enumerate(rows):
        if not any(str(cell).strip() for cell in row):
            processed.append(row)
        elif index == 0 and is_header_row(row):
            processed.append(ensure_output_columns(row, header=True))
        else:
            processed.append(clean_csv_row(row))

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL)
        writer.writerows(processed)

def is_audio(url):
    return (
        url
        and "learningcenter/audio" in url
        and url.lower().endswith((".mp3", ".m4a"))
    )

def get_audio_url(tag):
    return (
        tag.get("data-src")
        or tag.get("data-audio")
        or tag.get("data-url")
        or tag.get("src")
    )

def get_title(soup):
    title_tag = soup.find("title")
    if title_tag:
        title = clean(title_tag.get_text())
        title = re.sub(r"\s*-\s*.*101.*$", "", title)
        return title
    return "lesson"

def nearest_text(tag):
    for parent in tag.parents:
        text = clean(parent.get_text(" ", strip=True))
        if text and len(text) < 300:
            return text
    return ""

def download(context, url, path):
    try:
        r = context.request.get(url)

        if r.status != 200:
            print("❌ Failed:", url)
            return False

        with open(path, "wb") as f:
            f.write(r.body())

        print("✅", os.path.basename(path))
        return True

    except Exception as e:
        print("❌ Error:", e)
        return False

# -------------------------
# MAIN
# -------------------------
rows = []
seen = set()

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir="pod101_profile",
        headless=False
    )

    page = context.new_page()
    page.goto(lesson_url, wait_until="networkidle", timeout=90000)

    print("\n👉 Log in if needed, then press ENTER")
    input()

    page.wait_for_timeout(4000)

    soup = BeautifulSoup(page.content(), "html.parser")

    title = get_title(soup)
    slug = safe_filename(title).lower()

    print("\nLesson:", title)

    lesson_meta = f"{LANGUAGE} - {LEVEL} - {title}" if LEVEL else f"{LANGUAGE} - {title}"

    # -------------------------
    # 1. DIALOGUE LINES
    # -------------------------
    dialogue_texts = soup.select(
        ".lsn3-lesson-dialogue__td--text, "
        ".lesson-dialogue__text, "
        ".dialogue-text, "
        "[class*='dialogue'][class*='text']"
    )

    dialogue_buttons = soup.select(
        "[data-src*='transcript'], "
        "[data-src*='dialogue'], "
        ".js-lsn3-play-dialogue"
    )

    for i, btn in enumerate(dialogue_buttons, 1):
        url = get_audio_url(btn)

        if not is_audio(url) or url in seen:
            continue

        seen.add(url)

        text = ""

        if i - 1 < len(dialogue_texts):
            text = clean(dialogue_texts[i - 1].get_text(" ", strip=True))

        if not text:
            text = clean(btn.get("data-text")) or nearest_text(btn)

        filename = f"{language_safe}_{slug}_dialogue_line_{i:02d}.mp3"
        path = os.path.join(OUTPUT_DIR, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Dialogue Line)",
                "Front": clean(text),
                "Back": "",
                "Audio": f"[sound:{filename}]"
            })

    # -------------------------
    # 2. VOCAB WORDS
    # -------------------------
    vocab_count = 0

    for row in soup.select("tr"):
        word_tag = row.select_one(
            ".lsn3-lesson-vocabulary__lang, "
            ".lesson-vocabulary__lang, "
            "[class*='vocabulary__lang'], "
            "[class*='term'], "
            "[class*='word']"
        )

        meaning_tag = row.select_one(
            ".lsn3-lesson-vocabulary__definition, "
            ".lesson-vocabulary__definition, "
            "[class*='definition'], "
            "[class*='meaning']"
        )

        audio_tag = row.select_one(
            ".js-lsn3-play-vocabulary[data-src], "
            "[data-src*='vocabulary'], "
            "[data-src*='vocab'], "
            "[data-src]"
        )

        if not word_tag or not audio_tag:
            continue

        url = get_audio_url(audio_tag)

        if not is_audio(url) or url in seen:
            continue

        seen.add(url)

        word = clean(word_tag.get_text(" ", strip=True))
        meaning = clean(meaning_tag.get_text(" ", strip=True)) if meaning_tag else ""

        if not word:
            continue

        vocab_count += 1

        filename = f"{language_safe}_{slug}_vocab_word_{vocab_count:02d}_{safe_filename(word)}.mp3"
        path = os.path.join(OUTPUT_DIR, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Vocab Word)",
                "Front": word,
                "Back": meaning,
                "Audio": f"[sound:{filename}]"
            })

    # -------------------------
    # 3. VOCAB SENTENCES / EXAMPLES
    # -------------------------
    sentence_count = 0

    example_blocks = soup.select(
        ".lsn3-lesson-vocabulary__sample, "
        ".lesson-vocabulary__sample, "
        "[class*='sample'], "
        "[class*='example']"
    )

    for block in example_blocks:
        audio_tag = block.select_one(
            ".js-lsn3-play-vocabulary[data-src], "
            "[data-src]"
        )

        if not audio_tag:
            continue

        url = get_audio_url(audio_tag)

        if not is_audio(url) or url in seen:
            continue

        seen.add(url)

        text = (
            clean(audio_tag.get("data-text"))
            or clean(block.get_text(" ", strip=True))
            or nearest_text(audio_tag)
        )

        if not text:
            continue

        sentence_count += 1

        filename = f"{language_safe}_{slug}_vocab_sentence_{sentence_count:02d}.mp3"
        path = os.path.join(OUTPUT_DIR, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Vocab Sentence)",
                "Front": text,
                "Back": "",
                "Audio": f"[sound:{filename}]"
            })

    context.close()

# -------------------------
# EXPORT CSV
# -------------------------
csv_path = f"{language_safe}_{slug}_anki.csv"

df = pd.DataFrame(rows, columns=["Type", "Front", "Back", "Audio"])
df.to_csv(csv_path, index=False, encoding="utf-8-sig")
finalize_csv(csv_path)

print("\nDONE")
print("CSV:", csv_path)
print("Audio folder:", OUTPUT_DIR)
print("Total cards:", len(rows))
