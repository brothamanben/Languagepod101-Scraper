import os
import re
import html
import time
import pandas as pd
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

level_url = input("Paste level page URL: ").strip()
LANGUAGE = input("Language, e.g. Korean, Japanese, Thai: ").strip()
LEVEL = input("Level, e.g. L1, L3, Beginner: ").strip()

language_safe = re.sub(r"[^a-zA-Z0-9]+", "_", LANGUAGE).strip("_").lower()
level_safe = re.sub(r"[^a-zA-Z0-9]+", "_", LEVEL).strip("_").lower()

ROOT_DIR = f"{language_safe}_{level_safe}_pod101"
os.makedirs(ROOT_DIR, exist_ok=True)


def clean(text):
    if not text:
        return ""
    text = html.unescape(str(text))
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(text, max_len=70):
    text = clean(text)
    text = re.sub(r'[<>:"/\\|?*]+', "", text)
    text = text.replace("'", "").replace("’", "")
    text = text.replace("...", "").replace("…", "")
    text = re.sub(
        r"[^\w가-힣ぁ-んァ-ン一-龯а-яА-ЯёЁà-ỹÀ-Ỹ]+",
        "_",
        text,
        flags=re.UNICODE
    )
    text = text.strip("._- ")
    text = text.rstrip(". ")
    return text[:max_len] or "item"


def safe_folder(text, max_len=70):
    text = clean(text)
    text = re.sub(r'[<>:"/\\|?*]+', "", text)
    text = text.replace("'", "").replace("’", "")
    text = text.replace("...", "").replace("…", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(". ")
    return text[:max_len] or "Lesson"


def ensure_parent_folder(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)


def lesson_already_done(lesson_number):
    if not os.path.exists(ROOT_DIR):
        return False

    prefix = f"{lesson_number:03d} -"

    for folder in os.listdir(ROOT_DIR):
        folder_path = os.path.join(ROOT_DIR, folder)

        if not os.path.isdir(folder_path):
            continue

        if not folder.startswith(prefix):
            continue

        for file in os.listdir(folder_path):
            file_path = os.path.join(folder_path, file)

            if file.endswith("_anki.csv") and os.path.getsize(file_path) > 0:
                return True

    return False


def is_audio(url):
    return url and "learningcenter/audio" in url and url.lower().endswith((".mp3", ".m4a"))


def get_audio_url(tag):
    return tag.get("data-src") or tag.get("data-audio") or tag.get("data-url") or tag.get("src")


def get_title(soup):
    t = soup.find("title")
    if t:
        return clean(re.sub(r"\s*-\s*.*101.*$", "", t.get_text()))
    h1 = soup.find("h1")
    return clean(h1.get_text()) if h1 else "lesson"


def download(context, url, path):
    try:
        ensure_parent_folder(path)

        if os.path.exists(path) and os.path.getsize(path) > 0:
            print("⏭️ Existing audio:", os.path.basename(path))
            return True

        r = context.request.get(url)

        if r.status != 200:
            print("❌ Failed:", url)
            return False

        with open(path, "wb") as f:
            f.write(r.body())

        print("✅", os.path.basename(path))
        return True

    except Exception as e:
        print(f"❌ Error saving {path}: {e}")
        return False


def scroll_page(page):
    for _ in range(18):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        except:
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
        full_url = urljoin(base_url, raw)
        parsed = urlparse(full_url)

        if "/lesson/" not in parsed.path:
            return

        full_url = full_url.split("#")[0]

        if full_url not in seen:
            seen.add(full_url)
            links.append(full_url)

    for a in soup.select("a[href]"):
        add_link(a.get("href"))

    patterns = [
        r'"url"\s*:\s*"([^"]*?/lesson/[^"]+)"',
        r"&quot;url&quot;\s*:\s*&quot;([^&]+?/lesson/[^&]+)&quot;",
        r"(\/lesson\/[^\"'<>\s]+?\?lp=\d+)",
        r"(https?://[^\"'<>\s]+?/lesson/[^\"'<>\s]+?\?lp=\d+)"
    ]

    for pattern in patterns:
        for match in re.findall(pattern, page_html):
            add_link(match)

    return links


def scrape_lesson(context, page, lesson_url, lesson_number):
    print("\n" + "=" * 70)
    print(f"Lesson {lesson_number:03d}: {lesson_url}")

    page.goto(lesson_url, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(4000)

    soup = BeautifulSoup(page.content(), "html.parser")

    title = get_title(soup)
    slug = safe_filename(title).lower()

    folder_title = safe_folder(title)
    lesson_folder = os.path.join(ROOT_DIR, f"{lesson_number:03d} - {folder_title}")
    os.makedirs(lesson_folder, exist_ok=True)

    lesson_meta = f"{LANGUAGE} - {LEVEL} - {lesson_number:03d} - {title}"

    rows = []
    seen = set()

    # -------------------------
    # 1. DIALOGUE ONLY
    # -------------------------
    dialogue_count = 0

    for tr in soup.select("tr"):
        btn = tr.select_one(".js-lsn3-play-dialogue[data-src]")
        if not btn:
            continue

        url = get_audio_url(btn)

        if not is_audio(url) or url in seen:
            continue

        speaker_tag = tr.select_one(".lsn3-lesson-dialogue__td--name")
        text_tag = tr.select_one(".lsn3-lesson-dialogue__td--text")

        speaker = clean(speaker_tag.get_text()) if speaker_tag else ""
        text = clean(text_tag.get_text(" ", strip=True)) if text_tag else clean(btn.get("data-text"))

        if speaker and text and not text.startswith(speaker):
            front = f"{speaker} {text}"
        else:
            front = text

        seen.add(url)
        dialogue_count += 1

        filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_dialogue_line_{dialogue_count:02d}.mp3"
        path = os.path.join(lesson_folder, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Dialogue Line)",
                "Front": front,
                "Back": clean(btn.get("data-english-text")),
                "Audio": f"[sound:{filename}]"
            })

    # -------------------------
    # 2. VOCAB WORDS ONLY
    # -------------------------
    vocab_count = 0

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

        btn = tr.select_one(".js-lsn3-play-vocabulary[data-src]")

        if not word_tag or not btn:
            continue

        url = get_audio_url(btn)

        if not is_audio(url) or url in seen:
            continue

        word = clean(word_tag.get_text(" ", strip=True))
        meaning = clean(meaning_tag.get_text(" ", strip=True)) if meaning_tag else ""

        if not word:
            continue

        seen.add(url)
        vocab_count += 1

        safe_word = safe_filename(word, max_len=30)
        filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_vocab_word_{vocab_count:02d}_{safe_word}.mp3"
        path = os.path.join(lesson_folder, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Vocab Word)",
                "Front": word,
                "Back": meaning,
                "Audio": f"[sound:{filename}]"
            })

    # -------------------------
    # 3. VOCAB SENTENCES / EXAMPLES ONLY
    # -------------------------
    sentence_count = 0

    example_blocks = soup.select(
        ".lsn3-lesson-vocabulary__sample, "
        ".lesson-vocabulary__sample, "
        "[class*='vocabulary__sample'], "
        "[class*='sample-sentence'], "
        "[class*='example']"
    )

    for block in example_blocks:
        btn = block.select_one(".js-lsn3-play-vocabulary[data-src]")

        if not btn:
            continue

        url = get_audio_url(btn)

        if not is_audio(url) or url in seen:
            continue

        text = clean(btn.get("data-text")) or clean(block.get_text(" ", strip=True))

        if not text:
            continue

        seen.add(url)
        sentence_count += 1

        filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_vocab_sentence_{sentence_count:02d}.mp3"
        path = os.path.join(lesson_folder, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Vocab Sentence)",
                "Front": text,
                "Back": "",
                "Audio": f"[sound:{filename}]"
            })

    csv_filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_anki.csv"
    csv_path = os.path.join(lesson_folder, csv_filename)

    ensure_parent_folder(csv_path)

    df = pd.DataFrame(rows, columns=["Type", "Front", "Back", "Audio"])
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    print("Dialogue:", dialogue_count)
    print("Vocab words:", vocab_count)
    print("Vocab sentences:", sentence_count)
    print("Total cards:", len(rows))
    print("CSV:", csv_path)

    return rows


all_rows = []

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir="pod101_profile",
        headless=False
    )

    page = context.new_page()
    page.goto(level_url, wait_until="networkidle", timeout=90000)

    print("\n👉 Log in if needed.")
    print("👉 Make sure the level page is fully loaded.")
    input("Press ENTER here when ready...")

    page.wait_for_timeout(5000)

    lesson_links = collect_lesson_links(page, level_url)

    if not lesson_links:
        print("\nNo lesson links found. Treating this as one lesson URL.")
        lesson_links = [level_url]

    print(f"\nFound {len(lesson_links)} lessons:\n")

    for i, link in enumerate(lesson_links, 1):
        print(f"{i:03d}. {link}")

    confirm = input("\nType YES to download all lessons: ").strip().upper()

    if confirm != "YES":
        print("Canceled.")
        context.close()
        raise SystemExit

    skipped_count = 0

    for lesson_number, lesson_link in enumerate(lesson_links, 1):

        if lesson_already_done(lesson_number):
            print(f"\n⏭️ Skipping Lesson {lesson_number:03d} because it already has an Anki CSV.")
            skipped_count += 1
            continue

        rows = scrape_lesson(context, page, lesson_link, lesson_number)
        all_rows.extend(rows)

        if lesson_number < len(lesson_links):
            print("\n⏳ Resting 20 seconds before next lesson...\n")
            time.sleep(20)

    context.close()


master_csv = os.path.join(ROOT_DIR, f"{language_safe}_{level_safe}_MASTER_anki.csv")
ensure_parent_folder(master_csv)

df_master = pd.DataFrame(all_rows, columns=["Type", "Front", "Back", "Audio"])
df_master.to_csv(master_csv, index=False, encoding="utf-8-sig")

print("\nDONE")
print("Main folder:", ROOT_DIR)
print("Master CSV:", master_csv)
print("New cards this run:", len(all_rows))
print("Skipped lessons:", skipped_count)
