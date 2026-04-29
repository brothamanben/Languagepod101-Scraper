import builtins
import hashlib
import html
import multiprocessing
import os
import queue
import re
import time
from collections import deque
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


PROFILE_DIR_BASE = "pod101_profile"

REST_BETWEEN_LESSONS = 20
REST_BETWEEN_JOB_STARTS = 8
REST_BETWEEN_JOB_BATCHES = 20

MAX_PARALLEL_LANGUAGES = 2
MAX_PARALLEL_URLS_PER_LANGUAGE = 2

LESSON_FOLDER_TITLE_MAX = 24

EXAMPLE_URLS = [
    "https://www.koreanclass101.com/lesson/lower-beginner-12-finding-your-way-around-a-korean-hotel?lp=260",
    "https://www.koreanclass101.com/lesson-library/level-2-korean?disable_ssr=1",
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def clean(text):
    if not text:
        return ""
    text = html.unescape(str(text))
    return re.sub(r"\s+", " ", text).strip()


def preview(text, max_len=80):
    text = clean(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def safe_filename(text, max_len=80):
    text = clean(text)
    text = re.sub(r'[<>:"/\\|?*]+', "", text)
    text = text.replace("'", "").replace("\u2019", "")
    text = text.replace("...", "").replace("\u2026", "")
    text = re.sub(
        r"[^\w\uAC00-\uD7A3\u3041-\u3093\u30A1-\u30F3\u4E00-\u9FFF\u0430-\u044F\u0410-\u042F\u0451\u0401\u00E0-\u1EF9\u00C0-\u1EF8]+",
        "_",
        text,
        flags=re.UNICODE,
    )
    text = text.strip("._- ")
    text = text.rstrip(". ")
    return text[:max_len].rstrip("._- ") or "item"


def safe_key(text):
    return safe_filename(text, max_len=70).lower()


def ensure_folder(path):
    os.makedirs(path, exist_ok=True)


def ensure_parent_folder(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)


def abs_path(path):
    return os.path.abspath(path)


def title_from_slug(text):
    text = re.sub(r"[_\-]+", " ", clean(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text.title() if text else "Item"


def get_domain(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def get_language_from_domain(url):
    domain = get_domain(url)
    site_name = domain.split(".")[0]
    site_name = re.sub(r"(class101|pod101)$", "", site_name, flags=re.IGNORECASE)
    return title_from_slug(site_name)


def guess_level_from_url(url):
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if len(path_parts) < 2:
        return ""

    slug = clean(path_parts[-1]).lower()
    patterns = [
        r"^(absolute-beginner)",
        r"^(upper-beginner)",
        r"^(lower-beginner)",
        r"^(beginner)",
        r"^(upper-intermediate)",
        r"^(lower-intermediate)",
        r"^(intermediate)",
        r"^(advanced)",
        r"^(level-\d+)",
        r"^(a\d+)",
        r"^(b\d+)",
        r"^(c\d+)",
    ]

    for pattern in patterns:
        match = re.match(pattern, slug)
        if match:
            return title_from_slug(match.group(1))

    return ""


def detect_url_type(url):
    path = urlparse(url).path.lower()
    if "/lesson/" in path:
        return "lesson"
    if "/lesson-library/" in path:
        return "level"
    return "level"


def url_type_label(url_type):
    return {
        "lesson": "Single Lesson",
        "level": "Level Page",
    }.get(url_type, "Level Page")


def canonical_url_key(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or parsed.path
    keep_query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() == "lp"]
    query = urlencode(keep_query, doseq=True)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def expand_url(raw, base_url):
    if not raw:
        return ""
    raw = html.unescape(str(raw)).replace("\\/", "/").replace("&amp;", "&")
    return urljoin(base_url, raw).split("#")[0]


def looks_like_pod101_url(url):
    domain = get_domain(url)
    return domain.endswith("pod101.com") or domain.endswith("class101.com")


def extract_urls_from_text(text):
    matches = re.findall(r"https?://[^\s]+", text)
    urls = []

    for match in matches:
        url = match.rstrip("),]}>.,;")
        urls.append(clean(url))

    return urls


def choose_url_type(url):
    detected = detect_url_type(url)
    default_choice = "1" if detected == "lesson" else "2"

    print("URL type options:")
    print(f"1. Single lesson [{url_type_label('lesson')}]")
    print(f"2. Level page [{url_type_label('level')}]")

    while True:
        choice = clean(input(f"Choose URL type [{default_choice}]: ")) or default_choice

        if choice == "1":
            return "lesson"
        if choice == "2":
            return "level"

        print("Please enter 1 or 2.")


def prompt_for_job_entries():
    print("\nPaste the Pod101/Class101 URLs you want to scrape.")
    print("You can paste one per line, or paste multiple URLs on one line.")
    print("Press ENTER on a blank line when you are done.\n")
    print("Only these URL types are supported in this script:")
    print("- Single lesson pages")
    print("- Level pages")

    print("\nExamples:")
    for example in EXAMPLE_URLS:
        print("-", example)

    collected_entries = []
    seen_keys = set()

    while True:
        line = input("\nURL or pasted batch: ").strip()
        if not line:
            break

        found_urls = extract_urls_from_text(line)
        if not found_urls:
            print("No URL found in that input. Paste full https://... links.")
            continue

        for url in found_urls:
            if not looks_like_pod101_url(url):
                print(f"Skipping unsupported domain: {url}")
                continue

            key = canonical_url_key(url)
            if key in seen_keys:
                print(f"Skipping duplicate URL: {url}")
                continue

            detected_language = get_language_from_domain(url)
            guessed_level = guess_level_from_url(url)

            print("\nURL accepted:")
            print(url)

            chosen_type = choose_url_type(url)
            language = clean(input(f"Language for this URL [{detected_language}]: ")) or detected_language

            if guessed_level:
                level_prompt = f"Level for this URL [{guessed_level}]: "
            else:
                level_prompt = "Level for this URL: "

            level = clean(input(level_prompt)) or guessed_level or "unspecified"

            collected_entries.append(
                {
                    "url": url,
                    "url_type": chosen_type,
                    "language": language,
                    "level": level,
                }
            )
            seen_keys.add(key)

            print(
                f"Added job {len(collected_entries)}: "
                f"{language} / {level} / {url_type_label(chosen_type)} / {url}"
            )

    return collected_entries


def get_profile_dir(language_safe):
    return os.path.join(SCRIPT_DIR, f"{PROFILE_DIR_BASE}_{language_safe}")


def get_job_slug(url):
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if not path_parts:
        return "job"
    return safe_key(path_parts[-1])


def build_jobs(raw_entries):
    jobs = []

    for entry in raw_entries:
        url = clean(entry.get("url"))
        if not url:
            continue

        url_type = clean(entry.get("url_type")) or detect_url_type(url)
        language = clean(entry.get("language")) or get_language_from_domain(url)
        level = clean(entry.get("level")) or "unspecified"
        language_safe = safe_key(language)
        level_safe = safe_key(level)
        job_slug = get_job_slug(url)
        job_hash = hashlib.sha1(canonical_url_key(url).encode("utf-8")).hexdigest()[:10]

        jobs.append(
            {
                "url": url,
                "url_type": url_type,
                "url_type_label": url_type_label(url_type),
                "domain": get_domain(url),
                "language": language,
                "language_safe": language_safe,
                "level": level,
                "level_safe": level_safe,
                "job_slug": job_slug,
                "job_hash": job_hash,
                "job_label": title_from_slug(job_slug),
            }
        )

    return jobs


def group_jobs_by_language(jobs):
    grouped = {}

    for job in jobs:
        language_safe = job["language_safe"]

        if language_safe not in grouped:
            grouped[language_safe] = {
                "language": job["language"],
                "language_safe": language_safe,
                "profile_dir": get_profile_dir(language_safe),
                "jobs": [],
                "levels": set(),
            }

        grouped[language_safe]["jobs"].append(job)
        grouped[language_safe]["levels"].add(job["level"])

    output = []
    for bundle in grouped.values():
        bundle["levels"] = sorted(bundle["levels"])
        output.append(bundle)

    return output


def dedupe_rows(rows):
    output = []
    seen = set()

    for row in rows:
        key = (
            clean(row.get("Type")),
            clean(row.get("Front")),
            clean(row.get("Back")),
            clean(row.get("Audio")),
        )
        if key in seen:
            continue

        seen.add(key)
        output.append(
            {
                "Type": clean(row.get("Type")),
                "Front": clean(row.get("Front")),
                "Back": clean(row.get("Back")),
                "Audio": clean(row.get("Audio")),
            }
        )

    return output


def read_csv_rows(path):
    if not os.path.exists(path):
        return []

    try:
        df = pd.read_csv(path)
        return df.to_dict("records")
    except Exception:
        return []


def save_csv(rows, path):
    ensure_parent_folder(path)
    rows = dedupe_rows(rows)
    df = pd.DataFrame(rows, columns=["Type", "Front", "Back", "Audio"])
    df.to_csv(path, index=False, encoding="utf-8-sig")


def merge_and_save_csv(existing_path, new_rows):
    old_rows = read_csv_rows(existing_path)
    save_csv(old_rows + new_rows, existing_path)


def get_parallel_limit(configured_value, total_items):
    if total_items <= 0:
        return 1

    if not configured_value or configured_value <= 0:
        return total_items

    return max(1, min(configured_value, total_items))


def get_level_root_dir(job):
    return os.path.join(SCRIPT_DIR, job["language_safe"], job["level_safe"])


def get_level_master_csv(job):
    return os.path.join(
        get_level_root_dir(job),
        safe_filename(f"{job['language_safe']}_{job['level_safe']}_MASTER_anki", max_len=90) + ".csv",
    )


def get_job_temp_csv(job):
    return os.path.join(
        get_level_root_dir(job),
        f"__job_{job['job_hash']}.csv",
    )


def get_lesson_id(lesson_url):
    path_parts = [part for part in urlparse(lesson_url).path.split("/") if part]
    if path_parts:
        return safe_key(path_parts[-1])[:24]
    return "lesson"


def get_lesson_folder(root_dir, lesson_number, lesson_title, lesson_id, job_type):
    title_part = safe_filename(lesson_title, max_len=LESSON_FOLDER_TITLE_MAX).lower()
    id_part = safe_filename(lesson_id, max_len=12).lower()

    if job_type == "lesson":
        prefix = "single"
    else:
        prefix = f"{lesson_number:03d}"

    folder_name = f"{prefix}_{title_part}__{id_part}".strip("_")
    return os.path.join(root_dir, folder_name)


def find_existing_lesson_folder(root_dir, lesson_id):
    if not os.path.exists(root_dir):
        return None

    suffix = "__" + safe_filename(lesson_id, max_len=12).lower()

    for current_root, dir_names, _ in os.walk(root_dir):
        for name in dir_names:
            path = os.path.join(current_root, name)
            if name.endswith(suffix):
                return path

    return None


def lesson_already_done(root_dir, lesson_id):
    lesson_folder = find_existing_lesson_folder(root_dir, lesson_id)
    if not lesson_folder:
        return False

    csv_path = os.path.join(lesson_folder, "anki.csv")
    return os.path.exists(csv_path) and os.path.getsize(csv_path) > 0


def claim_lesson_lock(root_dir, lesson_id):
    ensure_folder(root_dir)
    lock_name = f".lock_{safe_filename(lesson_id, max_len=24).lower()}"
    lock_path = os.path.join(root_dir, lock_name)

    try:
        file_handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(file_handle)
        return lock_path
    except FileExistsError:
        return ""


def release_lesson_lock(lock_path):
    if lock_path and os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except Exception:
            pass


def is_audio(url):
    if not url:
        return False
    path = urlparse(url).path.lower()
    return path.endswith((".mp3", ".m4a", ".wav", ".ogg"))


def get_audio_url(tag, base_url):
    raw = (
        tag.get("data-src")
        or tag.get("data-audio")
        or tag.get("data-url")
        or tag.get("src")
    )
    return expand_url(raw, base_url) if raw else ""


def safe_goto(page, url, timeout=90000, sleep_after=3000):
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(sleep_after)
            return True
        except PlaywrightTimeoutError:
            print(f"Timeout loading page, attempt {attempt}/3:")
            print(url)
            page.wait_for_timeout(4000)
        except Exception as e:
            message = str(e)
            if "interrupted by another navigation" in message:
                print(f"Navigation interrupted, retrying attempt {attempt}/3:")
                print(url)
                page.wait_for_timeout(5000)
                continue

            print(f"Navigation error, attempt {attempt}/3:")
            print(url)
            print(e)
            page.wait_for_timeout(5000)

    print("Could not safely load page:")
    print(url)
    return False


def scroll_page(page):
    for _ in range(18):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        except Exception:
            page.wait_for_timeout(3000)


def get_page_title_from_html_content(page_html):
    soup = BeautifulSoup(page_html, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = clean(h1.get_text(" ", strip=True))
        if title:
            return title

    title_tag = soup.find("title")
    if title_tag:
        title = clean(title_tag.get_text(" ", strip=True))
        title = re.sub(r"\s*-\s*.*101.*$", "", title)
        if title:
            return title

    return "lesson"


def collect_lesson_links(page, base_url):
    scroll_page(page)

    page_html = page.content()
    soup = BeautifulSoup(page_html, "html.parser")

    links = []
    seen = set()

    def add_link(raw):
        if not raw:
            return

        full_url = expand_url(raw, base_url)
        if not full_url or "/lesson/" not in urlparse(full_url).path.lower():
            return

        key = canonical_url_key(full_url)
        if key in seen:
            return

        seen.add(key)
        links.append(full_url)

    for a in soup.select("a[href]"):
        add_link(a.get("href"))

    patterns = [
        r'"url"\s*:\s*"([^"]*?/lesson/[^"]+)"',
        r"&quot;url&quot;\s*:\s*&quot;([^&]+?/lesson/[^&]+)&quot;",
        r"(\/lesson\/[^\"'<>\s]+?\?lp=\d+)",
        r"(https?://[^\"'<>\s]+?/lesson/[^\"'<>\s]+?\?lp=\d+)",
        r"(\/lesson\/[^\"'<>\s]+)",
        r"(https?://[^\"'<>\s]+?/lesson/[^\"'<>\s]+)",
    ]

    for pattern in patterns:
        for match in re.findall(pattern, page_html):
            add_link(match)

    return links


def download_audio(context, url, path, label):
    try:
        ensure_parent_folder(path)

        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"    Using existing audio for {label}: {abs_path(path)}")
            return True

        print(f"    Downloading audio for {label}: {abs_path(path)}")
        response = context.request.get(url)

        if response.status != 200:
            print("    Failed audio request:")
            print("    ", url)
            print("    ", "HTTP", response.status)
            return False

        with open(path, "wb") as file_handle:
            file_handle.write(response.body())

        print(f"    Saved audio for {label}: {abs_path(path)}")
        return True

    except Exception as e:
        print("    Audio download error:")
        print("    ", url)
        print("    ", e)
        return False


def extract_dialogue_items(soup, lesson_url, seen_audio):
    items = []

    for tr in soup.select("tr"):
        btn = tr.select_one(".js-lsn3-play-dialogue[data-src], .js-lsn3-play-dialogue")
        if not btn:
            continue

        audio_url = get_audio_url(btn, lesson_url)
        if not is_audio(audio_url):
            continue

        audio_key = canonical_url_key(audio_url)
        if audio_key in seen_audio:
            continue

        speaker_tag = tr.select_one(".lsn3-lesson-dialogue__td--name")
        text_tag = tr.select_one(".lsn3-lesson-dialogue__td--text")

        speaker = clean(speaker_tag.get_text(" ", strip=True)) if speaker_tag else ""
        text = clean(text_tag.get_text(" ", strip=True)) if text_tag else clean(btn.get("data-text"))
        back = clean(btn.get("data-english-text"))

        front = f"{speaker} {text}" if speaker and text and not text.startswith(speaker) else text
        if not front:
            continue

        seen_audio.add(audio_key)
        items.append(
            {
                "audio_url": audio_url,
                "front": front,
                "back": back,
            }
        )

    return items


def extract_vocab_items(soup, lesson_url, seen_audio):
    items = []

    vocab_rows = soup.select(
        "tr:has(.lsn3-lesson-vocabulary__lang), "
        "tr:has(.lesson-vocabulary__lang), "
        "tr:has([class*='vocabulary__lang'])"
    )

    for tr in vocab_rows:
        classes = " ".join(tr.get("class", []))
        if "sample" in classes.lower() or "example" in classes.lower():
            continue

        word_tag = tr.select_one(
            ".lsn3-lesson-vocabulary__lang, "
            ".lesson-vocabulary__lang, "
            "[class*='vocabulary__lang']"
        )
        meaning_tag = tr.select_one(
            ".lsn3-lesson-vocabulary__definition, "
            ".lesson-vocabulary__definition, "
            "[class*='definition'], "
            "[class*='meaning']"
        )
        btn = tr.select_one(".js-lsn3-play-vocabulary[data-src], .js-lsn3-play-vocabulary")

        if not word_tag or not btn:
            continue

        audio_url = get_audio_url(btn, lesson_url)
        if not is_audio(audio_url):
            continue

        audio_key = canonical_url_key(audio_url)
        if audio_key in seen_audio:
            continue

        word = clean(word_tag.get_text(" ", strip=True))
        meaning = clean(meaning_tag.get_text(" ", strip=True)) if meaning_tag else ""
        if not word:
            continue

        seen_audio.add(audio_key)
        items.append(
            {
                "audio_url": audio_url,
                "front": word,
                "back": meaning,
            }
        )

    return items


def extract_sentence_items(soup, lesson_url, seen_audio):
    items = []

    example_blocks = soup.select(
        ".lsn3-lesson-vocabulary__sample, "
        ".lesson-vocabulary__sample, "
        "[class*='vocabulary__sample'], "
        "[class*='sample-sentence'], "
        "[class*='example']"
    )

    for block in example_blocks:
        btn = block.select_one(".js-lsn3-play-vocabulary[data-src], .js-lsn3-play-vocabulary")
        if not btn:
            continue

        audio_url = get_audio_url(btn, lesson_url)
        if not is_audio(audio_url):
            continue

        audio_key = canonical_url_key(audio_url)
        if audio_key in seen_audio:
            continue

        text = clean(btn.get("data-text")) or clean(block.get_text(" ", strip=True))
        if not text:
            continue

        seen_audio.add(audio_key)
        items.append(
            {
                "audio_url": audio_url,
                "front": text,
                "back": "",
            }
        )

    return items


def short_language_tag(text):
    parts = [part for part in safe_key(text).split("_") if part]

    if not parts:
        return "lang"
    if len(parts) == 1:
        return parts[0][:4] or "lang"

    initials = "".join(part[0] for part in parts[:6])
    return initials[:6] or parts[0][:4] or "lang"


def short_lesson_tag(lesson_id):
    lesson_id = safe_key(lesson_id)
    if lesson_id:
        return lesson_id[:10]
    return "lesson"


def build_audio_filename(job, lesson_number, lesson_id, audio_kind, index, audio_url=""):
    lang_tag = short_language_tag(job["language_safe"])
    lesson_tag = short_lesson_tag(lesson_id)
    source_hash = hashlib.sha1(clean(audio_url).encode("utf-8")).hexdigest()[:6] if audio_url else "audio0"
    stem = f"{lang_tag}_{lesson_number:03d}_{lesson_tag}_{audio_kind}{index:02d}_{source_hash}"
    stem = safe_filename(stem, max_len=40)
    return stem + ".mp3"


def scrape_lesson(context, page, lesson_url, lesson_number, job, root_dir):
    lesson_id = get_lesson_id(lesson_url)

    print("\n" + "=" * 95)
    print(f"DOWNLOADING LESSON {lesson_number:03d}")
    print(f"Language:   {job['language']}")
    print(f"Level:      {job['level']}")
    print(f"URL type:   {job['url_type_label']}")
    print(f"Job label:  {job['job_label']}")
    print(f"Lesson URL: {lesson_url}")
    print("=" * 95)

    if not safe_goto(page, lesson_url, timeout=90000, sleep_after=4000):
        print("Could not open lesson. Skipping.")
        return []

    page_html = page.content()
    soup = BeautifulSoup(page_html, "html.parser")

    title = get_page_title_from_html_content(page_html)
    lesson_folder = get_lesson_folder(root_dir, lesson_number, title, lesson_id, job["url_type"])
    ensure_folder(lesson_folder)

    print(f"\nLesson title:  {title}")
    print(f"Lesson ID:     {lesson_id}")
    print(f"Saving folder: {abs_path(lesson_folder)}")

    lesson_meta = f"{job['language']} - {job['level']} - {lesson_number:03d} - {title}"

    rows = []
    seen_audio = set()
    audio_ready_count = 0

    dialogue_items = extract_dialogue_items(soup, lesson_url, seen_audio)
    vocab_items = extract_vocab_items(soup, lesson_url, seen_audio)
    sentence_items = extract_sentence_items(soup, lesson_url, seen_audio)

    print("\nLESSON CONTENT FOUND")
    print("-" * 70)
    print(f"Dialogue items:  {len(dialogue_items)}")
    print(f"Vocab items:     {len(vocab_items)}")
    print(f"Sentence items:  {len(sentence_items)}")
    print(f"Potential cards: {len(dialogue_items) + len(vocab_items) + len(sentence_items)}")
    print("-" * 70)

    dialogue_card_count = 0
    print("\nDownloading dialogue audio and building dialogue cards...")
    for index, item in enumerate(dialogue_items, 1):
        filename = build_audio_filename(job, lesson_number, lesson_id, "d", index, item["audio_url"])
        path = os.path.join(lesson_folder, filename)

        print(f"  [Dialogue {index:02d}/{len(dialogue_items)}] {preview(item['front'])}")
        if download_audio(context, item["audio_url"], path, f"dialogue {index:02d}"):
            audio_ready_count += 1
            dialogue_card_count += 1
            rows.append(
                {
                    "Type": f"{lesson_meta} (Dialogue Line)",
                    "Front": item["front"],
                    "Back": item["back"],
                    "Audio": f"[sound:{filename}]",
                }
            )

    vocab_card_count = 0
    print("\nDownloading vocabulary audio and building vocab cards...")
    for index, item in enumerate(vocab_items, 1):
        filename = build_audio_filename(job, lesson_number, lesson_id, "w", index, item["audio_url"])
        path = os.path.join(lesson_folder, filename)

        print(f"  [Vocab {index:02d}/{len(vocab_items)}] {preview(item['front'])}")
        if download_audio(context, item["audio_url"], path, f"vocab {index:02d}"):
            audio_ready_count += 1
            vocab_card_count += 1
            rows.append(
                {
                    "Type": f"{lesson_meta} (Vocab Word)",
                    "Front": item["front"],
                    "Back": item["back"],
                    "Audio": f"[sound:{filename}]",
                }
            )

    sentence_card_count = 0
    print("\nDownloading sentence audio and building sentence cards...")
    for index, item in enumerate(sentence_items, 1):
        filename = build_audio_filename(job, lesson_number, lesson_id, "s", index, item["audio_url"])
        path = os.path.join(lesson_folder, filename)

        print(f"  [Sentence {index:02d}/{len(sentence_items)}] {preview(item['front'])}")
        if download_audio(context, item["audio_url"], path, f"sentence {index:02d}"):
            audio_ready_count += 1
            sentence_card_count += 1
            rows.append(
                {
                    "Type": f"{lesson_meta} (Vocab Sentence)",
                    "Front": item["front"],
                    "Back": item["back"],
                    "Audio": f"[sound:{filename}]",
                }
            )

    csv_path = os.path.join(lesson_folder, "anki.csv")
    save_csv(rows, csv_path)

    print("\nLESSON SUMMARY")
    print("-" * 75)
    print(f"Lesson:             {lesson_number:03d} - {title}")
    print(f"Folder:             {abs_path(lesson_folder)}")
    print(f"Dialogue cards:     {dialogue_card_count}")
    print(f"Vocab cards:        {vocab_card_count}")
    print(f"Sentence cards:     {sentence_card_count}")
    print(f"Audio ready:        {audio_ready_count}")
    print(f"Total lesson cards: {len(rows)}")
    print(f"CSV saved to:       {abs_path(csv_path)}")
    print("-" * 75)

    return rows


def process_one_lesson(context, page, lesson_url, lesson_number, job, root_dir):
    lesson_id = get_lesson_id(lesson_url)

    if lesson_already_done(root_dir, lesson_id):
        existing_folder = find_existing_lesson_folder(root_dir, lesson_id)
        print(f"\nSkipping lesson because CSV already exists: {lesson_url}")
        if existing_folder:
            print(f"Existing folder: {abs_path(existing_folder)}")
        return [], 1

    lock_path = claim_lesson_lock(root_dir, lesson_id)
    if not lock_path:
        print(f"\nSkipping lesson because another worker is already handling it: {lesson_url}")
        return [], 1

    try:
        if lesson_already_done(root_dir, lesson_id):
            existing_folder = find_existing_lesson_folder(root_dir, lesson_id)
            print(f"\nSkipping lesson because CSV already exists: {lesson_url}")
            if existing_folder:
                print(f"Existing folder: {abs_path(existing_folder)}")
            return [], 1

        return scrape_lesson(context, page, lesson_url, lesson_number, job, root_dir), 0
    finally:
        release_lesson_lock(lock_path)


def process_single_lesson_job(context, page, job, job_index, total_jobs):
    root_dir = get_level_root_dir(job)
    ensure_folder(root_dir)

    print("\n" + "=" * 100)
    print(f"STARTING JOB {job_index}/{total_jobs}")
    print(f"Language:  {job['language']}")
    print(f"Level:     {job['level']}")
    print(f"URL type:  {job['url_type_label']}")
    print(f"Job label: {job['job_label']}")
    print(f"Folder:    {abs_path(root_dir)}")
    print(f"URL:       {job['url']}")
    print("=" * 100)

    rows, skipped = process_one_lesson(
        context=context,
        page=page,
        lesson_url=job["url"],
        lesson_number=1,
        job=job,
        root_dir=root_dir,
    )

    save_csv(rows, get_job_temp_csv(job))

    print("\nJOB SUMMARY")
    print("=" * 80)
    print(f"Job:                {job_index}/{total_jobs}")
    print(f"Language:           {job['language']}")
    print(f"Level:              {job['level']}")
    print(f"URL type:           {job['url_type_label']}")
    print(f"New cards this job: {len(rows)}")
    print(f"Skipped lessons:    {skipped}")
    print("=" * 80)

    return rows, skipped


def process_level_job(context, page, job, job_index, total_jobs):
    root_dir = get_level_root_dir(job)
    ensure_folder(root_dir)

    print("\n" + "=" * 100)
    print(f"STARTING JOB {job_index}/{total_jobs}")
    print(f"Language:  {job['language']}")
    print(f"Level:     {job['level']}")
    print(f"URL type:  {job['url_type_label']}")
    print(f"Job label: {job['job_label']}")
    print(f"Folder:    {abs_path(root_dir)}")
    print(f"URL:       {job['url']}")
    print("=" * 100)

    if not safe_goto(page, job["url"], timeout=90000, sleep_after=5000):
        print("Could not open level page. Skipping this job.")
        save_csv([], get_job_temp_csv(job))
        return [], 0

    lesson_links = collect_lesson_links(page, job["url"])
    if not lesson_links:
        print("\nNo lesson links found on that level page.")
        print("Treating the input as one lesson URL instead.")
        lesson_links = [job["url"]]

    print(f"\nFound {len(lesson_links)} lessons for this job.\n")
    for index, link in enumerate(lesson_links, 1):
        print(f"{index:03d}. {link}")

    all_rows = []
    skipped_count = 0

    for lesson_number, lesson_url in enumerate(lesson_links, 1):
        rows, skipped = process_one_lesson(
            context=context,
            page=page,
            lesson_url=lesson_url,
            lesson_number=lesson_number,
            job=job,
            root_dir=root_dir,
        )

        all_rows.extend(rows)
        skipped_count += skipped

        print("\nJOB RUNNING TOTAL")
        print("-" * 70)
        print(f"Finished lesson:    {lesson_number}/{len(lesson_links)}")
        print(f"Cards this lesson:  {len(rows)}")
        print(f"Cards this job:     {len(all_rows)}")
        print(f"Skipped lessons:    {skipped_count}")
        print("-" * 70)

        if lesson_number < len(lesson_links):
            print(f"\nResting {REST_BETWEEN_LESSONS} seconds before next lesson...\n")
            time.sleep(REST_BETWEEN_LESSONS)

    save_csv(all_rows, get_job_temp_csv(job))

    print("\nJOB SUMMARY")
    print("=" * 80)
    print(f"Job:                {job_index}/{total_jobs}")
    print(f"Language:           {job['language']}")
    print(f"Level:              {job['level']}")
    print(f"URL type:           {job['url_type_label']}")
    print(f"Lessons found:      {len(lesson_links)}")
    print(f"Skipped lessons:    {skipped_count}")
    print(f"New cards this job: {len(all_rows)}")
    print("=" * 80)

    return all_rows, skipped_count


def process_job(context, page, job, job_index, total_jobs):
    if job["url_type"] == "lesson":
        return process_single_lesson_job(context, page, job, job_index, total_jobs)
    return process_level_job(context, page, job, job_index, total_jobs)


def install_print_prefix(prefix):
    original_print = builtins.print

    def prefixed_print(*args, **kwargs):
        if args:
            return original_print(f"[{prefix}]", *args, **kwargs)
        return original_print(*args, **kwargs)

    builtins.print = prefixed_print
    return original_print


def open_login_tabs_for_language(context, language_bundle):
    print(f"\nOpening login tabs for {language_bundle['language']}...")

    domain_to_url = {}
    for job in language_bundle["jobs"]:
        domain_to_url.setdefault(job["domain"], job["url"])

    for domain, url in sorted(domain_to_url.items()):
        page = context.new_page()
        safe_goto(page, url, timeout=90000, sleep_after=3000)
        print(f"Opened login tab for {language_bundle['language']}: {domain}")


def get_language_storage_state_path(language_bundle):
    profile_dir = abs_path(language_bundle["profile_dir"])
    ensure_folder(profile_dir)
    return os.path.join(profile_dir, f"{language_bundle['language_safe']}_storage_state.json")


def save_language_storage_state(context, language_bundle):
    storage_state_path = get_language_storage_state_path(language_bundle)
    context.storage_state(path=storage_state_path)
    return storage_state_path


def make_job_summary(job, cards, skipped):
    return {
        "language": job["language"],
        "level": job["level"],
        "type": job["url_type_label"],
        "label": job["job_label"],
        "cards": cards,
        "skipped": skipped,
    }


def merge_level_masters_for_language(language_bundle):
    level_rows = {}
    level_jobs = {}
    master_paths = []

    for job in language_bundle["jobs"]:
        level_rows.setdefault(job["level_safe"], [])
        level_rows[job["level_safe"]].extend(read_csv_rows(get_job_temp_csv(job)))
        level_jobs[job["level_safe"]] = job

    for level_safe, rows in level_rows.items():
        job = level_jobs[level_safe]
        master_path = get_level_master_csv(job)
        merge_and_save_csv(master_path, rows)
        master_paths.append(master_path)

    for job in language_bundle["jobs"]:
        temp_path = get_job_temp_csv(job)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    return master_paths


def run_job_worker_from_storage_state(language_bundle, job, job_index, total_jobs, storage_state_path, result_queue):
    restore_print = install_print_prefix(f"{language_bundle['language']} {job_index:02d}")
    browser = None
    context = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(storage_state=storage_state_path)
            page = context.new_page()

            rows, skipped = process_job(
                context=context,
                page=page,
                job=job,
                job_index=job_index,
                total_jobs=total_jobs,
            )

        result_queue.put(
            {
                "job_index": job_index,
                "status": "ok",
                "new_cards": len(rows),
                "skipped": skipped,
                "job_summary": make_job_summary(job, len(rows), skipped),
            }
        )
    except Exception as e:
        result_queue.put(
            {
                "job_index": job_index,
                "status": "error",
                "new_cards": 0,
                "skipped": 0,
                "job_summary": make_job_summary(job, 0, 0),
                "error": str(e),
            }
        )
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        builtins.print = restore_print


def drain_result_queue(result_queue):
    results = []

    while True:
        try:
            results.append(result_queue.get_nowait())
        except queue.Empty:
            break

    return results


def run_jobs_parallel_for_language(language_bundle, storage_state_path):
    jobs = language_bundle["jobs"]
    total_jobs = len(jobs)

    if not jobs:
        return [], 0, 0, []

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    parallel_limit = get_parallel_limit(MAX_PARALLEL_URLS_PER_LANGUAGE, total_jobs)
    results_by_index = {}
    total_new_cards = 0
    total_skipped = 0
    job_errors = []

    for batch_start in range(0, total_jobs, parallel_limit):
        batch = [
            (job_index, jobs[job_index - 1])
            for job_index in range(batch_start + 1, min(batch_start + parallel_limit, total_jobs) + 1)
        ]
        processes = []

        print("\nStarting URL batch for language:")
        for job_index, job in batch:
            print(f"- {job_index}/{total_jobs}: {job['job_label']} ({job['url_type_label']})")

        for offset, (job_index, job) in enumerate(batch, 1):
            if offset > 1 and REST_BETWEEN_JOB_STARTS > 0:
                print(f"\nResting {REST_BETWEEN_JOB_STARTS} seconds before starting the next URL worker...\n")
                time.sleep(REST_BETWEEN_JOB_STARTS)

            process = ctx.Process(
                target=run_job_worker_from_storage_state,
                args=(language_bundle, job, job_index, total_jobs, storage_state_path, result_queue),
            )
            process.start()
            processes.append((job_index, job, process))

        for _, _, process in processes:
            process.join()

        batch_results = {result["job_index"]: result for result in drain_result_queue(result_queue)}

        for job_index, job, process in processes:
            result = batch_results.get(job_index)

            if not result:
                result = {
                    "job_index": job_index,
                    "status": "error",
                    "new_cards": 0,
                    "skipped": 0,
                    "job_summary": make_job_summary(job, 0, 0),
                    "error": f"Job worker exited with code {process.exitcode}",
                }

            results_by_index[job_index] = result
            total_new_cards += result.get("new_cards", 0)
            total_skipped += result.get("skipped", 0)

            if result.get("status") != "ok":
                job_errors.append(
                    {
                        "job_index": job_index,
                        "language": job["language"],
                        "level": job["level"],
                        "type": job["url_type_label"],
                        "label": job["job_label"],
                        "error": result.get("error", "Unknown error"),
                    }
                )

            print("\nLANGUAGE RUNNING TOTAL")
            print("-" * 75)
            print(f"Finished job:       {job_index}/{total_jobs}")
            print(f"Cards this job:     {result.get('new_cards', 0)}")
            print(f"Skipped this job:   {result.get('skipped', 0)}")
            print(f"Language new cards: {total_new_cards}")
            print(f"Language skipped:   {total_skipped}")
            if result.get("status") != "ok":
                print(f"Job error:          {result.get('error', 'Unknown error')}")
            print("-" * 75)

        if batch_start + parallel_limit < total_jobs and REST_BETWEEN_JOB_BATCHES > 0:
            print(f"\nResting {REST_BETWEEN_JOB_BATCHES} seconds before the next URL batch...\n")
            time.sleep(REST_BETWEEN_JOB_BATCHES)

    ordered_results = [results_by_index[index] for index in sorted(results_by_index)]
    return ordered_results, total_new_cards, total_skipped, job_errors


def run_language_worker(language_bundle, control_queue, start_event, result_queue):
    restore_print = install_print_prefix(language_bundle["language"])
    context = None
    login_signal_sent = False
    storage_state_path = ""

    total_new_cards = 0
    total_skipped = 0
    job_summaries = []
    job_errors = []
    level_master_paths = []

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=language_bundle["profile_dir"],
                headless=False,
            )
            open_login_tabs_for_language(context, language_bundle)
            control_queue.put(
                {
                    "kind": "login_ready",
                    "language": language_bundle["language"],
                    "language_safe": language_bundle["language_safe"],
                }
            )
            login_signal_sent = True

            print("\nLogin tabs are ready in this worker.")
            print("Waiting for the main script to continue after you finish logging in...")
            start_event.wait()
            storage_state_path = save_language_storage_state(context, language_bundle)

        context = None

        ordered_job_results, total_new_cards, total_skipped, job_errors = run_jobs_parallel_for_language(
            language_bundle,
            storage_state_path,
        )
        job_summaries = [
            result["job_summary"]
            for result in ordered_job_results
            if result.get("status") == "ok"
        ]
        level_master_paths = merge_level_masters_for_language(language_bundle)

        print("\nLANGUAGE SUMMARY")
        print("=" * 85)
        print(f"Language:              {language_bundle['language']}")
        print(f"Jobs completed:        {len(language_bundle['jobs'])}")
        print(f"Parallel URL workers:  {get_parallel_limit(MAX_PARALLEL_URLS_PER_LANGUAGE, len(language_bundle['jobs']))}")
        print(f"Total new cards:       {total_new_cards}")
        print(f"Total skipped lessons: {total_skipped}")
        print(f"Level master CSVs:     {len(level_master_paths)}")
        if job_errors:
            print(f"Job errors:            {len(job_errors)}")
        print("=" * 85)

        result_queue.put(
            {
                "language": language_bundle["language"],
                "language_safe": language_bundle["language_safe"],
                "job_summaries": job_summaries,
                "new_cards": total_new_cards,
                "skipped": total_skipped,
                "errors": job_errors,
                "level_master_paths": level_master_paths,
                "status": "ok" if not job_errors else "error",
            }
        )
    except Exception as e:
        if not login_signal_sent:
            control_queue.put(
                {
                    "kind": "login_error",
                    "language": language_bundle["language"],
                    "language_safe": language_bundle["language_safe"],
                    "error": str(e),
                }
            )

        result_queue.put(
            {
                "language": language_bundle["language"],
                "language_safe": language_bundle["language_safe"],
                "job_summaries": job_summaries,
                "new_cards": total_new_cards,
                "skipped": total_skipped,
                "errors": job_errors,
                "level_master_paths": level_master_paths,
                "status": "error",
                "error": str(e),
            }
        )
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        if storage_state_path and os.path.exists(storage_state_path):
            try:
                os.remove(storage_state_path)
            except Exception:
                pass
        builtins.print = restore_print


def run_languages_parallel(language_bundles):
    if not language_bundles:
        return []

    ctx = multiprocessing.get_context("spawn")
    control_queue = ctx.Queue()
    result_queue = ctx.Queue()
    results = []

    parallel_limit = get_parallel_limit(MAX_PARALLEL_LANGUAGES, len(language_bundles))

    for start in range(0, len(language_bundles), parallel_limit):
        batch = language_bundles[start : start + parallel_limit]
        processes = []
        start_event = ctx.Event()

        print("\nStarting parallel language batch:")
        for bundle in batch:
            print(f"- {bundle['language']} ({len(bundle['jobs'])} jobs)")

        for bundle in batch:
            process = ctx.Process(
                target=run_language_worker,
                args=(bundle, control_queue, start_event, result_queue),
            )
            process.start()
            processes.append((bundle, process))

        login_status_by_language = {}

        while len(login_status_by_language) < len(batch):
            signal = control_queue.get()
            language_safe = signal["language_safe"]

            if language_safe in login_status_by_language:
                continue

            login_status_by_language[language_safe] = signal

            if signal["kind"] == "login_ready":
                print(f"Login window ready for {signal['language']}.")
            else:
                print(f"Could not prepare login window for {signal['language']}: {signal.get('error', 'Unknown error')}")

        ready_languages = [
            signal["language"]
            for signal in login_status_by_language.values()
            if signal["kind"] == "login_ready"
        ]

        if ready_languages:
            print("\nThese login windows will prepare the session for the download workers:")
            for language in ready_languages:
                print(f"- {language}")
            print("Log in to them now. After that, the script will reuse that session across the parallel download workers.")
            input("\nPress ENTER when these language logins are done... ")

        start_event.set()

        for bundle, process in processes:
            process.join()

            if process.exitcode != 0:
                results.append(
                    {
                        "language": bundle["language"],
                        "language_safe": bundle["language_safe"],
                        "job_summaries": [],
                        "new_cards": 0,
                        "skipped": 0,
                        "errors": [],
                        "level_master_paths": [],
                        "status": "error",
                        "error": f"Worker exited with code {process.exitcode}",
                    }
                )

        results.extend(drain_result_queue(result_queue))

    deduped_results = {}
    for result in results:
        deduped_results[result["language_safe"]] = result

    ordered_results = []
    for bundle in language_bundles:
        ordered_results.append(
            deduped_results.get(
                bundle["language_safe"],
                {
                    "language": bundle["language"],
                    "language_safe": bundle["language_safe"],
                    "job_summaries": [],
                    "new_cards": 0,
                    "skipped": 0,
                    "errors": [],
                    "level_master_paths": [],
                    "status": "error",
                    "error": "No worker result returned",
                },
            )
        )

    return ordered_results


def main():
    raw_entries = prompt_for_job_entries()

    if not raw_entries:
        print("No supported URLs were entered. Exiting.")
        return

    jobs = build_jobs(raw_entries)
    if not jobs:
        print("No jobs could be built from the URLs you entered. Exiting.")
        return

    print("\nJobs queued:")
    for index, job in enumerate(jobs, 1):
        print(
            f"{index}. {job['language']} / {job['level']} / {job['url_type_label']} / "
            f"{job['job_label']} / {job['url']}"
        )

    print("\nUnique sites that will open for login:")
    for domain in sorted(set(job["domain"] for job in jobs)):
        print("-", domain)

    language_bundles = group_jobs_by_language(jobs)

    print("\nLanguage workers that will run in parallel:")
    for bundle in language_bundles:
        print(
            f"- {bundle['language']}: {len(bundle['jobs'])} jobs, "
            f"levels {', '.join(bundle['levels'])}, profile {bundle['profile_dir']}"
        )

    print("\nOutput folders use this layout:")
    print("- language / level / lesson")
    print("- Each lesson folder gets its own audio files and anki.csv")
    print("- Each level folder gets one MASTER_anki.csv")

    print("\nParallel mode:")
    print(f"- Up to {get_parallel_limit(MAX_PARALLEL_LANGUAGES, len(language_bundles))} languages at once.")
    print(f"- Up to {get_parallel_limit(MAX_PARALLEL_URLS_PER_LANGUAGE, max((len(bundle['jobs']) for bundle in language_bundles), default=1))} URLs at once inside each language.")
    print(f"- {REST_BETWEEN_JOB_STARTS} seconds between starting parallel URL workers.")
    print(f"- {REST_BETWEEN_JOB_BATCHES} seconds between URL batches.")
    print(f"- {REST_BETWEEN_LESSONS} seconds between lessons inside one level page.")

    try:
        worker_results = run_languages_parallel(language_bundles)

        print("\nFINAL SUMMARY")
        print("=" * 95)

        summary_index = 1
        all_level_master_paths = []

        for result in worker_results:
            for summary in result.get("job_summaries", []):
                print(
                    f"{summary_index:02d}. {summary['language']} / {summary['level']} / "
                    f"{summary['type']} / {summary['label']} "
                    f"- Cards: {summary['cards']} - Skipped: {summary['skipped']}"
                )
                summary_index += 1

            for error in result.get("errors", []):
                print(
                    f"{summary_index:02d}. {error['language']} / {error['level']} / "
                    f"{error['type']} / {error['label']} "
                    f"- Job Error: {error.get('error', 'Unknown error')}"
                )
                summary_index += 1

            if result.get("error"):
                print(
                    f"{summary_index:02d}. {result['language']} / Worker Error / "
                    f"{result.get('error', 'Unknown error')}"
                )
                summary_index += 1

            all_level_master_paths.extend(result.get("level_master_paths", []))

        all_level_master_paths = list(dict.fromkeys(all_level_master_paths))

        print("-" * 95)
        print("Level master CSVs created or updated:")
        for path in all_level_master_paths:
            print("-", abs_path(path))
        print("Total new cards:", sum(result.get("new_cards", 0) for result in worker_results))
        print("Total skipped lessons:", sum(result.get("skipped", 0) for result in worker_results))
        print("=" * 95)
    except Exception as e:
        print("\nSCRIPT ERROR:")
        print(e)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
