# Vogue Runway Scraper

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

You can stop the terminal and run the same command again later. The scraper will:

- reload the saved state
- check files already on disk
- continue from the remaining designers, shows, and images

## Notes

- Requests are intentionally slowed with randomized delays, rotating user agents, and retries
- show folders use designer-first, year-first naming like `miu-miu/2026-fall-ready-to-wear/`
- CSV rows include `designer`, `show`, `gallery`, `show_description`, `image_index`, `image_name`, and `image_url`
