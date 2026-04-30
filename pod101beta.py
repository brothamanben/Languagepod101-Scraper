import builtins
import csv
import html
import os
import random
import re
import time
from collections import deque
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


PROFILE_DIR_BASE = "pod101_profile"
LOGIN_EMAIL = "fooheads@gmail.com"
LOGIN_PASSWORD = "Iloveyoumax1127!"

REST_BETWEEN_LESSONS = 20
REST_BETWEEN_JOBS = 20

LESSON_FOLDER_TITLE_MAX = 45
FILE_NAME_MAX = 110

MAX_LIBRARY_DEPTH = 6
MAX_LIBRARY_PAGES = 500

# Example URL shapes this script accepts.
EXAMPLE_URLS = [
    "https://www.koreanclass101.com/lesson/lower-beginner-12-finding-your-way-around-a-korean-hotel?lp=260",
    "https://www.koreanclass101.com/lesson-library/level-2-korean?disable_ssr=1",
    "https://www.koreanclass101.com/lesson-library/absolute-beginner?isrc=content_pathways_browse",
]

LANGUAGEPOD_SITE_URLS = [
    "https://www.afrikaanspod101.com",
    "https://www.arabicpod101.com",
    "https://www.bulgarianpod101.com",
    "https://www.cantoneseclass101.com",
    "https://www.chineseclass101.com",
    "https://www.czechclass101.com",
    "https://www.danishclass101.com",
    "https://www.dutchpod101.com",
    "https://www.englishclass101.com",
    "https://www.filipinopod101.com",
    "https://www.finnishpod101.com",
    "https://www.frenchpod101.com",
    "https://www.germanpod101.com",
    "https://www.greekpod101.com",
    "https://www.hebrewpod101.com",
    "https://www.hindipod101.com",
    "https://www.hungarianpod101.com",
    "https://www.indonesianpod101.com",
    "https://www.italianpod101.com",
    "https://www.japanesepod101.com",
    "https://www.koreanclass101.com",
    "https://www.norwegianclass101.com",
    "https://www.persianpod101.com",
    "https://www.polishpod101.com",
    "https://www.portuguesepod101.com",
    "https://www.romanianpod101.com",
    "https://www.russianpod101.com",
    "https://www.spanishpod101.com",
    "https://www.swahilipod101.com",
    "https://www.swedishpod101.com",
    "https://www.thaipod101.com",
    "https://www.turkishclass101.com",
    "https://www.urdupod101.com",
    "https://www.vietnamesepod101.com",
]

TARGET_LEVEL_NUMBERS = [1, 2, 3, 4, 5]


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


TYPE_COL = 0
FRONT_COL = 1
BACK_COL = 2
CHOICES_COL = 3
AUDIO_COL = 4
TAGS_COL = 5


def normalize_quotes(text):
    text = str(text)
    return (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("Ã¢â‚¬Å“", '"')
        .replace("Ã¢â‚¬Â", '"')
        .replace("Ã¢â‚¬Ëœ", "'")
        .replace("Ã¢â‚¬â„¢", "'")
    )


def clean_speaker_labels(text):
    text = re.sub(r"(^|\s)[A-D]:\s*", " ", str(text))
    return re.sub(r"\s+", " ", text).strip()


def extract_quoted_english(text):
    matches = re.findall(r'"([^"]+)"', normalize_quotes(text))
    return " / ".join(match.strip() for match in matches if match.strip())


def remove_quoted_text(text):
    text = re.sub(r'"[^"]+"', " ", normalize_quotes(text))
    return re.sub(r"\s+", " ", text).strip()


def split_into_choice_chunks(text, count=4):
    words = clean(text).split()

    if len(words) >= count:
        base_size, remainder = divmod(len(words), count)
        chunks = []
        start = 0
        for index in range(count):
            size = base_size + (1 if index < remainder else 0)
            chunks.append(" ".join(words[start:start + size]))
            start += size
    else:
        chars = list(re.sub(r"\s+", "", clean(text)))
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
    chunks = [chunk for chunk in split_into_choice_chunks(text, 4) if chunk]
    random.shuffle(chunks)

    while len(chunks) < 4:
        chunks.append("")

    return " ".join(f"{chr(65 + index)}) {chunk}".strip() for index, chunk in enumerate(chunks)).strip()


def looks_like_audio(text):
    value = str(text).strip().lower()
    return value.startswith("[sound:") or value.endswith((".mp3", ".m4a", ".wav", ".ogg"))


def extract_level_tag(type_value):
    text = str(type_value).strip()
    language_match = re.match(r"\s*([^-]+?)\s*-\s*", text)
    level_match = re.search(r"\bL\s*([0-9]+)\b", text, flags=re.IGNORECASE)

    if not language_match or not level_match:
        return ""

    language = re.sub(r"[^a-z0-9]+", "", language_match.group(1).lower())
    level_number = level_match.group(1)

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

    if len(row) == 4:
        row.insert(CHOICES_COL, "Choices" if header else "")

    while len(row) <= TAGS_COL:
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
        row[CHOICES_COL] = "Choices"
        row[AUDIO_COL] = "Audio"
        row[TAGS_COL] = "Tags"

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

    if not clean(row[TAGS_COL]):
        row[TAGS_COL] = extract_level_tag(row[TYPE_COL])

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


def abs_path(path):
    return os.path.abspath(path)


def detect_start_url_type(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()

    if "/lesson/" in path:
        return "lesson"
    if "/lesson-library/" in path and "content_pathways_browse" in query:
        return "pathway_link"
    if "/lesson-library/" in path:
        return "level_link"
    return "unknown"


def page_type_label(page_type):
    return {
        "lesson": "Single Lesson Link",
        "level_link": "Level Link",
        "pathway_link": "Pathway Link",
        "unknown": "Unknown",
    }.get(page_type, "Unknown")


def page_type_folder(page_type):
    return {
        "lesson": "direct_lessons",
        "level_link": "level_links",
        "pathway_link": "pathway_links",
        "unknown": "unknown_urls",
    }.get(page_type, "unknown_urls")


def expand_url(raw, base_url):
    if not raw:
        return ""
    raw = html.unescape(str(raw)).replace("\\/", "/").replace("&amp;", "&")
    return urljoin(base_url, raw).split("#")[0]


def canonical_url_key(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or parsed.path
    keep_query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() == "lp"]
    query = urlencode(keep_query, doseq=True)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def is_lesson_url(url):
    return "/lesson/" in urlparse(url).path.lower()


def is_library_url(url):
    return "/lesson-library/" in urlparse(url).path.lower()


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


def collect_lesson_debug_counts(soup):
    return {
        "dialogue_buttons": len(soup.select(".js-lsn3-play-dialogue")),
        "vocab_buttons": len(soup.select(".js-lsn3-play-vocabulary")),
        "dialogue_name_cells": len(soup.select(".lsn3-lesson-dialogue__td--name")),
        "dialogue_text_cells": len(soup.select(".lsn3-lesson-dialogue__td--text")),
        "vocab_lang_cells": len(
            soup.select(
                ".lsn3-lesson-vocabulary__lang, "
                ".lesson-vocabulary__lang, "
                "[class*='vocabulary__lang']"
            )
        ),
        "sample_blocks": len(
            soup.select(
                ".lsn3-lesson-vocabulary__sample, "
                ".lesson-vocabulary__sample, "
                "[class*='vocabulary__sample'], "
                "[class*='sample-sentence'], "
                "[class*='example']"
            )
        ),
    }


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
            msg = str(e)
            if "interrupted by another navigation" in msg:
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


def scroll_page(page, rounds=16):
    for _ in range(rounds):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)
        except Exception:
            page.wait_for_timeout(1500)


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


def get_page_title_from_html(page):
    return get_page_title_from_html_content(page.content())


def get_job_slug(url):
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if not path_parts:
        return "job"
    return safe_key(path_parts[-1])


def get_profile_dir(language_safe):
    return f"{PROFILE_DIR_BASE}_{language_safe}"


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


def extract_urls_from_text(text):
    matches = re.findall(r"https?://[^\s]+", text)
    urls = []

    for match in matches:
        url = match.rstrip("),]}>.,;")
        urls.append(clean(url))

    return urls


def looks_like_pod101_url(url):
    domain = get_domain(url)
    return domain.endswith("pod101.com") or domain.endswith("class101.com")


def dedupe_urls(urls):
    output = []
    seen = set()

    for url in urls:
        key = canonical_url_key(url)
        if key in seen:
            continue
        seen.add(key)
        output.append(url)

    return output


def choose_page_type(url):
    detected_page_type = detect_start_url_type(url)
    detected_label = page_type_label(detected_page_type)

    print("Link type options:")
    print(f"1. Auto detect [{detected_label}]")
    print("2. Single lesson link")
    print("3. Level link where all the lessons are stored")
    print("4. Pathway link where groups of lessons are stored")

    while True:
        choice = clean(input("Choose link type [1]: ")) or "1"

        if choice == "1":
            return detected_page_type
        if choice == "2":
            return "lesson"
        if choice == "3":
            return "level_link"
        if choice == "4":
            return "pathway_link"

        print("Please enter 1, 2, 3, or 4.")


def build_languagepod_catalog():
    catalog = []

    for site_url in LANGUAGEPOD_SITE_URLS:
        language = get_language_from_domain(site_url)
        catalog.append(
            {
                "language": language,
                "language_safe": safe_key(language),
                "site_url": site_url.rstrip("/"),
                "domain": get_domain(site_url),
            }
        )

    return sorted(catalog, key=lambda item: item["language"].lower())


def languagepod_catalog_lookup():
    catalog = build_languagepod_catalog()
    lookup = {}

    for entry in catalog:
        domain_slug = entry["domain"].split(".")[0]
        keys = {
            entry["language_safe"],
            safe_key(entry["language"].replace(" ", "")),
            safe_key(domain_slug),
            safe_key(re.sub(r"(class101|pod101)$", "", domain_slug, flags=re.IGNORECASE)),
        }

        for key in keys:
            lookup[key] = entry

    return catalog, lookup


def prompt_for_start_mode():
    print("\nChoose how you want to start this scraper:")
    print("1. Type language names from LanguagePod101 and auto-scrape levels 1 to 5")
    print("2. Paste Pod101/Class101 URLs manually")

    while True:
        choice = clean(input("Choose mode [1]: ")) or "1"
        if choice == "1":
            return "auto_languages"
        if choice == "2":
            return "manual_urls"
        print("Please enter 1 or 2.")


def prompt_for_languages_from_catalog():
    catalog, lookup = languagepod_catalog_lookup()

    print("\nLanguagePod101 auto mode")
    print("- Type one or more language names.")
    print("- Separate them with commas, or put one on each line.")
    print("- Press ENTER on a blank line immediately to run all available languages.")
    print("- Press ENTER on a blank line when you are done.\n")

    print("Available languages:")
    print(", ".join(entry["language"] for entry in catalog))

    selected = []
    selected_keys = set()

    while True:
        line = input("\nLanguage(s): ").strip()

        if not line:
            break

        raw_tokens = [clean(part) for part in re.split(r"[,;\n]+", line) if clean(part)]
        if not raw_tokens:
            continue

        for token in raw_tokens:
            token_key = safe_key(token)
            entry = lookup.get(token_key)

            if not entry:
                matches = [
                    item
                    for item in catalog
                    if token_key in item["language_safe"] or item["language_safe"] in token_key
                ]
                if len(matches) == 1:
                    entry = matches[0]

            if not entry:
                print(f"Could not match language: {token}")
                continue

            if entry["language_safe"] in selected_keys:
                print(f"Skipping duplicate language: {entry['language']}")
                continue

            selected.append(dict(entry))
            selected_keys.add(entry["language_safe"])
            print(f"Added language: {entry['language']} -> {entry['site_url']}")

    if not selected:
        print("\nNo specific languages entered. Defaulting to all available languages.")
        return [dict(entry) for entry in catalog]

    return selected


def build_auto_language_bundles(selected_sites):
    bundles = []

    for site in selected_sites:
        bundles.append(
            {
                "language": site["language"],
                "language_safe": site["language_safe"],
                "profile_dir": get_profile_dir(site["language_safe"]),
                "site_url": site["site_url"],
                "domain": site["domain"],
                "levels": [f"Level {number}" for number in TARGET_LEVEL_NUMBERS],
                "jobs": [],
                "auto_discovery": True,
            }
        )

    return bundles


def prompt_for_job_entries():
    print("\nPaste the Pod101/Class101 URLs you want to scrape.")
    print("You can paste one per line, or paste multiple URLs on one line.")
    print("Press ENTER on a blank line when you are done.\n")

    print("Accepted URL types:")
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

        valid_urls = []
        invalid_urls = []

        for url in found_urls:
            if looks_like_pod101_url(url):
                valid_urls.append(url)
            else:
                invalid_urls.append(url)

        for url in valid_urls:
            key = canonical_url_key(url)
            if key in seen_keys:
                print(f"Skipping duplicate URL: {url}")
                continue

            detected_language = get_language_from_domain(url)
            guessed_level = guess_level_from_url(url)

            print("\nURL accepted:")
            print(url)

            page_type = choose_page_type(url)
            language = clean(input(f"Language for this URL [{detected_language}]: ")) or detected_language

            if guessed_level:
                level_prompt = f"Level for this URL [{guessed_level}]: "
            else:
                level_prompt = "Level for this URL: "

            level = clean(input(level_prompt)) or guessed_level or "unspecified"

            collected_entries.append(
                {
                    "url": url,
                    "page_type": page_type,
                    "language": language,
                    "level": level,
                }
            )
            seen_keys.add(key)
            print(
                f"Added job {len(collected_entries)}: "
                f"{language} / {level} / {page_type_label(page_type)} / {url}"
            )

        for url in invalid_urls:
            print(f"Skipping unsupported domain: {url}")

    return collected_entries


def build_jobs(raw_entries):
    jobs = []

    for entry in raw_entries:
        url = clean(entry.get("url"))
        if not url:
            continue

        page_type = clean(entry.get("page_type")) or detect_start_url_type(url)
        language = clean(entry.get("language")) or get_language_from_domain(url)
        language_safe = safe_key(language)
        level = clean(entry.get("level")) or "unspecified"
        if level.lower() != "unspecified" and not is_target_level_text(level):
            print(f"Skipping non-target level for {language}: {level} / {url}")
            continue
        level_safe = safe_key(level)
        job_slug = get_job_slug(url)

        jobs.append(
            {
                "url": url,
                "page_type": page_type,
                "page_type_label": page_type_label(page_type),
                "domain": get_domain(url),
                "language": language,
                "language_safe": language_safe,
                "level": level,
                "level_safe": level_safe,
                "job_slug": job_slug,
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
                "site_url": f"{urlparse(job['url']).scheme}://{urlparse(job['url']).netloc}",
                "domain": job["domain"],
                "levels": set(),
                "jobs": [],
            }

        grouped[language_safe]["levels"].add(job["level"])
        grouped[language_safe]["jobs"].append(job)

    output = []
    for bundle in grouped.values():
        bundle["levels"] = sorted(bundle["levels"])
        bundle["jobs"] = sorted(
            bundle["jobs"],
            key=lambda job: (
                get_level_number_from_text(job["level"]) or 999,
                job["page_type"],
                job["job_label"],
                job["url"],
            ),
        )
        output.append(bundle)

    output.sort(key=lambda bundle: bundle["language_safe"])
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
    finalize_csv(path)


def merge_and_save_csv(existing_path, new_rows):
    old_rows = read_csv_rows(existing_path)
    save_csv(old_rows + new_rows, existing_path)


def get_language_master_csv(language_safe):
    return os.path.join(
        language_safe,
        safe_filename(f"{language_safe}_MASTER_anki", max_len=100) + ".csv",
    )


def make_job_root_dir(job):
    return os.path.join(
        job["language_safe"],
        job["level_safe"],
        page_type_folder(job["page_type"]),
        safe_key(job["job_slug"]),
    )


def get_lesson_id(lesson_url):
    path_parts = [part for part in urlparse(lesson_url).path.split("/") if part]
    if path_parts:
        return safe_key(path_parts[-1])[:40]
    return "lesson"


def get_lesson_folder(root_dir, lesson_number, lesson_title, lesson_id):
    title_part = safe_filename(lesson_title, max_len=LESSON_FOLDER_TITLE_MAX).lower()
    id_part = safe_filename(lesson_id, max_len=35).lower()
    folder_name = f"{lesson_number:03d}_{title_part}__{id_part}"
    return os.path.join(root_dir, folder_name)


def find_existing_lesson_folder(root_dir, lesson_id):
    if not os.path.exists(root_dir):
        return None

    suffix = "__" + safe_filename(lesson_id, max_len=35).lower()

    for name in os.listdir(root_dir):
        path = os.path.join(root_dir, name)
        if os.path.isdir(path) and name.endswith(suffix):
            return path

    return None


def lesson_already_done(root_dir, lesson_id):
    lesson_folder = find_existing_lesson_folder(root_dir, lesson_id)

    if not lesson_folder:
        return False

    for file_name in os.listdir(lesson_folder):
        file_path = os.path.join(lesson_folder, file_name)
        if file_name.endswith("_anki.csv") and os.path.getsize(file_path) > 0:
            return True

    return False


def add_unique_link(raw, base_url, predicate, seen_keys, output):
    if not raw:
        return

    full_url = expand_url(raw, base_url)
    if not full_url or not predicate(full_url):
        return

    key = canonical_url_key(full_url)
    if key not in seen_keys:
        seen_keys.add(key)
        output.append(full_url)


def collect_page_targets(page, base_url):
    scroll_page(page)

    page_html = page.content()
    soup = BeautifulSoup(page_html, "html.parser")

    lesson_links = []
    library_links = []
    seen_lessons = set()
    seen_libraries = set()

    for a in soup.select("a[href]"):
        href = a.get("href")
        add_unique_link(href, base_url, is_lesson_url, seen_lessons, lesson_links)
        add_unique_link(href, base_url, is_library_url, seen_libraries, library_links)

    lesson_patterns = [
        r'"url"\s*:\s*"([^"]*?/lesson/[^"]+)"',
        r"&quot;url&quot;\s*:\s*&quot;([^&]+?/lesson/[^&]+)&quot;",
        r"(\/lesson\/[^\"'<>\s]+?\?lp=\d+)",
        r"(https?://[^\"'<>\s]+?/lesson/[^\"'<>\s]+?\?lp=\d+)",
        r"(\/lesson\/[^\"'<>\s]+)",
        r"(https?://[^\"'<>\s]+?/lesson/[^\"'<>\s]+)",
    ]

    library_patterns = [
        r'"url"\s*:\s*"([^"]*?/lesson-library/[^"]+)"',
        r"&quot;url&quot;\s*:\s*&quot;([^&]+?/lesson-library/[^&]+)&quot;",
        r"(\/lesson-library\/[^\"'<>\s]+)",
        r"(https?://[^\"'<>\s]+?/lesson-library/[^\"'<>\s]+)",
    ]

    for pattern in lesson_patterns:
        for match in re.findall(pattern, page_html):
            add_unique_link(match, base_url, is_lesson_url, seen_lessons, lesson_links)

    for pattern in library_patterns:
        for match in re.findall(pattern, page_html):
            add_unique_link(match, base_url, is_library_url, seen_libraries, library_links)

    base_key = canonical_url_key(base_url)
    library_links = [link for link in library_links if canonical_url_key(link) != base_key]

    return lesson_links, library_links


def get_level_number_from_text(text):
    text = clean(text).lower()
    if not text:
        return None

    if re.search(r"\blevel[\s\-_]*1\b", text) or "absolute-beginner" in text or re.search(r"\ba1\b", text):
        return 1
    if re.search(r"\blevel[\s\-_]*2\b", text) or "lower-beginner" in text:
        return 2
    if re.search(r"\blevel[\s\-_]*3\b", text) or "upper-beginner" in text or re.search(r"\bbeginner\b", text):
        return 3
    if re.search(r"\blevel[\s\-_]*4\b", text) or "lower-intermediate" in text or re.search(r"\bintermediate\b", text):
        return 4
    if re.search(r"\blevel[\s\-_]*5\b", text) or "upper-intermediate" in text or "advanced" in text or re.search(r"\bb2\b|\bc1\b", text):
        return 5

    return None


def is_target_level_text(text):
    return get_level_number_from_text(text) in TARGET_LEVEL_NUMBERS


def build_level_candidate_groups(site_url, language_safe):
    site_url = site_url.rstrip("/")
    language_slug = language_safe.replace("_", "-")

    return {
        1: [
            f"{site_url}/lesson-library/absolute-beginner?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/level-1-{language_slug}?disable_ssr=1",
            f"{site_url}/lesson-library/level-1?disable_ssr=1",
        ],
        2: [
            f"{site_url}/lesson-library/lower-beginner?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/level-2-{language_slug}?disable_ssr=1",
            f"{site_url}/lesson-library/level-2?disable_ssr=1",
        ],
        3: [
            f"{site_url}/lesson-library/upper-beginner?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/beginner?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/level-3-{language_slug}?disable_ssr=1",
            f"{site_url}/lesson-library/level-3?disable_ssr=1",
        ],
        4: [
            f"{site_url}/lesson-library/lower-intermediate?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/intermediate?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/level-4-{language_slug}?disable_ssr=1",
            f"{site_url}/lesson-library/level-4?disable_ssr=1",
        ],
        5: [
            f"{site_url}/lesson-library/upper-intermediate?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/advanced?isrc=content_pathways_browse",
            f"{site_url}/lesson-library/level-5-{language_slug}?disable_ssr=1",
            f"{site_url}/lesson-library/level-5?disable_ssr=1",
        ],
    }


def build_level_job(language_bundle, level_number, url):
    level_label = f"Level {level_number}"
    page_type = "level_link"

    return {
        "url": url,
        "page_type": page_type,
        "page_type_label": page_type_label(page_type),
        "domain": language_bundle["domain"],
        "language": language_bundle["language"],
        "language_safe": language_bundle["language_safe"],
        "level": level_label,
        "level_safe": safe_key(level_label),
        "job_slug": get_job_slug(url),
        "job_label": level_label,
    }


def probe_level_page(page, language_bundle, level_number, candidate_url):
    print(f"\nChecking {language_bundle['language']} {level_number} candidate:")
    print(candidate_url)

    if not safe_goto(page, candidate_url, timeout=90000, sleep_after=2500):
        return None

    title = get_page_title_from_html(page)
    lesson_links, library_links = collect_page_targets(page, candidate_url)

    print(f"Candidate title: {title}")
    print(f"Lesson links found: {len(lesson_links)}")
    print(f"Library links found: {len(library_links)}")

    inferred_level = (
        get_level_number_from_text(candidate_url)
        or get_level_number_from_text(title)
    )

    if inferred_level != level_number:
        return None

    if not lesson_links and not library_links:
        return None

    return build_level_job(language_bundle, level_number, candidate_url)


def discover_level_jobs(page, language_bundle):
    site_url = language_bundle["site_url"].rstrip("/")
    discovered_by_level = {}
    seen_links = set()

    seed_urls = [
        site_url,
        f"{site_url}/welcome",
        f"{site_url}/lesson-library/absolute-beginner?isrc=content_pathways_browse",
    ]

    print("\nDiscovering level pages from the site...")
    for seed_url in dedupe_urls(seed_urls):
        if not safe_goto(page, seed_url, timeout=90000, sleep_after=2500):
            continue

        title = get_page_title_from_html(page)
        _, library_links = collect_page_targets(page, seed_url)

        print(f"Discovery seed: {seed_url}")
        print(f"Seed title: {title}")
        print(f"Library links discovered: {len(library_links)}")

        for link in library_links:
            link_key = canonical_url_key(link)
            if link_key in seen_links:
                continue
            seen_links.add(link_key)

            level_number = get_level_number_from_text(link)
            if level_number and level_number not in discovered_by_level:
                discovered_by_level[level_number] = build_level_job(language_bundle, level_number, link)

        if all(level in discovered_by_level for level in TARGET_LEVEL_NUMBERS):
            break

    candidate_groups = build_level_candidate_groups(site_url, language_bundle["language_safe"])

    for level_number in TARGET_LEVEL_NUMBERS:
        if level_number in discovered_by_level:
            continue

        for candidate_url in dedupe_urls(candidate_groups[level_number]):
            job = probe_level_page(page, language_bundle, level_number, candidate_url)
            if job:
                discovered_by_level[level_number] = job
                break

        if level_number not in discovered_by_level:
            print(f"Could not confirm {language_bundle['language']} Level {level_number}.")

    jobs = [discovered_by_level[level] for level in TARGET_LEVEL_NUMBERS if level in discovered_by_level]
    jobs.sort(key=lambda item: get_level_number_from_text(item["level"]) or 999)

    print("\nAuto-discovery result")
    print("-" * 75)
    for job in jobs:
        print(f"{job['language']} / {job['level']} / {job['url']}")
    print("-" * 75)

    return jobs


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
    raw_rows = 0
    duplicate_audio = 0
    missing_audio = 0
    empty_front = 0

    for tr in soup.select("tr"):
        btn = tr.select_one(".js-lsn3-play-dialogue[data-src]")
        if not btn:
            continue
        raw_rows += 1

        audio_url = get_audio_url(btn, lesson_url)
        if not is_audio(audio_url):
            missing_audio += 1
            continue

        audio_key = canonical_url_key(audio_url)
        if audio_key in seen_audio:
            duplicate_audio += 1
            continue

        speaker_tag = tr.select_one(".lsn3-lesson-dialogue__td--name")
        text_tag = tr.select_one(".lsn3-lesson-dialogue__td--text")

        speaker = clean(speaker_tag.get_text(" ", strip=True)) if speaker_tag else ""
        text = clean(text_tag.get_text(" ", strip=True)) if text_tag else clean(btn.get("data-text"))
        back = clean(btn.get("data-english-text"))

        front = f"{speaker} {text}" if speaker and text and not text.startswith(speaker) else text
        if not front:
            empty_front += 1
            continue

        seen_audio.add(audio_key)
        items.append(
            {
                "audio_url": audio_url,
                "front": front,
                "back": back,
            }
        )

    print(f"  Dialogue scan: rows with play buttons={raw_rows}, accepted={len(items)}")
    if missing_audio:
        print(f"  Dialogue scan: skipped missing or non-audio URLs={missing_audio}")
    if duplicate_audio:
        print(f"  Dialogue scan: skipped duplicate audio URLs={duplicate_audio}")
    if empty_front:
        print(f"  Dialogue scan: skipped empty dialogue text={empty_front}")
    return items


def extract_vocab_items(soup, lesson_url, seen_audio):
    items = []
    raw_rows = 0
    skipped_samples = 0
    missing_audio = 0
    duplicate_audio = 0
    empty_word = 0

    vocab_rows = soup.select(
        "tr:has(.lsn3-lesson-vocabulary__lang), "
        "tr:has(.lesson-vocabulary__lang), "
        "tr:has([class*='vocabulary__lang'])"
    )

    for tr in vocab_rows:
        word_tag = tr.select_one(
            ".lsn3-lesson-vocabulary__lang, "
            ".lesson-vocabulary__lang, "
            "[class*='vocabulary__lang']"
        )
        btn = tr.select_one(".js-lsn3-play-vocabulary[data-src]")

        if not word_tag or not btn:
            continue
        raw_rows += 1

        classes = " ".join(tr.get("class", []))
        if "sample" in classes.lower() or "example" in classes.lower():
            skipped_samples += 1
            continue

        audio_url = get_audio_url(btn, lesson_url)
        if not is_audio(audio_url):
            missing_audio += 1
            continue

        audio_key = canonical_url_key(audio_url)
        if audio_key in seen_audio:
            duplicate_audio += 1
            continue

        meaning_tag = tr.select_one(
            ".lsn3-lesson-vocabulary__definition, "
            ".lesson-vocabulary__definition, "
            "[class*='definition'], "
            "[class*='meaning']"
        )

        word = clean(word_tag.get_text(" ", strip=True))
        meaning = clean(meaning_tag.get_text(" ", strip=True)) if meaning_tag else clean(btn.get("data-english-text"))

        if not word:
            empty_word += 1
            continue

        seen_audio.add(audio_key)
        items.append(
            {
                "audio_url": audio_url,
                "front": word,
                "back": meaning,
                "safe_word": safe_filename(word, max_len=25),
            }
        )

    print(f"  Vocab scan: rows with play buttons={raw_rows}, accepted={len(items)}")
    if skipped_samples:
        print(f"  Vocab scan: skipped sample/example rows={skipped_samples}")
    if missing_audio:
        print(f"  Vocab scan: skipped missing or non-audio URLs={missing_audio}")
    if duplicate_audio:
        print(f"  Vocab scan: skipped duplicate audio URLs={duplicate_audio}")
    if empty_word:
        print(f"  Vocab scan: skipped empty vocab text={empty_word}")
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

    raw_blocks = 0
    missing_audio = 0
    duplicate_audio = 0
    empty_text = 0

    for block in example_blocks:
        btn = block.select_one(".js-lsn3-play-vocabulary[data-src]")
        if not btn:
            continue
        raw_blocks += 1

        audio_url = get_audio_url(btn, lesson_url)
        if not is_audio(audio_url):
            missing_audio += 1
            continue

        audio_key = canonical_url_key(audio_url)
        if audio_key in seen_audio:
            duplicate_audio += 1
            continue

        text = clean(btn.get("data-text")) or clean(block.get_text(" ", strip=True))
        if not text:
            empty_text += 1
            continue

        seen_audio.add(audio_key)
        items.append(
            {
                "audio_url": audio_url,
                "front": text,
                "back": "",
            }
        )

    print(f"  Sentence scan: sample blocks with play buttons={raw_blocks}, accepted={len(items)}")
    if missing_audio:
        print(f"  Sentence scan: skipped missing or non-audio URLs={missing_audio}")
    if duplicate_audio:
        print(f"  Sentence scan: skipped duplicate audio URLs={duplicate_audio}")
    if empty_text:
        print(f"  Sentence scan: skipped empty sentence text={empty_text}")
    return items


def scrape_lesson(context, page, lesson_url, lesson_number, job, root_dir):
    lesson_id = get_lesson_id(lesson_url)

    print("\n" + "=" * 95)
    print(f"DOWNLOADING LESSON {lesson_number:03d}")
    print(f"Language:   {job['language']}")
    print(f"Level:      {job['level']}")
    print(f"Job type:   {job['page_type_label']}")
    print(f"Job label:  {job['job_label']}")
    print(f"Lesson URL: {lesson_url}")
    print("=" * 95)

    if not safe_goto(page, lesson_url):
        print("Could not open lesson. Skipping.")
        return []

    print("\nWaiting for lesson page to finish loading...")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
        print("Lesson page reached network idle.")
    except PlaywrightTimeoutError:
        print("Lesson page did not reach network idle in time. Continuing with current HTML.")

    page.wait_for_timeout(4000)
    print("Finished lesson settle wait.")

    page_html = page.content()
    soup = BeautifulSoup(page_html, "html.parser")

    title = get_page_title_from_html_content(page_html)
    lesson_folder = get_lesson_folder(root_dir, lesson_number, title, lesson_id)
    ensure_folder(lesson_folder)

    print(f"\nLesson title:  {title}")
    print(f"Lesson ID:     {lesson_id}")
    print(f"Saving folder: {abs_path(lesson_folder)}")

    lesson_meta = (
        f"{job['language']} - {job['level']} - {job['page_type_label']} - {job['job_label']} - "
        f"{lesson_number:03d} - {title}"
    )

    rows = []
    seen_audio = set()
    audio_ready_count = 0
    lesson_slug = safe_filename(title, max_len=50).lower()
    debug_counts = collect_lesson_debug_counts(soup)

    print("\nRAW PAGE COUNTS")
    print("-" * 70)
    print(f"Page title seen by browser: {clean(page.title())}")
    print(f"Dialogue play buttons:      {debug_counts['dialogue_buttons']}")
    print(f"Dialogue name cells:        {debug_counts['dialogue_name_cells']}")
    print(f"Dialogue text cells:        {debug_counts['dialogue_text_cells']}")
    print(f"Vocab play buttons:         {debug_counts['vocab_buttons']}")
    print(f"Vocab language cells:       {debug_counts['vocab_lang_cells']}")
    print(f"Sample/example blocks:      {debug_counts['sample_blocks']}")
    print("-" * 70)

    print("\nScanning dialogue rows...")
    dialogue_items = extract_dialogue_items(soup, lesson_url, seen_audio)
    print("Scanning vocabulary rows...")
    vocab_items = extract_vocab_items(soup, lesson_url, seen_audio)
    print("Scanning sentence/example blocks...")
    sentence_items = extract_sentence_items(soup, lesson_url, seen_audio)

    print("\nLESSON CONTENT FOUND")
    print("-" * 70)
    print(f"Dialogue items:  {len(dialogue_items)}")
    print(f"Vocab items:     {len(vocab_items)}")
    print(f"Sentence items:  {len(sentence_items)}")
    print(f"Potential cards: {len(dialogue_items) + len(vocab_items) + len(sentence_items)}")
    print("-" * 70)

    print("\nDownloading dialogue audio and building dialogue cards...")
    dialogue_card_count = 0
    for index, item in enumerate(dialogue_items, 1):
        filename = (
            f"{job['language_safe']}_{lesson_number:03d}_{lesson_id}_{lesson_slug}_dlg_{index:02d}.mp3"
        )
        filename = safe_filename(filename.replace(".mp3", ""), max_len=FILE_NAME_MAX) + ".mp3"
        path = os.path.join(lesson_folder, filename)

        print(f"  [Dialogue {index:02d}/{len(dialogue_items)}] {preview(item['front'])}")
        print(f"    Audio URL: {item['audio_url']}")
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
            print(f"    Dialogue cards so far: {dialogue_card_count}")
            print(f"    Total lesson cards so far: {len(rows)}")

    print("\nDownloading vocabulary audio and building vocab cards...")
    vocab_card_count = 0
    for index, item in enumerate(vocab_items, 1):
        filename = (
            f"{job['language_safe']}_{lesson_number:03d}_{lesson_id}_{lesson_slug}_vw_{index:02d}_{item['safe_word']}.mp3"
        )
        filename = safe_filename(filename.replace(".mp3", ""), max_len=FILE_NAME_MAX) + ".mp3"
        path = os.path.join(lesson_folder, filename)

        print(f"  [Vocab {index:02d}/{len(vocab_items)}] {preview(item['front'])}")
        print(f"    Audio URL: {item['audio_url']}")
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
            print(f"    Vocab cards so far: {vocab_card_count}")
            print(f"    Total lesson cards so far: {len(rows)}")

    print("\nDownloading sentence audio and building sentence cards...")
    sentence_card_count = 0
    for index, item in enumerate(sentence_items, 1):
        filename = (
            f"{job['language_safe']}_{lesson_number:03d}_{lesson_id}_{lesson_slug}_vs_{index:02d}.mp3"
        )
        filename = safe_filename(filename.replace(".mp3", ""), max_len=FILE_NAME_MAX) + ".mp3"
        path = os.path.join(lesson_folder, filename)

        print(f"  [Sentence {index:02d}/{len(sentence_items)}] {preview(item['front'])}")
        print(f"    Audio URL: {item['audio_url']}")
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
            print(f"    Sentence cards so far: {sentence_card_count}")
            print(f"    Total lesson cards so far: {len(rows)}")

    csv_filename = (
        f"{job['language_safe']}_{lesson_number:03d}_{lesson_id}_{lesson_slug}_anki.csv"
    )
    csv_filename = safe_filename(csv_filename.replace(".csv", ""), max_len=FILE_NAME_MAX) + ".csv"
    csv_path = os.path.join(lesson_folder, csv_filename)
    print("\nWriting lesson CSV...")
    save_csv(rows, csv_path)
    print("Finished writing lesson CSV and running cleanup columns.")

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


def crawl_library_for_lessons(page, start_url, job):
    queue_items = deque([(start_url, 0)])
    queued_keys = {canonical_url_key(start_url)}
    seen_library_keys = set()
    seen_lesson_keys = set()
    lesson_links = []
    library_pages_scanned = 0

    while queue_items:
        current_url, depth = queue_items.popleft()
        current_key = canonical_url_key(current_url)

        if current_key in seen_library_keys:
            continue

        if library_pages_scanned >= MAX_LIBRARY_PAGES:
            print("\nReached MAX_LIBRARY_PAGES safety limit.")
            break

        library_pages_scanned += 1

        print("\n" + "#" * 95)
        print(f"SCANNING LIBRARY PAGE {library_pages_scanned}")
        print(f"Language: {job['language']}")
        print(f"Level:    {job['level']}")
        print(f"Type:     {job['page_type_label']}")
        print(f"Depth:    {depth}/{MAX_LIBRARY_DEPTH}")
        print(f"URL:      {current_url}")
        print("#" * 95)

        if not safe_goto(page, current_url):
            print("Could not open library page. Skipping this page.")
            seen_library_keys.add(current_key)
            continue

        seen_library_keys.add(current_key)
        title = get_page_title_from_html(page)
        discovered_lessons, discovered_libraries = collect_page_targets(page, current_url)

        new_lessons = 0
        for link in discovered_lessons:
            lesson_key = canonical_url_key(link)
            if lesson_key not in seen_lesson_keys:
                seen_lesson_keys.add(lesson_key)
                lesson_links.append(link)
                new_lessons += 1

        new_library_pages = 0
        if depth < MAX_LIBRARY_DEPTH:
            for link in discovered_libraries:
                library_key = canonical_url_key(link)
                if library_key in seen_library_keys or library_key in queued_keys:
                    continue
                queue_items.append((link, depth + 1))
                queued_keys.add(library_key)
                new_library_pages += 1

        print("\nLIBRARY PAGE SUMMARY")
        print("-" * 75)
        print(f"Title:                  {title}")
        print(f"New lesson links found: {new_lessons}")
        print(f"Total lesson links:     {len(lesson_links)}")
        print(f"New nested libraries:   {new_library_pages}")
        print(f"Queue remaining:        {len(queue_items)}")
        print("-" * 75)

    return lesson_links, library_pages_scanned


def process_direct_lesson_job(context, page, job, job_index, total_jobs):
    root_dir = make_job_root_dir(job)
    ensure_folder(root_dir)

    print("\n" + "=" * 100)
    print(f"STARTING JOB {job_index}/{total_jobs}")
    print(f"Language:  {job['language']}")
    print(f"Level:     {job['level']}")
    print(f"Job type:  {job['page_type_label']}")
    print(f"Job label: {job['job_label']}")
    print(f"Folder:    {abs_path(root_dir)}")
    print(f"URL:       {job['url']}")
    print("=" * 100)

    lesson_id = get_lesson_id(job["url"])
    skipped = 0

    if lesson_already_done(root_dir, lesson_id):
        existing_folder = find_existing_lesson_folder(root_dir, lesson_id)
        print("\nSkipping direct lesson; CSV already exists.")
        print(f"Existing folder: {abs_path(existing_folder)}")
        skipped = 1
        rows = []
    else:
        rows = scrape_lesson(
            context=context,
            page=page,
            lesson_url=job["url"],
            lesson_number=1,
            job=job,
            root_dir=root_dir,
        )

    job_master_csv = os.path.join(
        root_dir,
        safe_filename(f"{job['language_safe']}_{job['job_slug']}_MASTER_anki", max_len=FILE_NAME_MAX) + ".csv",
    )
    merge_and_save_csv(job_master_csv, rows)

    language_master_csv = get_language_master_csv(job["language_safe"])
    merge_and_save_csv(language_master_csv, rows)

    print("\nJOB SUMMARY")
    print("=" * 80)
    print(f"Job:                 {job_index}/{total_jobs}")
    print(f"Language:            {job['language']}")
    print(f"Level:               {job['level']}")
    print(f"Job type:            {job['page_type_label']}")
    print(f"New cards this job:  {len(rows)}")
    print(f"Skipped lessons:     {skipped}")
    print(f"Job master CSV:      {abs_path(job_master_csv)}")
    print(f"Language master CSV: {abs_path(language_master_csv)}")
    print("=" * 80)

    return rows, skipped


def process_library_job(context, page, job, job_index, total_jobs):
    root_dir = make_job_root_dir(job)
    ensure_folder(root_dir)

    print("\n" + "=" * 100)
    print(f"STARTING JOB {job_index}/{total_jobs}")
    print(f"Language:  {job['language']}")
    print(f"Level:     {job['level']}")
    print(f"Job type:  {job['page_type_label']}")
    print(f"Job label: {job['job_label']}")
    print(f"Folder:    {abs_path(root_dir)}")
    print(f"URL:       {job['url']}")
    print("=" * 100)

    lesson_links, library_pages_scanned = crawl_library_for_lessons(page, job["url"], job)

    if not lesson_links:
        print("\nNo direct lesson links were found under this library job.")
        return [], 0

    print(f"\nFound {len(lesson_links)} lesson links for this job.\n")
    for index, link in enumerate(lesson_links, 1):
        print(f"{index:03d}. {link}")

    all_rows = []
    skipped_count = 0

    for lesson_number, lesson_url in enumerate(lesson_links, 1):
        lesson_id = get_lesson_id(lesson_url)

        if lesson_already_done(root_dir, lesson_id):
            existing_folder = find_existing_lesson_folder(root_dir, lesson_id)
            print(f"\nSkipping lesson {lesson_number:03d}; CSV already exists.")
            print(f"Lesson URL:      {lesson_url}")
            print(f"Existing folder: {abs_path(existing_folder)}")
            skipped_count += 1
            continue

        rows = scrape_lesson(
            context=context,
            page=page,
            lesson_url=lesson_url,
            lesson_number=lesson_number,
            job=job,
            root_dir=root_dir,
        )
        all_rows.extend(rows)

        print("\nJOB RUNNING TOTAL")
        print("-" * 70)
        print(f"Finished lesson:    {lesson_number}/{len(lesson_links)}")
        print(f"Cards this lesson:  {len(rows)}")
        print(f"Cards this job:     {len(all_rows)}")
        print(f"Skipped lessons:    {skipped_count}")
        print(f"Library pages seen: {library_pages_scanned}")
        print("-" * 70)

        if lesson_number < len(lesson_links):
            print(f"\nResting {REST_BETWEEN_LESSONS} seconds before next lesson...\n")
            time.sleep(REST_BETWEEN_LESSONS)

    job_master_csv = os.path.join(
        root_dir,
        safe_filename(f"{job['language_safe']}_{job['job_slug']}_MASTER_anki", max_len=FILE_NAME_MAX) + ".csv",
    )
    merge_and_save_csv(job_master_csv, all_rows)

    language_master_csv = get_language_master_csv(job["language_safe"])
    merge_and_save_csv(language_master_csv, all_rows)

    print("\nJOB SUMMARY")
    print("=" * 80)
    print(f"Job:                 {job_index}/{total_jobs}")
    print(f"Language:            {job['language']}")
    print(f"Level:               {job['level']}")
    print(f"Job type:            {job['page_type_label']}")
    print(f"Library pages seen:  {library_pages_scanned}")
    print(f"Lessons found:       {len(lesson_links)}")
    print(f"Lessons skipped:     {skipped_count}")
    print(f"New cards this job:  {len(all_rows)}")
    print(f"Job master CSV:      {abs_path(job_master_csv)}")
    print(f"Language master CSV: {abs_path(language_master_csv)}")
    print("=" * 80)

    return all_rows, skipped_count


def process_job(context, page, job, job_index, total_jobs):
    if job["page_type"] == "lesson":
        return process_direct_lesson_job(context, page, job, job_index, total_jobs)

    if job["page_type"] in {"level_link", "pathway_link"}:
        return process_library_job(context, page, job, job_index, total_jobs)

    print("\nSkipping unsupported URL type:")
    print(job["url"])
    return [], 0


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

    if not domain_to_url and language_bundle.get("site_url"):
        domain_to_url[language_bundle["domain"]] = language_bundle["site_url"]
        domain_to_url[f"{language_bundle['domain']}-welcome"] = language_bundle["site_url"].rstrip("/") + "/welcome"

    for domain, url in sorted(domain_to_url.items()):
        page = context.new_page()
        safe_goto(page, url, timeout=90000, sleep_after=3000)
        print(f"Opened login tab for {language_bundle['language']}: {domain}")

def first_available_locator(page, selectors):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def click_first_available(page, selectors, timeout=5000):
    locator = first_available_locator(page, selectors)
    if not locator:
        return False

    try:
        locator.click(timeout=timeout)
        page.wait_for_timeout(2000)
        return True
    except Exception:
        return False


def fill_first_available(page, selectors, value):
    locator = first_available_locator(page, selectors)
    if not locator:
        return False

    try:
        locator.fill("")
        locator.fill(value)
        return True
    except Exception:
        return False


def page_has_login_form(page):
    email_selectors = [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
    ]
    password_selectors = [
        "input[type='password']",
        "input[name*='password' i]",
        "input[id*='password' i]",
        "input[placeholder*='password' i]",
    ]
    return bool(first_available_locator(page, email_selectors)) and bool(first_available_locator(page, password_selectors))


def page_looks_logged_in(page):
    positive_selectors = [
        "a:has-text('Logout')",
        "a:has-text('Log Out')",
        "button:has-text('Logout')",
        "button:has-text('Log Out')",
        "[href*='logout']",
        "[href*='member/logout']",
    ]
    return bool(first_available_locator(page, positive_selectors))


def submit_login_form(page):
    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Log In')",
        "button:has-text('Login')",
        "button:has-text('Sign In')",
        "a:has-text('Log In')",
        "a:has-text('Login')",
        "a:has-text('Sign In')",
    ]

    if click_first_available(page, submit_selectors, timeout=8000):
        return True

    password_locator = first_available_locator(page, ["input[type='password']"])
    if password_locator:
        try:
            password_locator.press("Enter")
            page.wait_for_timeout(2000)
            return True
        except Exception:
            return False

    return False


def ensure_logged_in(page, language_bundle):
    site_url = language_bundle["site_url"].rstrip("/")
    login_urls = [
        f"{site_url}/login",
        f"{site_url}/member/login",
        f"{site_url}/welcome",
        site_url,
    ]
    login_trigger_selectors = [
        "a:has-text('Log In')",
        "a:has-text('Login')",
        "a:has-text('Sign In')",
        "button:has-text('Log In')",
        "button:has-text('Login')",
        "button:has-text('Sign In')",
        "[href*='login']",
        "[href*='signin']",
    ]
    email_selectors = [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
    ]
    password_selectors = [
        "input[type='password']",
        "input[name*='password' i]",
        "input[id*='password' i]",
        "input[placeholder*='password' i]",
    ]

    page.goto(site_url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2500)

    if page_looks_logged_in(page):
        print(f"Already logged in for {language_bundle['language']}.")
        return

    login_form_found = page_has_login_form(page)
    if not login_form_found:
        click_first_available(page, login_trigger_selectors, timeout=8000)
        login_form_found = page_has_login_form(page)

    if not login_form_found:
        for login_url in login_urls:
            if not safe_goto(page, login_url, timeout=90000, sleep_after=2500):
                continue
            if page_has_login_form(page):
                login_form_found = True
                break

    if not login_form_found:
        raise RuntimeError(f"Could not find login form for {language_bundle['language']} at {site_url}")

    if not fill_first_available(page, email_selectors, LOGIN_EMAIL):
        raise RuntimeError(f"Could not fill email for {language_bundle['language']}")

    if not fill_first_available(page, password_selectors, LOGIN_PASSWORD):
        raise RuntimeError(f"Could not fill password for {language_bundle['language']}")

    if not submit_login_form(page):
        raise RuntimeError(f"Could not submit login form for {language_bundle['language']}")

    page.wait_for_timeout(5000)
    safe_goto(page, f"{site_url}/welcome", timeout=90000, sleep_after=3000)

    if page_has_login_form(page) and not page_looks_logged_in(page):
        raise RuntimeError(f"Login appears to have failed for {language_bundle['language']}")

    print(f"Login completed for {language_bundle['language']}.")


def prepare_language_profiles(language_bundles):
    with sync_playwright() as playwright:
        for index, bundle in enumerate(language_bundles, 1):
            print("\n" + "=" * 95)
            print(f"PREPARING LOGIN {index}/{len(language_bundles)}")
            print(f"Language: {bundle['language']}")
            print(f"Site:     {bundle.get('site_url', bundle.get('domain', ''))}")
            print(f"Profile:  {bundle['profile_dir']}")
            print("=" * 95)

            context = playwright.chromium.launch_persistent_context(
                user_data_dir=bundle["profile_dir"],
                headless=False,
            )
            try:
                page = context.new_page()
                ensure_logged_in(page, bundle)
            finally:
                try:
                    context.close()
                except Exception:
                    pass


def run_language_worker(language_bundle):
    restore_print = install_print_prefix(language_bundle["language"])
    context = None

    total_new_cards = 0
    total_skipped = 0
    job_summaries = []

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=language_bundle["profile_dir"],
                headless=False,
            )

            page = context.new_page()
            if language_bundle.get("auto_discovery"):
                language_bundle["jobs"] = discover_level_jobs(page, language_bundle)
                language_bundle["levels"] = [job["level"] for job in language_bundle["jobs"]]

                if not language_bundle["jobs"]:
                    raise RuntimeError(
                        f"No usable level pages were discovered for {language_bundle['language']}."
                    )

            for job_index, job in enumerate(language_bundle["jobs"], 1):
                rows, skipped = process_job(
                    context=context,
                    page=page,
                    job=job,
                    job_index=job_index,
                    total_jobs=len(language_bundle["jobs"]),
                )

                total_new_cards += len(rows)
                total_skipped += skipped

                job_summaries.append(
                    {
                        "language": job["language"],
                        "level": job["level"],
                        "type": job["page_type_label"],
                        "label": job["job_label"],
                        "cards": len(rows),
                        "skipped": skipped,
                    }
                )

                print("\nLANGUAGE RUNNING TOTAL")
                print("-" * 75)
                print(f"Finished job:       {job_index}/{len(language_bundle['jobs'])}")
                print(f"Cards this job:     {len(rows)}")
                print(f"Skipped this job:   {skipped}")
                print(f"Language new cards: {total_new_cards}")
                print(f"Language skipped:   {total_skipped}")
                print("-" * 75)

                if job_index < len(language_bundle["jobs"]):
                    print(f"\nResting {REST_BETWEEN_JOBS} seconds before next job...\n")
                    time.sleep(REST_BETWEEN_JOBS)

            print("\nLANGUAGE SUMMARY")
            print("=" * 85)
            print(f"Language:              {language_bundle['language']}")
            print(f"Jobs completed:        {len(language_bundle['jobs'])}")
            print(f"Total new cards:       {total_new_cards}")
            print(f"Total skipped lessons: {total_skipped}")
            print("=" * 85)

        return {
            "language": language_bundle["language"],
            "language_safe": language_bundle["language_safe"],
            "job_summaries": job_summaries,
            "new_cards": total_new_cards,
            "skipped": total_skipped,
            "status": "ok",
        }
    except Exception as e:
        return {
            "language": language_bundle["language"],
            "language_safe": language_bundle["language_safe"],
            "job_summaries": job_summaries,
            "new_cards": total_new_cards,
            "skipped": total_skipped,
            "status": "error",
            "error": str(e),
        }
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        builtins.print = restore_print


def run_languages_sequential(language_bundles):
    results = []

    for bundle_index, bundle in enumerate(language_bundles, 1):
        print("\nStarting language:")
        print(f"- {bundle_index}/{len(language_bundles)}: {bundle['language']}")
        results.append(run_language_worker(bundle))

    return results


def collect_all_language_rows(language_bundles):
    combined_rows = []

    for bundle in language_bundles:
        combined_rows.extend(read_csv_rows(get_language_master_csv(bundle["language_safe"])))

    return combined_rows


def main():
    start_mode = prompt_for_start_mode()

    if start_mode == "auto_languages":
        selected_sites = prompt_for_languages_from_catalog()

        language_bundles = build_auto_language_bundles(selected_sites)

        print("\nLanguages queued for auto-discovery:")
        for index, bundle in enumerate(language_bundles, 1):
            print(
                f"{index}. {bundle['language']} / Levels 1 to 5 / "
                f"{bundle['site_url']}"
            )

        print("\nUnique sites that will open for login:")
        for bundle in language_bundles:
            print("-", bundle["domain"])

        print("\nLanguages queued to run one by one:")
        for bundle in language_bundles:
            print(
                f"- {bundle['language']}: auto-discover levels 1 to 5, "
                f"profile {bundle['profile_dir']}"
            )
    else:
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
                f"{index}. {job['language']} / {job['level']} / {job['page_type_label']} / "
                f"{job['job_label']} / {job['url']}"
            )

        print("\nUnique sites that will open for login:")
        for domain in sorted(set(job["domain"] for job in jobs)):
            print("-", domain)

        language_bundles = group_jobs_by_language(jobs)

        print("\nLanguages queued to run one by one:")
        for bundle in language_bundles:
            print(
                f"- {bundle['language']}: {len(bundle['jobs'])} jobs, "
                f"levels {', '.join(bundle['levels'])}, profile {bundle['profile_dir']}"
            )

    print("\nSequential mode: one language at a time.")
    print("Each language keeps its own browser profile.")
    print("The script logs into every language site first, then scrapes language by language.")
    print("For each language, it only targets Levels 1 through 5 before moving to the next language.")
    print("Existing lesson folders and CSVs are checked so the run can resume where it left off.")

    try:
        prepare_language_profiles(language_bundles)
        worker_results = run_languages_sequential(language_bundles)

        grand_rows = collect_all_language_rows(language_bundles)
        grand_skipped = sum(result.get("skipped", 0) for result in worker_results)

        all_master_csv = "ALL_LANGUAGES_ALL_JOBS_MASTER_anki.csv"
        save_csv(grand_rows, all_master_csv)

        print("\nFINAL SUMMARY")
        print("=" * 95)

        summary_index = 1
        for result in worker_results:
            for summary in result.get("job_summaries", []):
                print(
                    f"{summary_index:02d}. {summary['language']} / {summary['level']} / "
                    f"{summary['type']} / {summary['label']} "
                    f"- Cards: {summary['cards']} - Skipped: {summary['skipped']}"
                )
                summary_index += 1

            if result.get("status") != "ok":
                print(
                    f"{summary_index:02d}. {result['language']} / Worker Error / "
                    f"{result.get('error', 'Unknown error')}"
                )
                summary_index += 1

        print("-" * 95)
        print("Combined master CSV:", abs_path(all_master_csv))
        print("Total new cards:", len(grand_rows))
        print("Total skipped lessons:", grand_skipped)
        print("=" * 95)
    except Exception as e:
        print("\nSCRIPT ERROR:")
        print(e)


if __name__ == "__main__":
    main()
