# Steam Trade History UI (Prototype)

This repo contains a small, local-first prototype that:
1) Fetches/parses saved Steam trade-history HTML pages into machine-readable JSON.
2) Serves a simple Flask web UI to visualize trades and search/filter the parsed result.

It is intentionally “vibe coded” (prototype quality): it is not meant for production use.

## License

MIT License (see `LICENSE` if you add one, or use this text as the intent): you can use/modify/distribute this project under the MIT terms.

## What this prototype uses (current implementation)

- `steam-trade-html/page-<N>.html`: saved Steam trade-history HTML pages (input)
- `page-<N>.parsed.json`: cached parsed output (generated on demand by the UI)
- `parse_steam_trade_page.py`: parses a single `page-<N>.html` into JSON
- `app.py`: Flask server that renders:
  - `GET /?page=<N>&q=<search>&direction=<received|given|unknown>&app_id=<id>`
  - `POST /parse/<page_num>` (parses `steam-trade-html/page-<page_num>.html` and caches to `page-<page_num>.parsed.json`)

Additionally, the UI includes:

- `/avatar/<profile_path>`: local Flask endpoint that fetches Steam profile `?xml=1` and redirects to a usable avatar image (with caching + local fallback).

## Clone

```bash
git clone <YOUR_GITHUB_URL_HERE>
cd steam-trade-history
```

## Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the Flask server

By default it tries to use `PORT=5000`. On macOS you may have something else already bound to `5000`, so choose your own port:

```bash
PORT=1337 python3 app.py
```

Then open:

- `http://127.0.0.1:1337/`

## Using the UI

- The UI loads `page-1.parsed.json` by default.
- To view another page, use: `/?page=<N>`
- If `page-<N>.parsed.json` doesn’t exist yet, click **“Parse page N from HTML”** (the server will parse `steam-trade-html/page-<N>.html` and cache the output).
- The search box matches across (best-effort):
  - partner name / event text
  - direction
  - item names (and inferred app ids)

## Notes / local-only assumptions

- This is a prototype for local use.
- Flask runs in debug mode.
- The `get`/`fetch` shell scripts and Steam scraping are intentionally not documented as “production ready” here; the UI works from local saved HTML + cached parsed JSON.

## Local data and artifacts

This prototype relies on local saved Steam HTML and generated parsed JSON:

- `steam-trade-html/` (input HTML) is ignored by `.gitignore` and should stay local.
- `json/` and `page-*.parsed.json` (generated/derived data) are also ignored by `.gitignore`.

When you run the UI, it will parse/cache `page-*.parsed.json` on demand.

## Default avatar fallback asset

The UI falls back to a local image when Steam doesn’t provide an avatar:

- `static/avatars/steam-default.jpg`

If you don’t have it yet, download it into that path (from the repo root):

```bash
mkdir -p static/avatars
curl -L "https://avatars.fastly.steamstatic.com/fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb_full.jpg" -o static/avatars/steam-default.jpg
```

