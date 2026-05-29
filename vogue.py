import concurrent.futures
import csv
import copy
import json
import os
import random
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from unidecode import unidecode


BASE_URL = "https://www.vogue.com"
DEFAULT_SAVE_PATH = "vogue_downloads"
CSV_COLUMNS = [
    "designer",
    "show",
    "gallery",
    "show_description",
    "image_index",
    "image_name",
    "image_url",
]
ALLOWED_GALLERIES = {"collection", "details"}
SEASON_PATTERNS = [
    (("spring", "summer"), "spring-summer"),
    (("fall", "winter"), "fall-winter"),
    (("autumn", "winter"), "autumn-winter"),
    (("spring-summer",), "spring-summer"),
    (("fall-winter",), "fall-winter"),
    (("autumn-winter",), "autumn-winter"),
    (("pre", "fall"), "pre-fall"),
]
SEASON_ALIASES = {
    "ss": "spring-summer",
    "fw": "fall-winter",
    "aw": "autumn-winter",
    "spring": "spring",
    "summer": "summer",
    "fall": "fall",
    "autumn": "autumn",
    "winter": "winter",
    "resort": "resort",
    "cruise": "cruise",
    "prefall": "pre-fall",
    "pre-fall": "pre-fall",
    "bridal": "bridal",
    "couture": "couture",
    "menswear": "menswear",
    "haute-couture": "haute-couture",
}
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]
REQUEST_PROFILES = {
    "page": (4.5, 11.0),
    "asset": (1.2, 4.0),
    "show": (7.0, 16.0),
    "designer": (12.0, 24.0),
}
IMAGE_WORKERS = 4
IMAGE_DELAY = (0.3, 1.0)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ScrapeState:
    def __init__(self, path):
        self.path = path
        self.data = self._load()

    def _default(self):
        timestamp = utc_now()
        return {
            "version": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "designers": {},
        }

    def _load(self):
        if not os.path.exists(self.path):
            return self._default()

        with open(self.path, "r", encoding="utf-8") as file:
            return json.load(file)

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.data["updated_at"] = utc_now()
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(self.data, file, indent=2, ensure_ascii=True)
        os.replace(temp_path, self.path)

    def ensure_designer(self, designer, designer_slug):
        designer_state = self.data["designers"].setdefault(
            designer_slug,
            {
                "designer": designer,
                "designer_slug": designer_slug,
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "last_attempted_at": None,
                "last_error": None,
                "shows": {},
            },
        )
        designer_state["designer"] = designer
        designer_state["designer_slug"] = designer_slug
        return designer_state

    def ensure_show(self, show_data):
        designer_state = self.ensure_designer(
            show_data["designer"],
            show_data["designer_slug"],
        )
        show_state = designer_state["shows"].setdefault(
            show_data["show_folder"],
            {
                "show": show_data["show"],
                "show_slug": show_data["show_slug"],
                "show_folder": show_data["show_folder"],
                "show_url": show_data["show_url"],
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "last_attempted_at": None,
                "last_error": None,
                "metadata_saved": False,
                "description_saved": False,
                "galleries": {},
            },
        )

        show_state["show"] = show_data["show"]
        show_state["show_slug"] = show_data["show_slug"]
        show_state["show_folder"] = show_data["show_folder"]
        show_state["show_url"] = show_data["show_url"]

        for gallery_name, images in show_data["galleries"].items():
            gallery_state = show_state["galleries"].setdefault(
                gallery_name,
                {
                    "status": "pending",
                    "total_images": 0,
                    "downloaded_images": 0,
                    "last_completed_image": 0,
                    "started_at": None,
                    "completed_at": None,
                    "last_error": None,
                },
            )
            gallery_state["total_images"] = len(images)

        return designer_state, show_state

    def sync_show(self, show_data, show_path):
        designer_state, show_state = self.ensure_show(show_data)
        metadata_path = os.path.join(show_path, "show_metadata.json")
        description_path = os.path.join(show_path, "description.md")
        show_state["metadata_saved"] = os.path.exists(metadata_path)
        show_state["description_saved"] = os.path.exists(description_path)

        any_downloaded = False
        all_complete = True

        for gallery_name, images in show_data["galleries"].items():
            gallery_state = show_state["galleries"][gallery_name]
            downloaded_images = 0
            last_completed_image = 0

            for record in images:
                export_path = os.path.join(show_path, gallery_name, record["image_name"])
                if os.path.exists(export_path):
                    downloaded_images += 1
                    last_completed_image = record["image_index"]

            gallery_state["total_images"] = len(images)
            gallery_state["downloaded_images"] = downloaded_images
            gallery_state["last_completed_image"] = last_completed_image

            if downloaded_images == 0:
                gallery_state["status"] = "pending"
                gallery_state["completed_at"] = None
            elif downloaded_images < len(images):
                gallery_state["status"] = "in_progress"
                gallery_state["completed_at"] = None
                any_downloaded = True
                all_complete = False
            else:
                gallery_state["status"] = "completed"
                gallery_state["completed_at"] = gallery_state["completed_at"] or utc_now()
                any_downloaded = True

            if downloaded_images != len(images):
                all_complete = False

        if show_data["galleries"] and all_complete:
            show_state["status"] = "completed"
            show_state["completed_at"] = show_state["completed_at"] or utc_now()
        elif any_downloaded or show_state["metadata_saved"] or show_state["description_saved"]:
            show_state["status"] = "in_progress"
            show_state["completed_at"] = None
        else:
            show_state["status"] = "pending"
            show_state["completed_at"] = None

        self._refresh_designer_status(designer_state)
        self.save()
        return copy.deepcopy(show_state)

    def mark_designer_started(self, designer, designer_slug):
        designer_state = self.ensure_designer(designer, designer_slug)
        designer_state["started_at"] = designer_state["started_at"] or utc_now()
        designer_state["last_attempted_at"] = utc_now()
        if designer_state["status"] != "completed":
            designer_state["status"] = "in_progress"
            designer_state["completed_at"] = None
        self.save()

    def mark_show_started(self, show_data):
        designer_state, show_state = self.ensure_show(show_data)
        timestamp = utc_now()
        designer_state["started_at"] = designer_state["started_at"] or timestamp
        designer_state["last_attempted_at"] = timestamp
        designer_state["status"] = "in_progress"
        designer_state["completed_at"] = None
        show_state["started_at"] = show_state["started_at"] or timestamp
        show_state["last_attempted_at"] = timestamp
        show_state["status"] = "in_progress"
        show_state["completed_at"] = None
        self.save()

    def mark_metadata_saved(self, show_data):
        designer_state, show_state = self.ensure_show(show_data)
        show_state["metadata_saved"] = True
        show_state["description_saved"] = True
        if show_state["status"] == "pending":
            show_state["status"] = "in_progress"
        self._refresh_designer_status(designer_state)
        self.save()

    def mark_image_downloaded(self, show_data, gallery_name, image_index):
        designer_state, show_state = self.ensure_show(show_data)
        gallery_state = show_state["galleries"][gallery_name]
        gallery_state["started_at"] = gallery_state["started_at"] or utc_now()
        gallery_state["downloaded_images"] = min(
            gallery_state["total_images"],
            gallery_state["downloaded_images"] + 1,
        )
        gallery_state["last_completed_image"] = max(
            gallery_state["last_completed_image"],
            image_index,
        )

        if gallery_state["downloaded_images"] >= gallery_state["total_images"]:
            gallery_state["status"] = "completed"
            gallery_state["completed_at"] = utc_now()
        else:
            gallery_state["status"] = "in_progress"
            gallery_state["completed_at"] = None

        show_state["status"] = "in_progress"
        show_state["completed_at"] = None
        self._refresh_designer_status(designer_state)
        self.save()

    def mark_show_error(self, show_data, error_message):
        designer_state, show_state = self.ensure_show(show_data)
        timestamp = utc_now()
        designer_state["last_attempted_at"] = timestamp
        designer_state["last_error"] = error_message
        designer_state["status"] = "in_progress"
        designer_state["completed_at"] = None
        show_state["last_attempted_at"] = timestamp
        show_state["last_error"] = error_message
        show_state["status"] = "in_progress"
        show_state["completed_at"] = None
        self.save()

    def mark_designer_error(self, designer, designer_slug, error_message):
        designer_state = self.ensure_designer(designer, designer_slug)
        designer_state["last_attempted_at"] = utc_now()
        designer_state["last_error"] = error_message
        designer_state["status"] = "in_progress"
        designer_state["completed_at"] = None
        self.save()

    def finalize_show(self, show_data, show_path):
        return self.sync_show(show_data, show_path)

    def is_designer_completed(self, designer_slug):
        designer_state = self.data["designers"].get(designer_slug)
        return bool(designer_state and designer_state.get("status") == "completed")

    def _refresh_designer_status(self, designer_state):
        shows = list(designer_state["shows"].values())
        if not shows:
            designer_state["status"] = "pending"
            designer_state["completed_at"] = None
            return

        if all(show["status"] == "completed" for show in shows):
            designer_state["status"] = "completed"
            designer_state["completed_at"] = designer_state["completed_at"] or utc_now()
            return

        if any(show["status"] in {"in_progress", "completed"} for show in shows):
            designer_state["status"] = "in_progress"
            designer_state["completed_at"] = None
            return

        designer_state["status"] = "pending"
        designer_state["completed_at"] = None


def extract_json_from_script(scripts, key_fragment):
    for script in scripts:
        if script.string and key_fragment in script.string:
            js = script.string
            break
    else:
        return None

    try:
        js_clean = js.split(" = ", 1)[1]
        brace_count = 0
        for i, char in enumerate(js_clean):
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    js_clean = js_clean[: i + 1]
                    break
        return json.loads(js_clean)
    except Exception as e:
        print(f"JSON extraction failed: {e}")
        return None


def normalize_slug(value):
    return unidecode(
        value.replace(" ", "-")
        .replace(".", "-")
        .replace("&", "")
        .replace("+", "")
        .replace("--", "-")
        .lower()
    ).strip("-")


def normalize_show_folder(show):
    normalized = normalize_slug(show)
    parts = normalized.split("-")

    year = None
    year_index = None
    for index, part in enumerate(parts):
        if part.isdigit() and len(part) == 4:
            year = part
            year_index = index
            break

    season = None
    season_indexes = []
    for pattern_parts, label in SEASON_PATTERNS:
        pattern_length = len(pattern_parts)
        for index in range(len(parts) - pattern_length + 1):
            if tuple(parts[index : index + pattern_length]) == pattern_parts:
                season = label
                season_indexes = list(range(index, index + pattern_length))
                break
        if season:
            break

    if not season:
        for index, part in enumerate(parts):
            alias = SEASON_ALIASES.get(part)
            if alias:
                season = alias
                season_indexes = [index]
                break

    used_indexes = set(season_indexes)
    if year_index is not None:
        used_indexes.add(year_index)

    remaining = [part for index, part in enumerate(parts) if index not in used_indexes]

    ordered = []
    if year:
        ordered.append(year)
    if season:
        ordered.append(season)
    ordered.extend(remaining)

    if ordered:
        return "-".join(ordered)

    return normalized


def slugify_filename(value, fallback="item"):
    cleaned = unidecode(value).lower()
    allowed = []
    previous_dash = False

    for char in cleaned:
        if char.isalnum():
            allowed.append(char)
            previous_dash = False
            continue
        if char in {"-", "_", " ", "."} and not previous_dash:
            allowed.append("-")
            previous_dash = True

    result = "".join(allowed).strip("-")
    return result or fallback


def extract_meta_description(soup):
    tag = soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


def extract_show_description(soup):
    body = soup.find(attrs={"data-testid": "BodyWrapper"})
    if body:
        paragraphs = [
            paragraph.get_text(" ", strip=True)
            for paragraph in body.find_all("p")
            if paragraph.get_text(" ", strip=True)
        ]
        if paragraphs:
            return "\n\n".join(paragraphs)

    return extract_meta_description(soup)


def get_extension_from_url(url, default=".jpg"):
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}:
        return suffix
    return default


def build_image_name(gallery_slug, image_index, image_url):
    extension = get_extension_from_url(image_url)
    gallery_prefix = "look" if gallery_slug == "collection" else gallery_slug.rstrip("s")
    return f"{gallery_prefix}_{image_index:04d}{extension}"


def _progress_bar(current, total, width=20):
    if total == 0:
        return "░" * width
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)


class VogueClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "DNT": "1",
            }
        )
        self.last_request_at = 0.0

        retry = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _sleep(self, profile):
        minimum, maximum = REQUEST_PROFILES[profile]
        delay = random.uniform(minimum, maximum)

        elapsed = time.monotonic() - self.last_request_at if self.last_request_at else None
        if elapsed is None or elapsed >= delay:
            return

        remaining = delay - elapsed
        time.sleep(remaining)

    def get(self, url, profile="page", referer=None, timeout=60):
        self._sleep(profile)
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer or BASE_URL,
        }
        response = self.session.get(url, headers=headers, timeout=timeout)
        self.last_request_at = time.monotonic()
        response.raise_for_status()
        return response

    def rest(self, profile):
        minimum, maximum = REQUEST_PROFILES[profile]
        delay = random.uniform(minimum, maximum)
        time.sleep(delay)


CLIENT = VogueClient()


def fetch_soup(url, profile="page", referer=None):
    response = CLIENT.get(url, profile=profile, referer=referer)
    return BeautifulSoup(response.content, "html5lib")


def read_designers(txt_path):
    with open(txt_path, "r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip()]


def get_state_path(save_path, state_path=None):
    return state_path or os.path.join(save_path, "_scrape_state.json")


def resolve_save_path(save_path=None):
    return save_path or DEFAULT_SAVE_PATH


def main(argv=None):
    argv = argv or sys.argv[1:]

    if not argv:
        print("Usage:")
        print("  python3 vogue.py download-all [designers.txt] [save_path]")
        print("  python3 vogue.py download-designer <designer> [save_path]")
        print("  python3 vogue.py download-show <designer> <show> [save_path]")
        return 1

    command = argv[0]

    if command == "download-all":
        txt_path = argv[1] if len(argv) >= 2 else "designers.txt"
        save_path = argv[2] if len(argv) >= 3 else None
        all_designers_to_download_images(txt_path, save_path)
        return 0

    if command == "download-designer":
        if len(argv) < 2:
            print("Usage: python3 vogue.py download-designer <designer> [save_path]")
            return 1
        designer = argv[1]
        save_path = argv[2] if len(argv) >= 3 else None
        designer_to_download_images(designer, save_path)
        return 0

    if command == "download-show":
        if len(argv) < 3:
            print("Usage: python3 vogue.py download-show <designer> <show> [save_path]")
            return 1
        designer = argv[1]
        show = argv[2]
        save_path = argv[3] if len(argv) >= 4 else None
        designer_show_to_download_images(designer, show, save_path)
        return 0

    print(f"Unknown command: {command}")
    return 1


def get_show_data(designer, show):
    show_slug = normalize_slug(show)
    show_folder = normalize_show_folder(show)
    designer_slug = normalize_slug(designer)
    url = f"{BASE_URL}/fashion-shows/{show_slug}/{designer_slug}"
    soup = fetch_soup(url, profile="page", referer=f"{BASE_URL}/fashion-shows/designer/{designer_slug}")

    data = extract_json_from_script(
        soup.find_all("script", type="text/javascript"),
        "runwayShowGalleries",
    )
    if not data:
        print(f"Could not load show: {designer} - {show}")
        return None

    show_description = extract_show_description(soup)
    records = []
    gallery_records = {}

    try:
        galleries = data["transformed"]["runwayShowGalleries"]["galleries"]
    except Exception as e:
        print(f"Failed to find gallery items: {e}")
        return None

    for gallery in galleries:
        gallery_title = gallery.get("title") or "Collection"
        gallery_slug = slugify_filename(gallery_title)
        if gallery_slug not in ALLOWED_GALLERIES:
            continue

        gallery_images = []
        for image_index, item in enumerate(gallery.get("items", []), start=1):
            try:
                image_url = item["image"]["sources"]["md"]["url"]
            except Exception:
                print(f"Skipping bad {gallery_slug} image item")
                continue

            image_name = build_image_name(
                gallery_slug,
                image_index,
                image_url,
            )
            record = {
                "designer": designer,
                "show": show,
                "gallery": gallery_slug,
                "show_description": show_description,
                "image_index": image_index,
                "image_name": image_name,
                "image_url": image_url,
            }
            gallery_images.append(record)
            records.append(record)

        if gallery_images:
            gallery_records[gallery_slug] = gallery_images

    return {
        "designer": designer,
        "designer_slug": designer_slug,
        "show": show,
        "show_slug": show_slug,
        "show_folder": show_folder,
        "show_description": show_description,
        "show_url": url,
        "galleries": gallery_records,
        "images": records,
    }


def write_show_metadata(show_path, show_data):
    metadata_path = os.path.join(show_path, "show_metadata.json")
    description_path = os.path.join(show_path, "description.md")
    payload = {
        "designer": show_data["designer"],
        "show": show_data["show"],
        "show_url": show_data["show_url"],
        "show_description": show_data["show_description"],
        "galleries": {
            gallery_name: len(images)
            for gallery_name, images in show_data["galleries"].items()
        },
        "image_count": len(show_data["images"]),
    }

    with open(metadata_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=True)

    with open(description_path, "w", encoding="utf-8") as file:
        file.write(f"# {show_data['designer']} - {show_data['show']}\n\n")
        file.write(show_data["show_description"].strip())
        file.write("\n")


def designer_to_shows(designer):
    designer_slug = normalize_slug(designer)
    url = f"{BASE_URL}/fashion-shows/designer/{designer_slug}"
    soup = fetch_soup(url, profile="page")

    data = extract_json_from_script(
        soup.find_all("script", type="text/javascript"),
        "window.__PRELOADED_STATE__",
    )
    if not data:
        print("Could not find JSON script")
        return []

    try:
        return [
            show["hed"]
            for show in data["transformed"]["runwayDesignerContent"]["designerCollections"]
        ]
    except Exception as e:
        print(f"Failed to parse shows list: {e}")
        return []


def _download_single_image(session, record, gallery_path, referer):
    export_path = os.path.join(gallery_path, record["image_name"])
    if os.path.exists(export_path):
        return record, "skipped"

    time.sleep(random.uniform(*IMAGE_DELAY))
    try:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer,
        }
        response = session.get(record["image_url"], headers=headers, timeout=120)
        response.raise_for_status()
        temp_path = export_path + ".tmp"
        with open(temp_path, "wb") as file:
            file.write(response.content)
        os.replace(temp_path, export_path)
        return record, "downloaded"
    except Exception as e:
        print(f"Error downloading {record['image_url']}: {e}")
        return record, f"error: {e}"


def designer_show_to_download_images(designer, show, save_path=None, state_path=None, progress=None):
    save_path = resolve_save_path(save_path)
    show_data = get_show_data(designer, show)
    if not show_data:
        return

    show_path = os.path.join(save_path, show_data["designer_slug"], show_data["show_folder"])
    os.makedirs(show_path, exist_ok=True)

    show_pos = ""
    if progress and "show_index" in progress:
        show_pos = f"  ({progress['show_index']}/{progress['total_shows']})"
    print(f"\n  ▸ {show}{show_pos}")

    state = ScrapeState(get_state_path(save_path, state_path))
    state.mark_show_started(show_data)
    state.sync_show(show_data, show_path)
    write_show_metadata(show_path, show_data)
    state.mark_metadata_saved(show_data)

    if not show_data["images"]:
        print(f"    ░ no images")
        state.finalize_show(show_data, show_path)
        return

    downloaded_count = 0
    last_error = None
    total_images = len(show_data["images"])

    for gallery_name, gallery_images in show_data["galleries"].items():
        gallery_path = os.path.join(show_path, gallery_name)
        os.makedirs(gallery_path, exist_ok=True)

        gallery_total = len(gallery_images)
        gallery_done = 0
        gallery_errors = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as executor:
            futures = {
                executor.submit(
                    _download_single_image,
                    CLIENT.session,
                    record,
                    gallery_path,
                    show_data["show_url"],
                ): record
                for record in gallery_images
            }

            for future in concurrent.futures.as_completed(futures):
                record, status = future.result()
                gallery_done += 1
                if status == "downloaded":
                    downloaded_count += 1
                elif status.startswith("error:"):
                    last_error = status
                    gallery_errors += 1

                bar = _progress_bar(gallery_done, gallery_total)
                line = f"\r    {gallery_name:<12} {bar}  {gallery_done}/{gallery_total}"
                sys.stdout.write(f"{line:<60}")
                sys.stdout.flush()

        bar = _progress_bar(gallery_done, gallery_total)
        if gallery_errors > 0:
            suffix = f"⚠ {gallery_errors} err"
        elif gallery_done == gallery_total:
            suffix = "✓"
        else:
            suffix = ""
        print(f"\r    {gallery_name:<12} {bar}  {gallery_done}/{gallery_total}  {suffix}")

    if last_error:
        state.mark_show_error(show_data, last_error)

    final_state = state.finalize_show(show_data, show_path)
    completed_images = sum(
        gallery_state["downloaded_images"]
        for gallery_state in final_state["galleries"].values()
    )

    status_icon = "✓" if completed_images == total_images else "⚠"
    designer_pos = ""
    if progress and "designer_index" in progress:
        designer_pos = f" │ designer {progress['designer_index']}/{progress['total_designers']}"
    print(f"[state: {designer} ▸ {show} │ {status_icon} {completed_images}/{total_images}{designer_pos}]")


def designer_to_download_images(designer, save_path=None, state_path=None, progress=None):
    save_path = resolve_save_path(save_path)
    state = ScrapeState(get_state_path(save_path, state_path))
    designer_slug = normalize_slug(designer)
    state.mark_designer_started(designer, designer_slug)
    shows = designer_to_shows(designer)

    if progress is None:
        progress = {}
    progress["total_shows"] = len(shows)

    for index, show in enumerate(shows, start=1):
        progress["show_index"] = index
        designer_show_to_download_images(
            designer, show, save_path, state_path=state.path, progress=progress,
        )
        if index < len(shows):
            CLIENT.rest("show")


def all_designers_to_download_images(txt_path, save_path=None, state_path=None):
    save_path = resolve_save_path(save_path)
    os.makedirs(save_path, exist_ok=True)
    resolved_state_path = get_state_path(save_path, state_path)
    state = ScrapeState(resolved_state_path)

    try:
        designers = read_designers(txt_path)
    except Exception as e:
        print(f"Error reading file {txt_path}: {e}")
        return

    if not designers:
        print("No designers found in the file.")
        return

    print(f"State file: {state.path}")

    total = len(designers)
    for designer_index, designer in enumerate(designers, start=1):
        designer_slug = normalize_slug(designer)
        if state.is_designer_completed(designer_slug):
            print(f"  ✓ {designer:<40} [{designer_index}/{total}]")
            continue

        label = f" {designer} "
        pos = f"[{designer_index}/{total}]"
        fill = max(1, 52 - len(label) - len(pos))
        print(f"\n━━{label}{'━' * fill} {pos}")

        progress = {
            "designer": designer,
            "designer_index": designer_index,
            "total_designers": total,
        }
        try:
            designer_to_download_images(
                designer, save_path, state_path=resolved_state_path, progress=progress,
            )
        except Exception as e:
            state = ScrapeState(resolved_state_path)
            state.mark_designer_error(designer, designer_slug, str(e))
            print(f"  ✗ Failed: {e}")

        state = ScrapeState(resolved_state_path)

        if designer_index < len(designers):
            CLIENT.rest("designer")


def designer_show_to_csv(designer, show, save_path=None):
    csv_path = None
    if save_path:
        os.makedirs(save_path, exist_ok=True)
        csv_path = os.path.join(
            save_path,
            f"{normalize_slug(designer)}_{normalize_show_folder(show)}.csv",
        )
        if os.path.exists(csv_path):
            print(f"CSV already exists: {csv_path}")
            return None

    show_data = get_show_data(designer, show)
    if not show_data:
        return None

    rows = show_data["images"]
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV saved to {csv_path}")

    return rows


def designer_to_csv(designer, save_path):
    designer_slug = normalize_slug(designer)
    os.makedirs(save_path, exist_ok=True)
    csv_path = os.path.join(save_path, f"{designer_slug}_all_shows.csv")

    if os.path.exists(csv_path):
        print(f"CSV already exists: {csv_path}")
        return

    shows = designer_to_shows(designer)
    if not shows:
        print(f"No shows found for {designer}")
        return

    all_rows = []
    for index, show in enumerate(shows, start=1):
        print(f"Scraping [{index}/{len(shows)}] {designer} - {show}")
        rows = designer_show_to_csv(designer, show)
        if rows:
            all_rows.extend(rows)
        if index < len(shows):
            CLIENT.rest("show")

    if all_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"All shows saved to {csv_path}")
    else:
        print("No images found to write.")


def all_designers_to_csv(txt_path, save_path):
    os.makedirs(save_path, exist_ok=True)
    csv_path = os.path.join(save_path, "all_designers.csv")

    try:
        with open(txt_path, "r", encoding="utf-8") as file:
            designers = [line.strip() for line in file if line.strip()]
    except Exception as e:
        print(f"Error reading file {txt_path}: {e}")
        return

    if not designers:
        print("No designers found in the file.")
        return

    existing_rows = set()
    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                existing_rows.add((row["designer"], row["show"]))

    with open(csv_path, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)

        if os.stat(csv_path).st_size == 0:
            writer.writeheader()

        for designer_index, designer in enumerate(designers, start=1):
            print(f"\nStarting designer [{designer_index}/{len(designers)}]: {designer}")
            try:
                shows = designer_to_shows(designer)
                for show_index, show in enumerate(shows, start=1):
                    if (designer, show) in existing_rows:
                        print(f"Already scraped: {designer} - {show}")
                        continue

                    print(f"Scraping [{show_index}/{len(shows)}] {designer} - {show}")
                    rows = designer_show_to_csv(designer, show)
                    if rows:
                        writer.writerows(rows)
                        file.flush()
                        existing_rows.add((designer, show))

                    if show_index < len(shows):
                        CLIENT.rest("show")
            except Exception as e:
                print(f"Failed to process {designer}: {e}")

            if designer_index < len(designers):
                CLIENT.rest("designer")


if __name__ == "__main__":
    raise SystemExit(main())
