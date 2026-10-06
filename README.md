# Vogue Runway Archiver

Downloads runway images and show reviews from Vogue Runway. Stop anytime and rerun to resume.

Based on [TonyAssi/Vogue-Runway-Scraper](https://github.com/TonyAssi/Vogue-Runway-Scraper).

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
python3 vogue.py download-all            # every designer in designers.txt
python3 vogue.py download-all --refresh  # recheck completed designers for new shows
python3 vogue.py retry-failed            # failed or incomplete designers only
python3 vogue.py download-designer gucci
python3 vogue.py download-show gucci "Spring 2018 Ready-to-Wear"
python3 vogue.py report                  # failures grouped by cause
```

Saves to `vogue_downloads/`. Override with `VOGUE_SAVE_PATH` or a path as the last argument.

Also usable from Python:

```python
import vogue

vogue.designer_to_download_images("gucci")
vogue.designer_to_csv("gucci", ".")
```

## Output

```text
<save path>/
  _scrape_state.json   # resume state
  _failures.jsonl      # one line per failure
  _pacing.json         # learned download speed
  miu-miu/
    2026-fall-ready-to-wear/
      description.md   # show review
      show_metadata.json
      collection/look_0001.jpg
      details/detail_0001.jpg
```

Images are 1024px WebP, saved with a `.jpg` extension. Beauty galleries are skipped.

## Rate limiting

- Page requests use random delays.
- Image downloads add workers (up to 12) while throughput improves, and halve them on a 403/429/503 or a dropped connection.
- If Vogue blocks the scraper, it pauses 15 minutes, retries up to 4 times, then stops.
