import html
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


level_url = input("Paste level page URL: ").strip()
LANGUAGE = input("Language or site label, e.g. Chinese, Korean, Japanese: ").strip()
LEVEL = input("Level, e.g. L1, L2, Beginner, Level 4: ").strip()

language_safe = re.sub(r"[^a-zA-Z0-9]+", "_", LANGUAGE).strip("_").lower()
level_safe = re.sub(r"[^a-zA-Z0-9]+", "_", LEVEL).strip("_").lower()
site_host = urlparse(level_url).netloc.lower()
site_safe = re.sub(r"[^a-zA-Z0-9]+", "_", site_host).strip("_").lower() or "languagepod101"

ROOT_DIR = f"{language_safe}_{level_safe}_lesson_audio"
os.makedirs(ROOT_DIR, exist_ok=True)


def clean(text):
    if not text:
        return ""
    text = html.unescape(str(text))
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(text, max_len=120):
    text = clean(text)
    text = re.sub(r'[<>:"/\\|?*]+', "", text)
    text = text.replace("'", "").replace("’", "")
    text = text.replace("...", "").replace("…", "")
    text = re.sub(
        r"[^\w\uAC00-\uD7A3\u3041-\u3093\u30A1-\u30F3\u4E00-\u9FFF\u0430-\u044F\u0410-\u042F\u0451\u0401\u00E0-\u1EF9\u00C0-\u1EF8]+",
        "_",
        text,
        flags=re.UNICODE,
    )
    text = re.sub(r"_+", "_", text)
    text = text.strip("._- ")
    return text[:max_len].rstrip("._- ") or "lesson"


def ensure_parent_folder(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)


def parse_json_attr(raw):
    raw = clean(raw)
    if not raw:
        return None
    try:
        return json.loads(html.unescape(raw))
    except Exception:
        return None


def collect_links_from_json(data, add_link):
    if isinstance(data, list):
        for item in data:
            collect_links_from_json(item, add_link)
        return

    if not isinstance(data, dict):
        return

    entity_type = str(data.get("entityType", "")).lower()
    has_lesson_hint = (
        entity_type == "lesson"
        or "lessonId" in data
        or "lesson_id" in data
        or "lesson_url" in data
    )

    for key in ("url", "lesson_url", "path_url"):
        value = data.get(key)
        if has_lesson_hint or key == "lesson_url":
            add_link(value)

    for value in data.values():
        if isinstance(value, (dict, list)):
            collect_links_from_json(value, add_link)


def scroll_page(page):
    for _ in range(18):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        except Exception:
            page.wait_for_timeout(3000)


def collect_lesson_links(page, base_url):
    scroll_page(page)

    page_html = page.content()
    soup = BeautifulSoup(page_html, "html.parser")

    links = []
    seen = set()

    def add_link(raw):
        if not raw:
            return

        raw = html.unescape(raw).replace("\\/", "/").replace("&amp;", "&")
        full_url = urljoin(base_url, raw).split("#")[0]

        if "/lesson/" not in urlparse(full_url).path:
            return

        if full_url not in seen:
            seen.add(full_url)
            links.append(full_url)

    for tag in soup.select("[data-collection-entries], [data-collection-entities], [data-user-collection], [data-collection-info], [data-user], [data-current-entity-index]"):
        for attr_name, attr_value in tag.attrs.items():
            if not attr_name.startswith("data-"):
                continue
            parsed = parse_json_attr(attr_value)
            if parsed is not None:
                collect_links_from_json(parsed, add_link)

    for a in soup.select("a[href]"):
        add_link(a.get("href"))

    patterns = [
        r'"url"\s*:\s*"([^"]*?/lesson/[^"]+)"',
        r"&quot;url&quot;\s*:\s*&quot;([^&]+?/lesson/[^&]+)&quot;",
        r"(\/lesson\/[^\"'<>\s]+?\?lp=\d+)",
        r"(https?://[^\"'<>\s]+?/lesson/[^\"'<>\s]+?\?lp=\d+)",
    ]

    for pattern in patterns:
        for match in re.findall(pattern, page_html):
            add_link(match)

    return links


def get_title(soup):
    h1 = soup.find("h1")
    if h1:
        return clean(h1.get_text())
    title_tag = soup.find("title")
    if title_tag:
        return clean(re.sub(r"\s*-\s*.*101.*$", "", title_tag.get_text()))
    return "lesson"


def extract_audio_href(raw, lesson_url):
    value = clean(raw)
    if value and value.lower().endswith((".mp3", ".m4a", ".wav", ".ogg")):
        return urljoin(lesson_url, value)
    return ""


def is_main_lesson_audio_url(audio_url):
    path = urlparse(audio_url).path.lower()
    blocked_markers = (
        "_dialog",
        "_review",
        "_vocabulary",
        "_vocab",
        "transcript_",
    )
    return not any(marker in path for marker in blocked_markers)


def extract_main_audio_href(raw, lesson_url):
    match = extract_audio_href(raw, lesson_url)
    if match and is_main_lesson_audio_url(match):
        return match
    return ""


def get_main_lesson_audio_url_from_html(soup, lesson_url):
    button = soup.select_one("button.js-lsn3-play-lesson-audio[data-url]")
    if button:
        match = extract_main_audio_href(button.get("data-url"), lesson_url)
        if match:
            return match

    audio = soup.select_one(".lsn3-hidden--main-audio audio[data-trackurl]")
    if audio:
        match = extract_main_audio_href(audio.get("data-trackurl"), lesson_url)
        if match:
            return match

    source = soup.select_one(".lsn3-hidden--main-audio audio source[src]")
    if source:
        match = extract_main_audio_href(source.get("src"), lesson_url)
        if match:
            return match

    for link in soup.select("#download-center a[href], a[download][href], a[href]"):
        link_text = clean(link.get_text()).lower()
        title_text = clean(link.get("title")).lower()
        aria_text = clean(link.get("aria-label")).lower()
        if "lesson audio" in {link_text, title_text, aria_text} or "lesson audio" in f"{link_text} {title_text} {aria_text}":
            match = extract_main_audio_href(link.get("href"), lesson_url)
            if match:
                return match

    for audio in soup.select("audio[data-trackurl]"):
        container = audio.find_parent(class_="lsn3-lesson-audio")
        if not container:
            continue
        if "lsn3-hidden--main-audio" in (container.get("class") or []):
            match = extract_main_audio_href(audio.get("data-trackurl"), lesson_url)
            if match:
                return match

    html_content = str(soup)
    patterns = [
        r'class="js-lsn3-play-lesson-audio"[^>]*data-url="([^"]+)"',
        r'data-title="[^"]*Lesson Audio[^"]*"[^>]*data-url="([^"]+)"',
        r'lsn3-hidden--main-audio.*?data-trackurl="([^"]+)"',
        r'<a[^>]+href="([^"]+)"[^>]*>\s*Lesson Audio\s*</a>',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, html_content, flags=re.IGNORECASE | re.DOTALL)
        for match in matches:
            match = extract_main_audio_href(match, lesson_url)
            if match:
                return match

    return ""


def get_main_lesson_audio_url(page, soup, lesson_url):
    match = get_main_lesson_audio_url_from_html(soup, lesson_url)
    if match:
        return match

    selectors = [
        "button.js-lsn3-play-lesson-audio[data-url]",
        ".lsn3-hidden--main-audio audio[data-trackurl]",
        ".lsn3-hidden--main-audio source[src]",
        "#download-center a[href]",
    ]

    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()

        for index in range(count):
            node = locator.nth(index)
            if selector == "#download-center a[href]":
                combined_text = " ".join(
                    [
                        clean(node.inner_text()),
                        clean(node.get_attribute("title")),
                        clean(node.get_attribute("aria-label")),
                    ]
                ).lower()
                if "lesson audio" not in combined_text:
                    continue
            for attr in ("data-url", "data-trackurl", "src", "href"):
                match = extract_main_audio_href(node.get_attribute(attr), lesson_url)
                if match:
                    return match

    return ""


def build_output_path(lesson_number, title, audio_url):
    ext = os.path.splitext(urlparse(audio_url).path)[1].lower()
    if ext not in {".mp3", ".m4a", ".wav", ".ogg"}:
        ext = ".mp3"
    filename = f"{lesson_number:03d} {safe_filename(title, max_len=100)}{ext}"
    return os.path.join(ROOT_DIR, filename)


def download(context, url, path):
    try:
        ensure_parent_folder(path)

        if os.path.exists(path) and os.path.getsize(path) > 0:
            print("Existing audio:", os.path.basename(path))
            return True

        response = context.request.get(url)
        if response.status != 200:
            print("Failed:", url)
            print("HTTP:", response.status)
            return False

        with open(path, "wb") as handle:
            handle.write(response.body())

        print("Saved:", os.path.basename(path))
        return True
    except Exception as e:
        print(f"Error saving {path}: {e}")
        return False


def scrape_lesson(context, page, lesson_url, lesson_number):
    print("\n" + "=" * 80)
    print(f"Lesson {lesson_number:03d}: {lesson_url}")

    page.goto(lesson_url, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(4000)
    try:
        page.locator("h1, button.js-lsn3-play-lesson-audio, .lsn3-hidden--main-audio").first.wait_for(timeout=15000)
    except Exception:
        pass

    soup = BeautifulSoup(page.content(), "html.parser")
    title = get_title(soup)

    print("Lesson title:", title)
    print("Finding lesson audio...")

    audio_url = get_main_lesson_audio_url(page, soup, lesson_url)
    if not audio_url:
        print("Could not find lesson audio on this page.")
        return False

    print("Lesson audio URL:", audio_url)
    output_path = build_output_path(lesson_number, title, audio_url)
    print("Output file:", output_path)

    return download(context, audio_url, output_path)


success_count = 0
failed_lessons = []

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=f"pod101_profile_{site_safe}",
        headless=False,
    )

    page = context.new_page()
    page.goto(level_url, wait_until="networkidle", timeout=90000)

    print(f"\nDetected site: {site_host}")
    print("Log in if needed.")
    print("Make sure the level page is fully loaded.")
    input("Press ENTER here when ready... ")

    page.wait_for_timeout(5000)

    lesson_links = collect_lesson_links(page, level_url)
    if not lesson_links:
        print("\nNo lesson links found. Treating this as one lesson URL.")
        lesson_links = [level_url]

    print(f"\nFound {len(lesson_links)} lessons:\n")
    for i, link in enumerate(lesson_links, 1):
        print(f"{i:03d}. {link}")

    confirm = input("\nType YES to download all lesson audio files: ").strip().upper()
    if confirm != "YES":
        print("Canceled.")
        context.close()
        raise SystemExit

    for lesson_number, lesson_link in enumerate(lesson_links, 1):
        try:
            if scrape_lesson(context, page, lesson_link, lesson_number):
                success_count += 1
            else:
                failed_lessons.append((lesson_number, lesson_link))
        except Exception as e:
            print(f"\nLesson failed: {lesson_link}")
            print(e)
            failed_lessons.append((lesson_number, lesson_link))

        if lesson_number < len(lesson_links):
            print("\nResting 20 seconds before next lesson...\n")
            time.sleep(20)

    context.close()


print("\nDONE")
print("Output folder:", ROOT_DIR)
print("Lesson audio files ready:", success_count)
print("Failures:", len(failed_lessons))

if failed_lessons:
    print("\nLessons that need attention:")
    for lesson_number, lesson_link in failed_lessons:
        print(f"{lesson_number:03d}. {lesson_link}")
