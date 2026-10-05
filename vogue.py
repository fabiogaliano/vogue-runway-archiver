import argparse
import collections
import concurrent.futures
import copy
import csv
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from unidecode import unidecode


BASE_URL = "https://www.vogue.com"
DEFAULT_SAVE_PATH = "vogue_downloads"
DIRECTORY_URL = f"{BASE_URL}/fashion-shows/designers"
DIRECTORY_MAX_AGE = 7 * 24 * 3600
# Vogue answers with 404/429 when it throttles us, which looks identical to a wrong
# slug. A page that has always existed tells the two apart.
CANARY_URL = f"{BASE_URL}/fashion-shows/designer/chanel"
BLOCK_COOLDOWN = 15 * 60
MAX_BLOCK_RETRIES = 4
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
    "show": (7.0, 16.0),
    "designer": (12.0, 24.0),
}
# Image concurrency adapts: it grows slowly while downloads are clean and halves at the
# first throttling signal, so the rate settles just below what the CDN tolerates.
IMAGE_WORKERS_START = 5
IMAGE_WORKERS_MAX = 12
IMAGE_GROW_AFTER = 60
# A worker that adds less than this much throughput only adds load on Vogue.
PLATEAU_GAIN = 0.10
# A learned ceiling is re-probed after this long, since throttling thresholds drift.
CEILING_MAX_AGE = 24 * 3600
IMAGE_DELAY = (0.2, 0.6)
IMAGE_ATTEMPTS = 4
THROTTLE_PAUSE = 60
THROTTLE_STATUSES = {403, 429, 503}


class NotFound(Exception):
    pass


class Blocked(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def is_downloaded(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


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

    def get_designer(self, designer_slug):
        return self.data["designers"].get(designer_slug)

    def ensure_designer(self, designer, designer_slug):
        designer_state = self.data["designers"].setdefault(
            designer_slug,
            {
                "designer": designer,
                "designer_slug": designer_slug,
                "url_slug": None,
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

    def ensure_show(self, designer, designer_slug, show, show_folder, show_url):
        designer_state = self.ensure_designer(designer, designer_slug)
        show_state = designer_state["shows"].setdefault(
            show_folder,
            {
                "show": show,
                "show_folder": show_folder,
                "show_url": show_url,
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
        show_state["show"] = show
        show_state["show_folder"] = show_folder
        show_state["show_url"] = show_url
        return designer_state, show_state

    def mark_show_started(self, show_data):
        designer_state, show_state = self._ensure_show_data(show_data)
        timestamp = utc_now()
        designer_state["started_at"] = designer_state["started_at"] or timestamp
        designer_state["last_attempted_at"] = timestamp
        if designer_state["status"] != "completed":
            designer_state["status"] = "in_progress"
        show_state["started_at"] = show_state["started_at"] or timestamp
        show_state["last_attempted_at"] = timestamp

    def sync_show(self, show_data, show_path):
        _, show_state = self._ensure_show_data(show_data)
        show_state["metadata_saved"] = os.path.exists(os.path.join(show_path, "show_metadata.json"))
        show_state["description_saved"] = os.path.exists(os.path.join(show_path, "description.md"))
        # Some seasons (e.g. Celine 2020-2022) were video-only: nothing to download,
        # and leaving them pending made the designer re-scrape on every run.
        show_state["empty"] = not show_data["galleries"]

        any_downloaded = False
        all_complete = True

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
            downloaded_images = 0
            last_completed_image = 0

            for record in images:
                if is_downloaded(os.path.join(show_path, gallery_name, record["image_name"])):
                    downloaded_images += 1
                    last_completed_image = record["image_index"]

            gallery_state["total_images"] = len(images)
            gallery_state["downloaded_images"] = downloaded_images
            gallery_state["last_completed_image"] = last_completed_image

            if downloaded_images == 0:
                gallery_state["status"] = "pending"
                gallery_state["completed_at"] = None
                all_complete = False
            elif downloaded_images < len(images):
                gallery_state["status"] = "in_progress"
                gallery_state["completed_at"] = None
                any_downloaded = True
                all_complete = False
            else:
                gallery_state["status"] = "completed"
                gallery_state["completed_at"] = gallery_state["completed_at"] or utc_now()
                any_downloaded = True

        if all_complete:
            show_state["status"] = "completed"
            show_state["completed_at"] = show_state["completed_at"] or utc_now()
            show_state["last_error"] = None
            show_state.pop("failure_kind", None)
        elif any_downloaded or show_state["metadata_saved"]:
            show_state["status"] = "in_progress"
            show_state["completed_at"] = None
        else:
            show_state["status"] = "pending"
            show_state["completed_at"] = None

        return copy.deepcopy(show_state)

    def mark_show_failed(self, designer, designer_slug, show, show_folder, show_url, kind, error):
        designer_state, show_state = self.ensure_show(designer, designer_slug, show, show_folder, show_url)
        timestamp = utc_now()
        designer_state["last_attempted_at"] = timestamp
        designer_state["last_error"] = error
        show_state["last_attempted_at"] = timestamp
        show_state["last_error"] = error
        show_state["failure_kind"] = kind
        if show_state["status"] != "completed":
            show_state["status"] = "failed"
        self.save()

    def mark_designer_pass(self, designer_slug, url_slug, show_folders):
        designer_state = self.data["designers"][designer_slug]
        designer_state["url_slug"] = url_slug
        designer_state["shows_listed"] = len(show_folders)
        designer_state["last_listed_at"] = utc_now()
        shows = designer_state["shows"]
        # Completion is only decided here, after the full show list was walked: deciding it
        # from the shows seen so far marked designers done when a run stopped between shows.
        if all(shows.get(folder, {}).get("status") == "completed" for folder in show_folders):
            designer_state["status"] = "completed"
            designer_state["completed_at"] = designer_state["completed_at"] or utc_now()
            designer_state["last_error"] = None
        else:
            designer_state["status"] = "in_progress"
            designer_state["completed_at"] = None
        self.save()

    def mark_designer_not_found(self, designer, designer_slug, error):
        designer_state = self.ensure_designer(designer, designer_slug)
        designer_state["status"] = "not_found"
        designer_state["last_attempted_at"] = utc_now()
        designer_state["last_error"] = error
        designer_state["completed_at"] = None
        self.save()

    def mark_designer_error(self, designer, designer_slug, error):
        designer_state = self.ensure_designer(designer, designer_slug)
        designer_state["last_attempted_at"] = utc_now()
        designer_state["last_error"] = error
        if designer_state["status"] != "completed":
            designer_state["status"] = "in_progress"
        designer_state["completed_at"] = None
        self.save()

    def _ensure_show_data(self, show_data):
        return self.ensure_show(
            show_data["designer"],
            show_data["designer_slug"],
            show_data["show"],
            show_data["show_folder"],
            show_data["show_url"],
        )


class FailureLog:
    def __init__(self, path):
        self.path = path

    def add(self, kind, designer, error, show=None, url=None):
        entry = {
            "at": utc_now(),
            "kind": kind,
            "designer": designer,
            "show": show,
            "url": url,
            "error": error,
        }
        with open(self.path, "a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def extract_json_from_script(scripts, key_fragment):
    for script in scripts:
        if script.string and key_fragment in script.string:
            js = script.string
            break
    else:
        return None

    try:
        start = js.index("{", js.index(" = "))
        # raw_decode stops at the end of the object, so braces inside review text can't break it.
        data, _ = json.JSONDecoder().raw_decode(js, start)
        return data
    except ValueError as e:
        print(f"    JSON extraction failed: {e}")
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


def designer_folder_slug(designer):
    # Kept equal to the original slug so existing folders and state keys stay valid.
    return normalize_slug(designer).replace("/", "-")


def name_key(name):
    return re.sub(r"[^a-z0-9]", "", unidecode(name).lower())


def candidate_slugs(designer):
    base = unidecode(designer).lower().strip()
    variants = [
        re.sub(r"['’]", "", base),
        base,
        re.sub(r"['’]", "", base).replace("&", " and "),
    ]
    candidates = []
    for variant in variants:
        slug = re.sub(r"[^a-z0-9]+", "-", variant).strip("-")
        if slug and slug not in candidates:
            candidates.append(slug)
    # Vogue keeps the dash left by a trailing period ("Agnès B." -> agnes-b-).
    if base.endswith(".") and candidates and f"{candidates[0]}-" not in candidates:
        candidates.append(f"{candidates[0]}-")
    legacy = normalize_slug(designer)
    if legacy not in candidates and re.fullmatch(r"[a-z0-9-]+", legacy):
        candidates.append(legacy)
    return candidates


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


class AdaptiveLimiter:
    def __init__(self, start, minimum, maximum, grow_after):
        self.limit = start
        self.minimum = minimum
        self.maximum = maximum
        self.grow_after = grow_after
        self.ceiling = maximum
        self.active = 0
        self.successes = 0
        self.cooldown_until = 0.0
        self.throttle_events = 0
        self.closed = False
        self.level_rates = {}
        self._condition = threading.Condition()
        self._reset_window()

    def _reset_window(self):
        self.successes = 0
        self._window_started = time.monotonic()
        self._idle_since = self._window_started if self.active == 0 else None

    def __enter__(self):
        with self._condition:
            while True:
                if self.closed:
                    raise RuntimeError("limiter closed")
                wait = self.cooldown_until - time.monotonic()
                if wait > 0:
                    self._condition.wait(min(wait, 1.0))
                elif self.active < self.limit:
                    break
                else:
                    self._condition.wait(1.0)
            # Gaps between galleries (page fetches, rests) aren't download time; leaving
            # them in would make every level look slower than it is.
            if self.active == 0 and self._idle_since is not None:
                self._window_started += time.monotonic() - self._idle_since
                self._idle_since = None
            self.active += 1
        return self

    def __exit__(self, *exc_info):
        with self._condition:
            self.active -= 1
            if self.active == 0:
                self._idle_since = time.monotonic()
            self._condition.notify_all()

    def on_success(self):
        with self._condition:
            self.successes += 1
            if self.successes < self.grow_after:
                return
            elapsed = max(time.monotonic() - self._window_started, 1e-6)
            self.level_rates[self.limit] = self.successes / elapsed
            previous = self.level_rates.get(self.limit - 1)
            if previous and self.level_rates[self.limit] < previous * (1 + PLATEAU_GAIN):
                self.ceiling = self.limit - 1
                self.limit = self.ceiling
            elif self.limit < self.ceiling:
                self.limit += 1
            self._reset_window()
            self._condition.notify_all()

    def on_throttle(self, retry_after=None):
        with self._condition:
            now = time.monotonic()
            # In-flight requests fail together when throttled; one burst is one event.
            if now < self.cooldown_until:
                return
            self.throttle_events += 1
            self.ceiling = max(self.minimum, self.limit - 1)
            self.limit = max(self.minimum, self.limit // 2)
            self.cooldown_until = now + max(retry_after or 0, THROTTLE_PAUSE)
            self._reset_window()
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {
                "limit": self.limit,
                "ceiling": self.ceiling,
                "images_per_second": {str(level): round(rate, 2) for level, rate in sorted(self.level_rates.items())},
                "saved_at": time.time(),
            }

    def restore(self, snapshot):
        with self._condition:
            if time.time() - snapshot.get("saved_at", 0) < CEILING_MAX_AGE:
                self.ceiling = min(self.maximum, max(self.minimum, snapshot.get("ceiling", self.maximum)))
            self.limit = min(self.ceiling, max(self.minimum, snapshot.get("limit", self.limit)))
            self._reset_window()

    def close(self):
        with self._condition:
            self.closed = True
            self._condition.notify_all()


IMAGE_LIMITER = AdaptiveLimiter(IMAGE_WORKERS_START, 1, IMAGE_WORKERS_MAX, IMAGE_GROW_AFTER)


def parse_retry_after(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ReportingRetry(Retry):
    # urllib3 retries 429/503 silently; surfacing them is the only way to know a page
    # request was throttled, and images should slow down too when that happens.
    def increment(self, method=None, url=None, response=None, error=None, *args, **kwargs):
        if response is not None and response.status in THROTTLE_STATUSES:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            print(f"\n    ⇣ Vogue returned {response.status} for a page; backing off")
            IMAGE_LIMITER.on_throttle(retry_after)
        return super().increment(method, url, response, error, *args, **kwargs)


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

        retry = ReportingRetry(
            total=5,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Images handle throttling themselves (see _download_single_image), so this
        # session only retries plain server errors.
        self.image_session = requests.Session()
        image_retry = Retry(total=2, backoff_factor=1, status_forcelist=(500, 502, 504), allowed_methods=("GET",))
        image_adapter = HTTPAdapter(
            max_retries=image_retry, pool_connections=IMAGE_WORKERS_MAX, pool_maxsize=IMAGE_WORKERS_MAX,
        )
        self.image_session.mount("https://", image_adapter)

    def _sleep(self, profile):
        minimum, maximum = REQUEST_PROFILES[profile]
        delay = random.uniform(minimum, maximum)

        elapsed = time.monotonic() - self.last_request_at if self.last_request_at else None
        if elapsed is None or elapsed >= delay:
            return

        time.sleep(delay - elapsed)

    def get(self, url, profile="page", referer=None, timeout=60):
        self._sleep(profile)
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer or BASE_URL,
        }
        try:
            response = self.session.get(url, headers=headers, timeout=timeout)
        finally:
            self.last_request_at = time.monotonic()
        response.raise_for_status()
        return response

    def rest(self, profile):
        minimum, maximum = REQUEST_PROFILES[profile]
        time.sleep(random.uniform(minimum, maximum))


CLIENT = VogueClient()


def fetch_soup(url, profile="page", referer=None):
    try:
        response = CLIENT.get(url, profile=profile, referer=referer)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise NotFound(url) from e
        raise
    return BeautifulSoup(response.content, "html5lib")


def read_designers(txt_path):
    with open(txt_path, "r", encoding="utf-8") as file:
        names = [line.strip() for line in file if line.strip()]
    return list(dict.fromkeys(names))


def get_state_path(save_path, state_path=None):
    return state_path or os.path.join(save_path, "_scrape_state.json")


def resolve_save_path(save_path=None):
    save_path = save_path or DEFAULT_SAVE_PATH
    parts = Path(save_path).parts
    # Without this check, an unmounted drive surfaces as a PermissionError deep inside makedirs.
    if len(parts) > 2 and parts[1] == "Volumes" and not os.path.isdir(os.path.join(*parts[:3])):
        raise SystemExit(f"Drive not mounted: {os.path.join(*parts[:3])}")
    return save_path


def fetch_designer_directory():
    html = CLIENT.get(DIRECTORY_URL, profile="page").text
    pairs = re.findall(
        r'\{"text":"((?:[^"\\]|\\.)*)","url":"\\u002Ffashion-shows\\u002Fdesigner\\u002F([^"\\]+)"',
        html,
    )
    if not pairs:
        raise ValueError("designer directory page had no designer links")
    return [{"name": json.loads(f'"{name}"'), "slug": slug} for name, slug in pairs]


def load_designer_directory(cache_path):
    entries = None
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as file:
            entries = json.load(file)
    fresh = os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < DIRECTORY_MAX_AGE

    if not fresh:
        try:
            entries = fetch_designer_directory()
            with open(cache_path, "w", encoding="utf-8") as file:
                json.dump(entries, file, ensure_ascii=False)
            print(f"Designer directory: {len(entries)} designers")
        except Exception as e:
            print(f"Could not refresh designer directory ({e}); using {'cache' if entries else 'guessed slugs'}")

    index = collections.defaultdict(list)
    for entry in entries or []:
        slugs = index[name_key(entry["name"])]
        if entry["slug"] not in slugs:
            slugs.append(entry["slug"])
    return index


def parse_designer_collections(soup):
    data = extract_json_from_script(
        soup.find_all("script", type="text/javascript"),
        "window.__PRELOADED_STATE__",
    )
    if not data:
        raise ValueError("designer page has no preloaded state")
    items = data["transformed"]["runwayDesignerContent"]["designerCollections"]
    return [
        {"show": item["hed"], "url": urljoin(BASE_URL, item["url"])}
        for item in items
        if item.get("hed") and item.get("url")
    ]


def get_show_data(designer, show, show_url=None, designer_slug=None, fetch=fetch_soup):
    show_folder = normalize_show_folder(show)
    designer_slug = designer_slug or designer_folder_slug(designer)
    url = show_url or f"{BASE_URL}/fashion-shows/{normalize_slug(show)}/{designer_slug}"
    show_slug = urlparse(url).path.rstrip("/").split("/")[-2]
    soup = fetch(url, referer=f"{BASE_URL}/fashion-shows/designer/{designer_slug}")

    data = extract_json_from_script(
        soup.find_all("script", type="text/javascript"),
        "runwayShowGalleries",
    )
    if not data:
        return None

    try:
        galleries = data["transformed"]["runwayShowGalleries"]["galleries"]
    except (KeyError, TypeError):
        return None

    show_description = extract_show_description(soup)
    records = []
    gallery_records = {}

    for gallery in galleries:
        gallery_title = gallery.get("title") or "Collection"
        gallery_slug = slugify_filename(gallery_title)
        if gallery_slug not in ALLOWED_GALLERIES:
            continue

        gallery_images = []
        for image_index, item in enumerate(gallery.get("items", []), start=1):
            try:
                image_url = item["image"]["sources"]["md"]["url"]
            except (KeyError, TypeError):
                continue

            record = {
                "designer": designer,
                "show": show,
                "gallery": gallery_slug,
                "show_description": show_description,
                "image_index": image_index,
                "image_name": build_image_name(gallery_slug, image_index, image_url),
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


def _download_single_image(session, record, gallery_path, referer, limiter=IMAGE_LIMITER):
    export_path = os.path.join(gallery_path, record["image_name"])
    if is_downloaded(export_path):
        return record, "skipped"

    headers = {"Referer": referer}
    status = "error: not attempted"
    for _ in range(IMAGE_ATTEMPTS):
        with limiter:
            time.sleep(random.uniform(*IMAGE_DELAY))
            headers["User-Agent"] = random.choice(USER_AGENTS)
            try:
                response = session.get(record["image_url"], headers=headers, timeout=120)
            except (requests.ConnectionError, requests.Timeout) as e:
                # Dropped connections were how the CDN pushed back in the last full run.
                limiter.on_throttle()
                status = f"error: {e}"
                continue

        if response.status_code in THROTTLE_STATUSES:
            limiter.on_throttle(parse_retry_after(response.headers.get("Retry-After")))
            status = f"error: HTTP {response.status_code} (throttled)"
            continue
        if not response.ok:
            return record, f"error: HTTP {response.status_code} for {record['image_url']}"

        content_type = response.headers.get("Content-Type", "")
        # A throttling or error page served with 200 would otherwise be saved as a .jpg
        # and count as done forever.
        if not content_type.startswith("image/") or not response.content:
            return record, f"error: not an image ({content_type or 'no content type'}, {len(response.content)} bytes)"
        temp_path = export_path + ".tmp"
        with open(temp_path, "wb") as file:
            file.write(response.content)
        os.replace(temp_path, export_path)
        limiter.on_success()
        return record, "downloaded"
    return record, status


class Scraper:
    def __init__(self, save_path=None, state_path=None):
        self.save_path = resolve_save_path(save_path)
        os.makedirs(self.save_path, exist_ok=True)
        self.state = ScrapeState(get_state_path(self.save_path, state_path))
        self.failures = FailureLog(os.path.join(self.save_path, "_failures.jsonl"))
        self.pacing_path = os.path.join(self.save_path, "_pacing.json")
        if os.path.exists(self.pacing_path):
            with open(self.pacing_path, "r", encoding="utf-8") as file:
                IMAGE_LIMITER.restore(json.load(file))
        self._directory = None
        self._canary_ok_at = 0.0

    def directory(self):
        if self._directory is None:
            self._directory = load_designer_directory(
                os.path.join(self.save_path, "_designer_directory.json")
            )
        return self._directory

    def check_not_blocked(self):
        if time.monotonic() - self._canary_ok_at < 120:
            return
        try:
            CLIENT.get(CANARY_URL, profile="page")
        except Exception as e:
            raise Blocked(f"canary page failed too: {e}") from e
        self._canary_ok_at = time.monotonic()

    def fetch(self, url, referer=None):
        try:
            return fetch_soup(url, referer=referer)
        except NotFound:
            self.check_not_blocked()
            raise
        except (requests.ConnectionError, requests.exceptions.RetryError, requests.Timeout):
            self.check_not_blocked()
            raise

    def fetch_collections(self, url_slug):
        return parse_designer_collections(self.fetch(f"{BASE_URL}/fashion-shows/designer/{url_slug}"))

    def find_designer(self, designer, designer_state):
        tried = []

        def attempt(slug):
            tried.append(slug)
            try:
                return self.fetch_collections(slug)
            except NotFound:
                return None

        cached = designer_state.get("url_slug")
        if cached:
            found = attempt(cached)
            if found is not None:
                return cached, found

        matches = [slug for slug in self.directory().get(name_key(designer), []) if slug not in tried]
        if len(matches) > 1:
            # The directory lists near-duplicates (viktor-rolf / viktorandrolf); the real
            # runway page is the one with the most shows.
            results = [(slug, attempt(slug)) for slug in matches]
            results = [result for result in results if result[1] is not None]
            if results:
                return max(results, key=lambda result: len(result[1]))

        for slug in matches + candidate_slugs(designer):
            if slug in tried:
                continue
            found = attempt(slug)
            if found is not None:
                return slug, found

        raise NotFound(f"no Vogue designer page (tried: {', '.join(tried)})")

    def download_designer(self, designer, progress=None, only_show=None):
        designer_slug = designer_folder_slug(designer)
        designer_state = self.state.ensure_designer(designer, designer_slug)

        try:
            url_slug, shows = self.find_designer(designer, designer_state)
        except NotFound as e:
            self.state.mark_designer_not_found(designer, designer_slug, str(e))
            self.failures.add("designer_not_found", designer, str(e))
            print(f"  ✗ {e}")
            return

        if only_show:
            wanted = name_key(only_show)
            shows = [entry for entry in shows if name_key(entry["show"]) == wanted]
            if not shows:
                print(f"  ✗ {designer} has no show called {only_show!r}")
                return

        progress = progress or {}
        progress["total_shows"] = len(shows)
        show_folders = [normalize_show_folder(entry["show"]) for entry in shows]
        pending = [
            (index, entry)
            for index, (entry, folder) in enumerate(zip(shows, show_folders), start=1)
            if designer_state["shows"].get(folder, {}).get("status") != "completed"
        ]
        done = len(shows) - len(pending)
        if done:
            print(f"  {done}/{len(shows)} shows already complete")

        for position, (index, entry) in enumerate(pending):
            if position:
                CLIENT.rest("show")
            progress["show_index"] = index
            show_folder = normalize_show_folder(entry["show"])
            try:
                self.download_show(designer, designer_slug, entry, progress)
            except Blocked:
                raise
            except NotFound as e:
                self._show_failed(designer, designer_slug, entry, show_folder, "show_not_found", f"404: {e}")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self._show_failed(designer, designer_slug, entry, show_folder, "show_failed", str(e))

        if only_show:
            self.state.save()
        else:
            self.state.mark_designer_pass(designer_slug, url_slug, show_folders)

    def _show_failed(self, designer, designer_slug, entry, show_folder, kind, error):
        print(f"\n  ▸ {entry['show']}\n    ✗ {kind}: {error}")
        self.state.mark_show_failed(
            designer, designer_slug, entry["show"], show_folder, entry["url"], kind, error
        )
        self.failures.add(kind, designer, error, show=entry["show"], url=entry["url"])

    def download_show(self, designer, designer_slug, entry, progress=None):
        show = entry["show"]
        show_pos = ""
        if progress and "show_index" in progress:
            show_pos = f"  ({progress['show_index']}/{progress['total_shows']})"
        print(f"\n  ▸ {show}{show_pos}")

        show_data = get_show_data(
            designer, show, show_url=entry["url"], designer_slug=designer_slug, fetch=self.fetch,
        )
        if not show_data:
            self._show_failed(
                designer, designer_slug, entry, normalize_show_folder(show),
                "show_unparseable", "page has no runway gallery data",
            )
            return

        show_path = os.path.join(self.save_path, designer_slug, show_data["show_folder"])
        os.makedirs(show_path, exist_ok=True)

        self.state.mark_show_started(show_data)
        write_show_metadata(show_path, show_data)

        if not show_data["images"]:
            print("    ░ no collection/details images")
            self.state.sync_show(show_data, show_path)
            self.state.save()
            return

        errors = []
        total_images = len(show_data["images"])
        throttles_before = IMAGE_LIMITER.throttle_events

        for gallery_name, gallery_images in show_data["galleries"].items():
            gallery_path = os.path.join(show_path, gallery_name)
            os.makedirs(gallery_path, exist_ok=True)
            gallery_total = len(gallery_images)
            gallery_done = 0
            gallery_errors = 0

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_WORKERS_MAX)
            try:
                futures = [
                    executor.submit(
                        _download_single_image, CLIENT.image_session, record, gallery_path, show_data["show_url"],
                    )
                    for record in gallery_images
                ]
                for future in concurrent.futures.as_completed(futures):
                    record, status = future.result()
                    gallery_done += 1
                    if status.startswith("error:"):
                        gallery_errors += 1
                        errors.append((record, status))
                    bar = _progress_bar(gallery_done, gallery_total)
                    pace = "paused" if IMAGE_LIMITER.cooldown_until > time.monotonic() else f"×{IMAGE_LIMITER.limit}"
                    line = f"\r    {gallery_name:<12} {bar}  {gallery_done}/{gallery_total}  {pace}"
                    sys.stdout.write(f"{line:<60}")
                    sys.stdout.flush()
            except KeyboardInterrupt:
                # Without cancelling, Ctrl-C waits for every queued image in the gallery.
                executor.shutdown(wait=False, cancel_futures=True)
                IMAGE_LIMITER.close()
                raise
            executor.shutdown()

            bar = _progress_bar(gallery_done, gallery_total)
            suffix = f"⚠ {gallery_errors} failed" if gallery_errors else "✓"
            print(f"\r    {gallery_name:<12} {bar}  {gallery_done}/{gallery_total}  {suffix}")

        throttles = IMAGE_LIMITER.throttle_events - throttles_before
        if throttles:
            print(f"    ⇣ throttled {throttles}× during this show; now ×{IMAGE_LIMITER.limit} workers")
            self.failures.add(
                "throttled", designer, f"{throttles} throttle event(s), workers now {IMAGE_LIMITER.limit}",
                show=show, url=show_data["show_url"],
            )

        with open(self.pacing_path, "w", encoding="utf-8") as file:
            json.dump(IMAGE_LIMITER.snapshot(), file, indent=2)

        self.state.sync_show(show_data, show_path)
        if errors:
            first_error = errors[0][1][len("error: "):]
            print(f"    ✗ {len(errors)} image(s) failed, e.g. {first_error[:120]}")
            self.state.mark_show_failed(
                designer, designer_slug, show, show_data["show_folder"], show_data["show_url"],
                "images_failed", f"{len(errors)} image(s) failed: {first_error}",
            )
            self.failures.add(
                "images_failed", designer, f"{len(errors)} image(s) failed: {first_error}",
                show=show, url=show_data["show_url"],
            )
        else:
            self.state.save()

        show_state = self.state.data["designers"][designer_slug]["shows"][show_data["show_folder"]]
        completed_images = sum(gallery["downloaded_images"] for gallery in show_state["galleries"].values())
        status_icon = "✓" if completed_images == total_images else "⚠"
        designer_pos = ""
        if progress and "designer_index" in progress:
            designer_pos = f" │ designer {progress['designer_index']}/{progress['total_designers']}"
        print(f"[state: {designer} ▸ {show} │ {status_icon} {completed_images}/{total_images}{designer_pos}]")

    def download_designers(self, designers, refresh=False, retry_failed=False):
        def needs_work(designer):
            designer_state = self.state.get_designer(designer_folder_slug(designer))
            status = designer_state["status"] if designer_state else "pending"
            if retry_failed:
                return status != "completed"
            if status == "completed":
                return refresh
            # Unresolvable designers cost several requests each; only retry them on request.
            return status != "not_found"

        todo = [designer for designer in designers if needs_work(designer)]
        skipped = len(designers) - len(todo)
        print(f"State file: {self.state.path}")
        print(f"Failure log: {self.failures.path}")
        print(f"Image pace: ×{IMAGE_LIMITER.limit} (ceiling ×{IMAGE_LIMITER.ceiling})")
        if skipped:
            print(f"Skipping {skipped} designers (completed or not found). {len(todo)} to go.")

        for designer_index, designer in enumerate(todo, start=1):
            label = f" {designer} "
            pos = f"[{designer_index}/{len(todo)}]"
            fill = max(1, 52 - len(label) - len(pos))
            print(f"\n━━{label}{'━' * fill} {pos}")

            progress = {
                "designer": designer,
                "designer_index": designer_index,
                "total_designers": len(todo),
            }
            for attempt in range(MAX_BLOCK_RETRIES + 1):
                try:
                    self.download_designer(designer, progress)
                    break
                except Blocked as e:
                    self.failures.add("blocked", designer, str(e))
                    if attempt == MAX_BLOCK_RETRIES:
                        raise
                    print(f"  ⏸ Vogue is throttling us; cooling down {BLOCK_COOLDOWN // 60} min")
                    time.sleep(BLOCK_COOLDOWN)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self.state.mark_designer_error(designer, designer_folder_slug(designer), str(e))
                    self.failures.add("designer_failed", designer, str(e))
                    print(f"  ✗ Failed: {e}")
                    break

            if designer_index < len(todo):
                CLIENT.rest("designer")


def classify_failure(error):
    error = error or ""
    if "SSLError" in error or "CERTIFICATE_VERIFY_FAILED" in error:
        return "ssl_error"
    if "assets.vogue.com" in error or "image(s) failed" in error:
        return "images_failed"
    if "Max retries" in error or "Connection" in error:
        return "request_failed"
    if "fashion-shows/designer/" in error and "404" in error:
        return "designer_not_found"
    if "Not Found" in error or error.startswith("404"):
        return "show_not_found"
    return "unknown"


def build_report(state_data, designers=None):
    lines = []
    designer_states = state_data["designers"]
    if designers is not None:
        wanted = {designer_folder_slug(designer) for designer in designers}
        designer_states = {slug: value for slug, value in designer_states.items() if slug in wanted}

    statuses = collections.Counter(value["status"] for value in designer_states.values())
    total = len(designers) if designers is not None else len(designer_states)
    lines.append(
        f"Designers: {total} total · {statuses['completed']} completed · "
        f"{statuses['not_found']} not found · {statuses['in_progress']} incomplete · "
        f"{total - sum(statuses.values()) + statuses['pending']} not started"
    )

    groups = collections.defaultdict(list)
    for value in sorted(designer_states.values(), key=lambda item: item["designer"]):
        if value["status"] == "completed":
            continue
        if value["status"] == "not_found":
            groups["designer_not_found"].append(f"{value['designer']}  ({value['last_error']})")
            continue
        failed_shows = [show for show in value["shows"].values() if show["status"] != "completed"]
        if not failed_shows:
            kind = classify_failure(value.get("last_error"))
            if kind == "unknown":
                kind = "interrupted"
            if value["status"] != "pending" or value.get("last_error"):
                groups[kind].append(f"{value['designer']}  ({value.get('last_error') or 'interrupted'})")
            continue
        for show in failed_shows:
            kind = show.get("failure_kind") or classify_failure(show.get("last_error"))
            if kind == "unknown" and not show.get("last_error"):
                kind = "no_images" if not show["galleries"] else "interrupted"
            groups[kind].append(f"{value['designer']} ▸ {show['show']}  ({show.get('last_error') or show['status']})")

    for kind, entries in sorted(groups.items(), key=lambda item: -len(item[1])):
        lines.append("")
        lines.append(f"{kind} ({len(entries)})")
        lines.extend(f"  {entry[:200]}" for entry in entries)
    return "\n".join(lines)


def designer_to_collections(designer):
    for slug in candidate_slugs(designer):
        try:
            return parse_designer_collections(fetch_soup(f"{BASE_URL}/fashion-shows/designer/{slug}"))
        except NotFound:
            continue
    print(f"No Vogue designer page found for {designer}")
    return []


def designer_to_shows(designer):
    return [entry["show"] for entry in designer_to_collections(designer)]


def designer_show_to_download_images(designer, show, save_path=None, state_path=None, progress=None):
    Scraper(save_path, state_path).download_designer(designer, progress, only_show=show)


def designer_to_download_images(designer, save_path=None, state_path=None, progress=None):
    Scraper(save_path, state_path).download_designer(designer, progress)


def all_designers_to_download_images(txt_path, save_path=None, state_path=None, refresh=False, retry_failed=False):
    try:
        designers = read_designers(txt_path)
    except OSError as e:
        print(f"Error reading file {txt_path}: {e}")
        return
    if not designers:
        print("No designers found in the file.")
        return
    Scraper(save_path, state_path).download_designers(designers, refresh=refresh, retry_failed=retry_failed)


def designer_show_to_csv(designer, show, save_path=None, show_url=None):
    csv_path = None
    if save_path:
        os.makedirs(save_path, exist_ok=True)
        csv_path = os.path.join(
            save_path,
            f"{designer_folder_slug(designer)}_{normalize_show_folder(show)}.csv",
        )
        if os.path.exists(csv_path):
            print(f"CSV already exists: {csv_path}")
            return None

    if not show_url:
        wanted = name_key(show)
        show_url = next(
            (entry["url"] for entry in designer_to_collections(designer) if name_key(entry["show"]) == wanted),
            None,
        )
    show_data = get_show_data(designer, show, show_url=show_url)
    if not show_data:
        print(f"Could not load show: {designer} - {show}")
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
    os.makedirs(save_path, exist_ok=True)
    csv_path = os.path.join(save_path, f"{designer_folder_slug(designer)}_all_shows.csv")

    if os.path.exists(csv_path):
        print(f"CSV already exists: {csv_path}")
        return

    shows = designer_to_collections(designer)
    if not shows:
        print(f"No shows found for {designer}")
        return

    all_rows = []
    for index, entry in enumerate(shows, start=1):
        print(f"Scraping [{index}/{len(shows)}] {designer} - {entry['show']}")
        try:
            rows = designer_show_to_csv(designer, entry["show"], show_url=entry["url"])
        except NotFound:
            print(f"Show page not found: {entry['url']}")
            rows = None
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
        designers = read_designers(txt_path)
    except OSError as e:
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
                shows = designer_to_collections(designer)
                for show_index, entry in enumerate(shows, start=1):
                    show = entry["show"]
                    if (designer, show) in existing_rows:
                        print(f"Already scraped: {designer} - {show}")
                        continue

                    print(f"Scraping [{show_index}/{len(shows)}] {designer} - {show}")
                    rows = designer_show_to_csv(designer, show, show_url=entry["url"])
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="vogue.py",
        description="Resumable Vogue Runway image scraper. Rerun any command to resume.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    download_all = commands.add_parser("download-all", help="download every designer in a list")
    download_all.add_argument("designers_file", nargs="?", default="designers.txt")
    download_all.add_argument("save_path", nargs="?")
    download_all.add_argument(
        "--refresh", action="store_true",
        help="revisit completed designers to pick up new collections",
    )

    retry = commands.add_parser("retry-failed", help="retry only designers that failed or are incomplete")
    retry.add_argument("designers_file", nargs="?", default="designers.txt")
    retry.add_argument("save_path", nargs="?")

    designer = commands.add_parser("download-designer", help="download all shows of one designer")
    designer.add_argument("designer")
    designer.add_argument("save_path", nargs="?")

    show = commands.add_parser("download-show", help="download one show")
    show.add_argument("designer")
    show.add_argument("show", help='show title as on Vogue, e.g. "Spring 2018 Ready-to-Wear"')
    show.add_argument("save_path", nargs="?")

    report = commands.add_parser("report", help="summarize what failed or is still incomplete")
    report.add_argument("designers_file", nargs="?", default="designers.txt")
    report.add_argument("save_path", nargs="?")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        if args.command == "download-all":
            all_designers_to_download_images(args.designers_file, args.save_path, refresh=args.refresh)
        elif args.command == "retry-failed":
            all_designers_to_download_images(args.designers_file, args.save_path, retry_failed=True)
        elif args.command == "download-designer":
            designer_to_download_images(args.designer, args.save_path)
        elif args.command == "download-show":
            designer_show_to_download_images(args.designer, args.show, args.save_path)
        elif args.command == "report":
            save_path = resolve_save_path(args.save_path)
            state = ScrapeState(get_state_path(save_path))
            designers = read_designers(args.designers_file) if os.path.exists(args.designers_file) else None
            print(build_report(state.data, designers))
    except KeyboardInterrupt:
        print("\n\nStopped. Run the same command again to resume.")
        return 130
    except Blocked as e:
        print(f"\n\nVogue kept blocking requests ({e}). Stopped; rerun later to resume.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
