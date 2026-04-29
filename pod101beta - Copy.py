import builtins
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
REST_BETWEEN_JOBS = 20

LESSON_FOLDER_TITLE_MAX = 45
FILE_NAME_MAX = 110

MAX_LIBRARY_DEPTH = 6
MAX_LIBRARY_PAGES = 500

# 0 means "run all languages in parallel".
MAX_PARALLEL_LANGUAGES = 0

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
                "levels": set(),
                "jobs": [],
            }

        grouped[language_safe]["levels"].add(job["level"])
        grouped[language_safe]["jobs"].append(job)

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

    for tr in soup.select("tr"):
        word_tag = tr.select_one(
            ".lsn3-lesson-vocabulary__lang, "
            ".lesson-vocabulary__lang, "
            "[class*='vocabulary__lang']"
        )
        btn = tr.select_one(".js-lsn3-play-vocabulary[data-src], .js-lsn3-play-vocabulary")

        if not word_tag or not btn:
            continue

        classes = " ".join(tr.get("class", []))
        if "sample" in classes.lower() or "example" in classes.lower():
            continue

        audio_url = get_audio_url(btn, lesson_url)
        if not is_audio(audio_url):
            continue

        audio_key = canonical_url_key(audio_url)
        if audio_key in seen_audio:
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

    print("\nDownloading dialogue audio and building dialogue cards...")
    dialogue_card_count = 0
    for index, item in enumerate(dialogue_items, 1):
        filename = (
            f"{job['language_safe']}_{lesson_number:03d}_{lesson_id}_{lesson_slug}_dlg_{index:02d}.mp3"
        )
        filename = safe_filename(filename.replace(".mp3", ""), max_len=FILE_NAME_MAX) + ".mp3"
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


def run_language_worker(language_bundle, control_queue, start_event, result_queue):
    restore_print = install_print_prefix(language_bundle["language"])
    context = None
    login_signal_sent = False

    total_new_cards = 0
    total_skipped = 0
    job_summaries = []

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

        result_queue.put(
            {
                "language": language_bundle["language"],
                "language_safe": language_bundle["language_safe"],
                "job_summaries": job_summaries,
                "new_cards": total_new_cards,
                "skipped": total_skipped,
                "status": "ok",
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
        builtins.print = restore_print


def drain_result_queue(result_queue):
    results = []

    while True:
        try:
            results.append(result_queue.get_nowait())
        except queue.Empty:
            break

    return results


def run_languages_parallel(language_bundles):
    if not language_bundles:
        return []

    ctx = multiprocessing.get_context("spawn")
    control_queue = ctx.Queue()
    result_queue = ctx.Queue()
    results = []

    parallel_limit = MAX_PARALLEL_LANGUAGES or len(language_bundles)
    parallel_limit = max(1, min(parallel_limit, len(language_bundles)))

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
            print("\nThese login windows are the same ones that will do the downloading:")
            for language in ready_languages:
                print(f"- {language}")
            print("Log in to them now. They will stay open and continue from the same session.")
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
                    "status": "error",
                    "error": "No worker result returned",
                },
            )
        )

    return ordered_results


def collect_all_language_rows(language_bundles):
    combined_rows = []

    for bundle in language_bundles:
        combined_rows.extend(read_csv_rows(get_language_master_csv(bundle["language_safe"])))

    return combined_rows


def main():
    start_mode = prompt_for_start_mode()

    if start_mode == "auto_languages":
        selected_sites = prompt_for_languages_from_catalog()

        if not selected_sites:
            print("No languages were entered. Exiting.")
            return

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

        print("\nLanguage workers that will run in parallel:")
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

        print("\nLanguage workers that will run in parallel:")
        for bundle in language_bundles:
            print(
                f"- {bundle['language']}: {len(bundle['jobs'])} jobs, "
                f"levels {', '.join(bundle['levels'])}, profile {bundle['profile_dir']}"
            )

    print("\nParallel mode: one worker per language.")
    print("Each language keeps its own browser profile and downloads independently.")
    print("Each worker rests between lessons so the same site is not hit too fast.")
    print("Login now happens inside the same browser session that will do the downloads.")

    try:
        worker_results = run_languages_parallel(language_bundles)

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
    multiprocessing.freeze_support()
    main()
