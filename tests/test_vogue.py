import json
import os
import sys

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vogue  # noqa: E402


def make_show_data(galleries):
    return {
        "designer": "MIU MIU",
        "designer_slug": "miu-miu",
        "show": "Fall 2026 Ready-to-Wear",
        "show_slug": "fall-2026-ready-to-wear",
        "show_folder": "2026-fall-ready-to-wear",
        "show_description": "",
        "show_url": "https://www.vogue.com/fashion-shows/fall-2026-ready-to-wear/miu-miu",
        "galleries": {
            name: [
                {"image_index": index, "image_name": f"look_{index:04d}.jpg"}
                for index in range(1, count + 1)
            ]
            for name, count in galleries.items()
        },
        "images": [],
    }


@pytest.fixture
def state(tmp_path):
    return vogue.ScrapeState(str(tmp_path / "state.json"))


def write_images(show_path, gallery, count):
    os.makedirs(show_path / gallery, exist_ok=True)
    for index in range(1, count + 1):
        (show_path / gallery / f"look_{index:04d}.jpg").write_bytes(b"jpg")


def test_empty_show_counts_as_completed(state, tmp_path):
    show_state = state.sync_show(make_show_data({}), str(tmp_path))
    assert show_state["status"] == "completed"
    assert show_state["empty"] is True


def test_zero_byte_image_is_not_downloaded(state, tmp_path):
    os.makedirs(tmp_path / "collection")
    (tmp_path / "collection" / "look_0001.jpg").write_bytes(b"")
    show_state = state.sync_show(make_show_data({"collection": 1}), str(tmp_path))
    assert show_state["status"] != "completed"


def test_designer_not_completed_until_all_listed_shows_done(state, tmp_path):
    write_images(tmp_path, "collection", 2)
    state.sync_show(make_show_data({"collection": 2}), str(tmp_path))
    state.mark_designer_pass("miu-miu", "miu-miu", ["2026-fall-ready-to-wear", "2026-spring-ready-to-wear"])
    assert state.get_designer("miu-miu")["status"] == "in_progress"

    state.mark_designer_pass("miu-miu", "miu-miu", ["2026-fall-ready-to-wear"])
    assert state.get_designer("miu-miu")["status"] == "completed"


def test_failed_show_is_recorded_with_kind(state):
    state.mark_show_failed("ALAÏA", "alaia", "Fall 2026", "2026-fall", "https://x", "show_not_found", "404")
    show = state.get_designer("alaia")["shows"]["2026-fall"]
    assert (show["status"], show["failure_kind"]) == ("failed", "show_not_found")


def test_extract_json_survives_braces_in_text():
    payload = {"transformed": {"body": "a {weird} review } with braces {"}}
    html = f'<script type="text/javascript">window.__PRELOADED_STATE__ = {json.dumps(payload)};</script>'
    scripts = BeautifulSoup(html, "html.parser").find_all("script")
    assert vogue.extract_json_from_script(scripts, "__PRELOADED_STATE__") == payload


@pytest.mark.parametrize(
    "designer, expected",
    [
        ("AGNÈS B.", "agnes-b-"),
        ("Y/PROJECT", "y-project"),
        ("MARQUES’ALMEIDA", "marques-almeida"),
        ("ALESSANDRO DELL'ACQUA", "alessandro-dellacqua"),
        ("VIKTOR & ROLF", "viktor-and-rolf"),
    ],
)
def test_candidate_slugs_cover_vogue_conventions(designer, expected):
    assert expected in vogue.candidate_slugs(designer)


def test_candidate_slugs_never_contain_path_characters():
    for designer in ["KUDOS / SODUK", "WHO IS IT?", "NUMBER (N)INE", "S=YZ"]:
        for slug in vogue.candidate_slugs(designer):
            assert all(char.isalnum() or char == "-" for char in slug), slug


def test_read_designers_dedupes(tmp_path):
    path = tmp_path / "designers.txt"
    path.write_text("A\nB\n\nA\n", encoding="utf-8")
    assert vogue.read_designers(str(path)) == ["A", "B"]


class FakeScraper(vogue.Scraper):
    def __init__(self, tmp_path, pages, directory):
        super().__init__(str(tmp_path), None)
        self.pages = pages
        self._directory = directory
        self.requested = []

    def fetch_collections(self, url_slug):
        self.requested.append(url_slug)
        if url_slug not in self.pages:
            raise vogue.NotFound(url_slug)
        return self.pages[url_slug]


def test_find_designer_prefers_directory_slug(tmp_path):
    scraper = FakeScraper(tmp_path, {"burberry-prorsum": [{"show": "s", "url": "u"}]}, {"burberry": ["burberry-prorsum"]})
    slug, _ = scraper.find_designer("BURBERRY", {})
    assert slug == "burberry-prorsum"
    assert scraper.requested == ["burberry-prorsum"]


def test_find_designer_picks_richest_duplicate(tmp_path):
    pages = {"viktorandrolf": [{"show": "a", "url": "u"}], "viktor-rolf": [{"show": "a", "url": "u"}] * 50}
    scraper = FakeScraper(tmp_path, pages, {"viktorrolf": ["viktorandrolf", "viktor-rolf"]})
    slug, _ = scraper.find_designer("VIKTOR & ROLF", {})
    assert slug == "viktor-rolf"


def test_find_designer_raises_not_found_with_tried_slugs(tmp_path):
    scraper = FakeScraper(tmp_path, {}, {})
    with pytest.raises(vogue.NotFound, match="tried: ghost"):
        scraper.find_designer("GHOST", {})


def test_report_groups_failures(state):
    state.mark_designer_not_found("BURBERRY", "burberry", "no Vogue designer page")
    state.mark_show_failed("ALAÏA", "alaia", "Fall 2026", "2026-fall", "https://x", "show_not_found", "404")
    report = vogue.build_report(state.data)
    assert "designer_not_found (1)" in report
    assert "show_not_found (1)" in report


def test_limiter_grows_on_success_and_halves_on_throttle(monkeypatch):
    monkeypatch.setattr(vogue, "THROTTLE_PAUSE", 0)
    limiter = vogue.AdaptiveLimiter(start=3, minimum=1, maximum=8, grow_after=2)
    for _ in range(4):
        limiter.on_success()
    assert limiter.limit == 5

    limiter.on_throttle()
    assert (limiter.limit, limiter.ceiling, limiter.throttle_events) == (2, 4, 1)


def drive(limiter, monkeypatch, seconds_per_success):
    clock = [1000.0]
    monkeypatch.setattr(vogue.time, "monotonic", lambda: clock[0])
    limiter._reset_window()

    def succeed():
        clock[0] += seconds_per_success(limiter.limit)
        limiter.on_success()

    return succeed


def test_limiter_stops_growing_when_throughput_plateaus(monkeypatch):
    limiter = vogue.AdaptiveLimiter(start=3, minimum=1, maximum=12, grow_after=10)
    # Throughput scales with workers up to 5, then is capped (e.g. by bandwidth).
    succeed = drive(limiter, monkeypatch, lambda level: 1 / min(level, 5))
    for _ in range(200):
        succeed()
    assert limiter.limit == limiter.ceiling == 5


def test_limiter_idle_gaps_do_not_count_as_slow_downloads(monkeypatch):
    limiter = vogue.AdaptiveLimiter(start=3, minimum=1, maximum=12, grow_after=10)
    clock = [1000.0]
    monkeypatch.setattr(vogue.time, "monotonic", lambda: clock[0])
    limiter._reset_window()
    for level_round in range(2):
        for _ in range(10):
            with limiter:
                clock[0] += 1 / limiter.limit
            limiter.on_success()
        clock[0] += 30  # page fetch between shows
    assert limiter.limit == 5


def test_limiter_restores_learned_pace(monkeypatch):
    limiter = vogue.AdaptiveLimiter(start=3, minimum=1, maximum=12, grow_after=10)
    limiter.restore({"limit": 6, "ceiling": 7, "saved_at": vogue.time.time()})
    assert (limiter.limit, limiter.ceiling) == (6, 7)

    stale = vogue.AdaptiveLimiter(start=3, minimum=1, maximum=12, grow_after=10)
    stale.restore({"limit": 6, "ceiling": 7, "saved_at": 0})
    assert (stale.limit, stale.ceiling) == (6, 12)


def test_limiter_counts_a_burst_of_throttles_once():
    limiter = vogue.AdaptiveLimiter(start=8, minimum=1, maximum=8, grow_after=40)
    for _ in range(5):
        limiter.on_throttle()
    assert (limiter.limit, limiter.throttle_events) == (4, 1)


class FakeResponse:
    def __init__(self, status_code, headers=None, content=b"img"):
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = headers or {"Content-Type": "image/webp"}
        self.content = content


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, url, headers, timeout):
        return self.responses.pop(0)


def test_image_download_backs_off_on_429_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(vogue, "IMAGE_DELAY", (0, 0))
    monkeypatch.setattr(vogue, "THROTTLE_PAUSE", 0)
    limiter = vogue.AdaptiveLimiter(start=4, minimum=1, maximum=8, grow_after=40)
    session = FakeSession([FakeResponse(429, {"Retry-After": "0"}), FakeResponse(200)])
    record = {"image_name": "look_0001.jpg", "image_url": "https://assets.vogue.com/x.jpg"}

    _, status = vogue._download_single_image(session, record, str(tmp_path), "ref", limiter)

    assert status == "downloaded"
    assert (limiter.limit, limiter.throttle_events) == (2, 1)
    assert (tmp_path / "look_0001.jpg").read_bytes() == b"img"


def test_image_download_rejects_html_served_as_200(tmp_path, monkeypatch):
    monkeypatch.setattr(vogue, "IMAGE_DELAY", (0, 0))
    session = FakeSession([FakeResponse(200, {"Content-Type": "text/html"})])
    record = {"image_name": "look_0001.jpg", "image_url": "https://assets.vogue.com/x.jpg"}

    _, status = vogue._download_single_image(session, record, str(tmp_path), "ref")

    assert status.startswith("error: not an image")
    assert not (tmp_path / "look_0001.jpg").exists()
