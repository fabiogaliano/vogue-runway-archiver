# Vogue Runway Archiver

Based on [TonyAssi/Vogue-Runway-Scraper](https://github.com/TonyAssi/Vogue-Runway-Scraper).

Scrapes Vogue Runway show data and downloads images slowly enough to be resumable and safer for long runs.

## Install

```bash
pip install -r requirements.txt
```

## Quick Start

```python
import vogue
```

Get all shows for a designer:

```python
vogue.designer_to_shows("gucci")
```

Download one show:

```python
vogue.designer_show_to_download_images("gucci", "Spring 2018 Ready-to-Wear")
```

Download all shows for one designer:

```python
vogue.designer_to_download_images("gucci")
```

Download all designers from a text file with resume support:

```python
vogue.all_designers_to_download_images("designers.txt")
```

By default, downloads are saved to:

```text
vogue_downloads
```

You can still override it by passing a custom path as the last argument.

Run the full download from the terminal:

```bash
python3 vogue.py download-all
```

That uses `designers.txt` and the default output folder above.

Export one show to CSV:

```python
vogue.designer_show_to_csv("gucci", "Spring 2018 Ready-to-Wear", ".")
```

Export all shows for one designer to CSV:

```python
vogue.designer_to_csv("gucci", ".")
```

Export all designers from a text file to CSV:

```python
vogue.all_designers_to_csv("designers.txt", ".")
```

## What Gets Saved

For downloads, each show is stored like this:

```text
images/
  miu-miu/
    2026-fall-ready-to-wear/
      description.md
      show_metadata.json
      collection/
        look_0001.jpg
      details/
        detail_0001.jpg
```

- `collection/` and `details/` are downloaded when available
- `beauty/` is skipped
- `description.md` stores the full show review text
- `show_metadata.json` stores structured metadata for resume logic and later ML/data work

## Resume Behavior

Long runs keep a central state file at:

```text
vogue_downloads/_scrape_state.json
```

You can stop the terminal (Ctrl-C) and run the same command again later. The scraper will:

- reload the saved state
- check files already on disk
- continue from the remaining designers, shows, and images

A designer only counts as complete once every show on its Vogue page is downloaded.

```bash
python3 vogue.py download-all --refresh   # revisit completed designers for new collections
python3 vogue.py retry-failed             # retry only designers that failed or are incomplete
python3 vogue.py report                   # what failed, grouped by cause
```

`download-all` skips designers Vogue has no page for; `retry-failed` tries them again.

## Failures

- `_failures.jsonl` (next to the state file) gets one line per failure: time, kind, designer, show, URL, error
- `report` groups everything still incomplete by kind: `designer_not_found`, `show_not_found`, `show_unparseable`, `images_failed`, `request_failed`, `ssl_error`, ...
- When Vogue throttles (it answers 404/429), the scraper checks a known page, pauses 15 minutes, and stops after repeated blocks instead of marking everything as failed

## Designer URLs

Vogue's URL names don't always follow the names in `designers.txt` (`Agnès B.` → `agnes-b-`, `Burberry` → `burberry-prorsum`). The scraper looks names up in Vogue's designer directory (cached for a week in `_designer_directory.json`) and falls back to guessed slugs. The slug that worked is stored in the state file. Show URLs are taken from the designer page instead of being guessed.

## Notes

- Requests are intentionally slowed with randomized delays, rotating user agents, and retries
- show folders use designer-first, year-first naming like `miu-miu/2026-fall-ready-to-wear/`
- images are 1024px wide and Vogue serves them as WebP, so the `.jpg` files are WebP data
- some shows (e.g. Celine 2020–2022) have no runway images on Vogue; they're marked complete with `"empty": true`
- CSV rows include `designer`, `show`, `gallery`, `show_description`, `image_index`, `image_name`, and `image_url`
