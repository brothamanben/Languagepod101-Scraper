import os
import re
import html
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


LANGUAGE = input("Language, e.g. Korean, Japanese, Thai, Russian: ").strip()
language_safe = re.sub(r"[^a-zA-Z0-9]+", "_", LANGUAGE).strip("_").lower()

LEVEL_JOBS = []

print("\nAdd each level one at a time.")
print("Paste a level URL, then enter its level name.")
print("When finished, type DONE as the URL.\n")

while True:
    level_url = input("Paste level page URL, or type DONE: ").strip()

    if level_url.upper() == "DONE":
        break

    if not level_url:
        continue

    level_name = input("Level name, e.g. L1, L2, L3, Beginner: ").strip()

    if not level_name:
        print("Level name cannot be blank.")
        continue

    level_safe = re.sub(r"[^a-zA-Z0-9]+", "_", level_name).strip("_").lower()

    LEVEL_JOBS.append({
        "url": level_url,
        "level": level_name,
        "level_safe": level_safe,
        "root_dir": f"{language_safe}_{level_safe}_pod101"
    })

if not LEVEL_JOBS:
    print("No levels added. Exiting.")
    raise SystemExit

print("\nLevels queued:")
for i, job in enumerate(LEVEL_JOBS, 1):
    print(f"{i}. {job['level']} - {job['url']}")

confirm = input("\nType YES to start downloading all queued levels: ").strip().upper()

if confirm != "YES":
    print("Canceled.")
    raise SystemExit


def clean(text):
    if not text:
        return ""
    text = html.unescape(str(text))
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(text, max_len=45):
    text = clean(text)
    text = re.sub(r'[<>:"/\\|?*]+', "", text)
    text = text.replace("'", "")
    text = text.replace("’", "")
    text = text.replace("...", "")
    text = text.replace("…", "")
    text = re.sub(r"[\s-]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"[^\w.]", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text)
    text = text.strip("._- ")
    text = text.rstrip(". ")
    return text[:max_len].rstrip(". ") or "item"


def ensure_parent_folder(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)


def lesson_already_done(root_dir, lesson_number):
    lesson_folder = os.path.join(root_dir, f"{lesson_number:03d}")

    if not os.path.exists(lesson_folder):
        return False

    for file in os.listdir(lesson_folder):
        file_path = os.path.join(lesson_folder, file)

        if file.endswith("_anki.csv") and os.path.getsize(file_path) > 0:
            return True

    return False


def is_audio(url):
    return url and "learningcenter/audio" in url and url.lower().endswith((".mp3", ".m4a"))


def get_audio_url(tag):
    return (
        tag.get("data-src")
        or tag.get("data-audio")
        or tag.get("data-url")
        or tag.get("src")
    )


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
            print("Existing audio:", os.path.basename(path))
            return True

        r = context.request.get(url)

        if r.status != 200:
            print("Failed:", url)
            return False

        with open(path, "wb") as f:
            f.write(r.body())

        print("Saved:", os.path.basename(path))
        return True

    except Exception as e:
        print(f"Error saving {path}: {e}")
        return False


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


def scrape_lesson(context, page, lesson_url, lesson_number, level_name, level_safe, root_dir):
    print("\n" + "=" * 70)
    print(f"{level_name} - Lesson {lesson_number:03d}: {lesson_url}")

    page.goto(lesson_url, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(4000)

    soup = BeautifulSoup(page.content(), "html.parser")

    title = get_title(soup)
    slug = safe_filename(title).lower()

    lesson_folder = os.path.join(root_dir, f"{lesson_number:03d}")
    os.makedirs(lesson_folder, exist_ok=True)

    lesson_meta = f"{LANGUAGE} - {level_name} - {lesson_number:03d} - {title}"

    rows = []
    seen = set()

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

        filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_dlg_{dialogue_count:02d}.mp3"
        filename = safe_filename(filename.replace(".mp3", ""), max_len=90) + ".mp3"
        path = os.path.join(lesson_folder, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Dialogue Line)",
                "Front": front,
                "Back": clean(btn.get("data-english-text")),
                "Audio": f"[sound:{filename}]"
            })

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

        safe_word = safe_filename(word, max_len=25)
        filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_vw_{vocab_count:02d}_{safe_word}.mp3"
        filename = safe_filename(filename.replace(".mp3", ""), max_len=90) + ".mp3"
        path = os.path.join(lesson_folder, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Vocab Word)",
                "Front": word,
                "Back": meaning,
                "Audio": f"[sound:{filename}]"
            })

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

        filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_vs_{sentence_count:02d}.mp3"
        filename = safe_filename(filename.replace(".mp3", ""), max_len=90) + ".mp3"
        path = os.path.join(lesson_folder, filename)

        if download(context, url, path):
            rows.append({
                "Type": f"{lesson_meta} (Vocab Sentence)",
                "Front": text,
                "Back": "",
                "Audio": f"[sound:{filename}]"
            })

    csv_filename = f"{language_safe}_{level_safe}_{lesson_number:03d}_{slug}_anki.csv"
    csv_filename = safe_filename(csv_filename.replace(".csv", ""), max_len=90) + ".csv"
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


BASE_PROFILE_DIR = os.path.abspath("pod101_profile")
WORKER_PROFILE_ROOT = os.path.abspath("pod101_worker_profiles")


def prepare_base_profile(first_level_url):
    os.makedirs(WORKER_PROFILE_ROOT, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=BASE_PROFILE_DIR,
            headless=False
        )
        page = context.new_page()

        print("\nBrowser opened.")
        print("Log in if needed on the first page.")
        print("After that, all queued levels will start downloading in parallel.\n")

        page.goto(first_level_url, wait_until="networkidle", timeout=90000)

        print("\nLog in if needed.")
        print("Make sure the level page is fully loaded.")
        input("Press ENTER here when ready...")

        context.close()


def clone_profile_for_job(job_index, level_safe):
    profile_dir = os.path.join(WORKER_PROFILE_ROOT, f"worker_{job_index:02d}_{level_safe}")

    if os.path.exists(profile_dir):
        shutil.rmtree(profile_dir, ignore_errors=True)

    shutil.copytree(BASE_PROFILE_DIR, profile_dir)

    for lock_name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
        lock_path = os.path.join(profile_dir, lock_name)
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except OSError:
                pass

    return profile_dir


def process_level(job, job_index, total_jobs):
    level_url = job["url"]
    level_name = job["level"]
    level_safe = job["level_safe"]
    root_dir = job["root_dir"]
    worker_profile_dir = clone_profile_for_job(job_index, level_safe)

    os.makedirs(root_dir, exist_ok=True)

    print("\n" + "#" * 80)
    print(f"STARTING LEVEL {job_index}/{total_jobs}: {level_name}")
    print("Folder:", root_dir)
    print("URL:", level_url)
    print("Profile:", worker_profile_dir)
    print("#" * 80)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=worker_profile_dir,
            headless=False
        )
        page = context.new_page()

        try:
            page.goto(level_url, wait_until="networkidle", timeout=90000)
            page.wait_for_timeout(5000)

            lesson_links = collect_lesson_links(page, level_url)

            if not lesson_links:
                print(f"\nNo lesson links found for {level_name}. Treating this as one lesson URL.")
                lesson_links = [level_url]

            print(f"\nFound {len(lesson_links)} lessons for {level_name}:\n")

            for i, link in enumerate(lesson_links, 1):
                print(f"{i:03d}. {link}")

            all_rows = []
            skipped_count = 0

            for lesson_number, lesson_link in enumerate(lesson_links, 1):
                if lesson_already_done(root_dir, lesson_number):
                    print(f"\nSkipping {level_name} Lesson {lesson_number:03d} because it already has an Anki CSV.")
                    skipped_count += 1
                    continue

                rows = scrape_lesson(
                    context=context,
                    page=page,
                    lesson_url=lesson_link,
                    lesson_number=lesson_number,
                    level_name=level_name,
                    level_safe=level_safe,
                    root_dir=root_dir
                )

                all_rows.extend(rows)

                if lesson_number < len(lesson_links):
                    print(f"\nResting 20 seconds before next lesson in {level_name}...\n")
                    time.sleep(20)

            level_master_csv = os.path.join(
                root_dir,
                safe_filename(f"{language_safe}_{level_safe}_MASTER_anki", max_len=90) + ".csv"
            )

            ensure_parent_folder(level_master_csv)

            df_level = pd.DataFrame(all_rows, columns=["Type", "Front", "Back", "Audio"])
            df_level.to_csv(level_master_csv, index=False, encoding="utf-8-sig")

            print("\nDONE WITH LEVEL:", level_name)
            print("Main folder:", root_dir)
            print("Level master CSV:", level_master_csv)
            print("New cards this level:", len(all_rows))
            print("Skipped lessons this level:", skipped_count)

            return {
                "level_name": level_name,
                "rows": all_rows,
                "skipped_count": skipped_count
            }
        finally:
            context.close()
            shutil.rmtree(worker_profile_dir, ignore_errors=True)


prepare_base_profile(LEVEL_JOBS[0]["url"])

grand_all_rows = []
grand_total_new_cards = 0
grand_total_skipped = 0

max_workers = max(1, len(LEVEL_JOBS))

with ThreadPoolExecutor(max_workers=max_workers) as executor:
    future_to_job = {
        executor.submit(process_level, job, job_index, len(LEVEL_JOBS)): job
        for job_index, job in enumerate(LEVEL_JOBS, 1)
    }

    for future in as_completed(future_to_job):
        job = future_to_job[future]

        try:
            result = future.result()
        except Exception as e:
            print(f"\nLevel failed: {job['level']} - {e}")
            continue

        grand_all_rows.extend(result["rows"])
        grand_total_new_cards += len(result["rows"])
        grand_total_skipped += result["skipped_count"]


all_levels_master_csv = safe_filename(f"{language_safe}_ALL_LEVELS_MASTER_anki", max_len=90) + ".csv"

df_all = pd.DataFrame(grand_all_rows, columns=["Type", "Front", "Back", "Audio"])
df_all.to_csv(all_levels_master_csv, index=False, encoding="utf-8-sig")

print("\nALL DONE")
print("Combined master CSV:", all_levels_master_csv)
print("Total new cards this run:", grand_total_new_cards)
print("Total skipped lessons:", grand_total_skipped)




