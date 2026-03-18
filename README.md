# Steam Trade History Analyzer (Prototype)

A local-first prototype that:
1) Parses saved Steam trade-history HTML pages into structured JSON.
2) Serves a simple Flask web UI to browse, search, and filter trades.

It is intentionally “vibe coded” (prototype quality) and not meant for production use.

---

## Features

- Parse Steam trade history HTML into structured JSON
- Browse trades via a local web UI
- Filter by:
  - partner
  - direction (received / given)
  - game (app_id, e.g. CS2 = 730, TF2 = 440)
- Search across:
  - partner name
  - event text
  - item names
- Local-first (no database required)
- Works offline after HTML is collected

---

## License

MIT License (see LICENSE if you add one): you can use, modify, and distribute this project under MIT terms.

---

## What this prototype uses (current implementation)

- steam-trade-html/page-<N>.html: saved Steam trade-history HTML pages (input)
- page-<N>.parsed.json: cached parsed output (generated on demand by the UI)
- parse_steam_trade_page.py: parses a single page-<N>.html into JSON
- app.py: Flask server that renders:
  - GET /?page=<N>&q=<search>&direction=<received|given|unknown>&app_id=<id>
  - POST /parse/<page_num> (parses HTML and caches JSON)

Additionally:

- /avatar/<profile_path>: fetches Steam profile XML (?xml=1) and resolves avatar images with caching + fallback

---

## Clone

```bash
git clone https://github.com/raitnigol/steam-trade-analyzer.git
cd steam-trade-analyzer
```

---

## Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Run the Flask server

```bash
PORT=1337 python3 app.py
```

Then open:

http://127.0.0.1:1337/

---

## Using the UI

- Default: loads page-1.parsed.json
- Navigate pages:
  /?page=<N>
- If a page is not parsed yet:
  click "Parse page N from HTML"
- Search matches:
  - partner name / event text
  - direction
  - item names (and inferred app IDs)

---

## Notes / local-only assumptions

- This is a prototype for local use
- Flask runs in debug mode
- Scraping/fetch scripts are not production-ready
- The UI operates purely on local HTML + cached JSON

---

## Local data and artifacts

This project relies on local files:

- steam-trade-html/ (raw HTML) → ignored by git
- page-*.parsed.json (generated data) → ignored by git

Parsing happens on demand and results are cached.

---

## Default avatar fallback asset

mkdir -p static/avatars
curl -L https://avatars.fastly.steamstatic.com/fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb_full.jpg -o static/avatars/steam-default.jpg

---

## Disclaimer

This project is not affiliated with Valve or Steam.

- All data is sourced from your own Steam account
- The tool operates on locally saved HTML pages
- Use responsibly and ensure compliance with Steam’s Terms of Service when collecting data