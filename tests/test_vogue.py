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
