from __future__ import annotations

import json as jsonlib
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, flash, redirect, render_template, request, url_for

from parse_steam_trade_page import parse_trade_page

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"

ROOT_DIR = Path(__file__).resolve().parent
HTML_DIR = ROOT_DIR / "steam-trade-html"
AVATAR_CACHE_DIR = ROOT_DIR / "cache" / "avatars"
AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days
STEAM_PROFILE_XML_URL_TEMPLATE = "https://steamcommunity.com/{profile_path}/?xml=1"
DEFAULT_AVATAR_STATIC_FILENAME = "avatars/steam-default.jpg"


def _parsed_path(page_num: int) -> Path:
    return ROOT_DIR / f"page-{page_num}.parsed.json"


def _html_path(page_num: int) -> Path:
    return HTML_DIR / f"page-{page_num}.html"


def _parse_and_cache_page(page_num: int) -> Dict[str, Any]:
    html_path = _html_path(page_num)
    if not html_path.is_file():
        raise FileNotFoundError(f"Missing HTML page: {html_path}")

    html = html_path.read_text(encoding="utf-8", errors="ignore")
    parsed = parse_trade_page(html, html_path.name)

    parsed_path = _parsed_path(page_num)
    parsed_path.write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return parsed


def load_page(page_num: int) -> Dict[str, Any]:
    parsed_path = _parsed_path(page_num)
    if parsed_path.is_file() and parsed_path.stat().st_size > 0:
        try:
            parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
        except Exception:
            parsed = None

        # Auto-refresh: older cached parsed pages might have missing TF2 material
        # icon reconstruction (when app_id couldn't be inferred from item_url).
        # If we detect likely stale entries, re-parse from HTML (if present).
        if isinstance(parsed, dict):
            try:
                refresh_needed = False
                for t in parsed.get("trades") or []:
                    for it in t.get("items") or []:
                        name = it.get("item_name")
                        # Stickers sometimes require JS icon lookup; if cached
                        # parsed pages ended up without icons, re-parse.
                        if isinstance(name, str) and name.startswith("Sticker |"):
                            if not it.get("item_img_url"):
                                refresh_needed = True
                                break
                        if name in {"Refined Metal", "Scrap Metal", "Reclaimed Metal"}:
                            if not it.get("item_img_url") and not it.get("app_id"):
                                refresh_needed = True
                                break
                        # Old cache might have over-applied "TF2 Metal" to all
                        # `#7d6d00`-colored TF2 items. If we see that, re-parse.
                        if it.get("item_rarity") == "TF2 Metal" and name not in {
                            "Refined Metal",
                            "Scrap Metal",
                            "Reclaimed Metal",
                        }:
                            refresh_needed = True
                            break
                    if refresh_needed:
                        break

                if refresh_needed:
                    try:
                        return _parse_and_cache_page(page_num)
                    except FileNotFoundError:
                        # No HTML available; keep whatever parsed cache we have.
                        return parsed
            except Exception:
                # Best-effort only; fall back to cached parsed data.
                pass
            return parsed

    return _parse_and_cache_page(page_num)


def trade_to_haystack(trade: Dict[str, Any]) -> str:
    parts: List[str] = []

    for key in (
        "event_text",
        "partner_name",
        "partner_url",
        "trade_game_scope",
        "direction",
    ):
        v = trade.get(key)
        if v:
            parts.append(str(v))

    app_ids = trade.get("trade_app_ids") or []
    if isinstance(app_ids, list):
        for app_id in app_ids:
            if app_id:
                parts.append(str(app_id))

    items = trade.get("items") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("item_name")
            if name:
                parts.append(str(name))

    return " ".join(parts).lower()


_PROTECTION_STATS_CACHE: Dict[str, Any] = {"latest_mtime": None}


def compute_protection_stats_across_cached_pages() -> Dict[str, int]:
    """
    Counts "trade_protected" trades across all cached `page-*.parsed.json`.

    This is a prototype helper for a Steam-like banner message:
      "You have X recent trades with protected items..."
    """
    global _PROTECTION_STATS_CACHE

    parsed_paths = sorted(ROOT_DIR.glob("page-*.parsed.json"))
    latest_mtime = max((p.stat().st_mtime for p in parsed_paths), default=0)

    if _PROTECTION_STATS_CACHE.get("latest_mtime") == latest_mtime:
        return {
            "protected_trades_count": int(_PROTECTION_STATS_CACHE.get("protected_trades_count", 0)),
            "total_trades_count": int(_PROTECTION_STATS_CACHE.get("total_trades_count", 0)),
        }

    protected_trades_count = 0
    total_trades_count = 0

    for path in parsed_paths:
        try:
            data = jsonlib.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for t in data.get("trades") or []:
            total_trades_count += 1
            if t.get("trade_protected"):
                protected_trades_count += 1

    _PROTECTION_STATS_CACHE = {
        "latest_mtime": latest_mtime,
        "protected_trades_count": protected_trades_count,
        "total_trades_count": total_trades_count,
    }

    return {
        "protected_trades_count": protected_trades_count,
        "total_trades_count": total_trades_count,
    }


@app.route("/", methods=["GET"])
def index() -> str:
    page_num = int(request.args.get("page", "1"))
    q = (request.args.get("q") or "").strip()
    direction = (request.args.get("direction") or "").strip()
    app_id = (request.args.get("app_id") or "").strip()

    def _page_nums_from_glob(pattern: str) -> set[int]:
        nums: set[int] = set()
        for p in ROOT_DIR.glob(pattern):
            name = p.name
            m = re.search(r"page-(\d+)\.", name)
            if not m:
                continue
            try:
                nums.add(int(m.group(1)))
            except Exception:
                continue
        return nums

    html_pages = _page_nums_from_glob("steam-trade-html/page-*.html")
    parsed_pages = _page_nums_from_glob("page-*.parsed.json")
    available_pages = sorted(html_pages | parsed_pages)

    first_page_num = available_pages[0] if available_pages else None
    last_page_num = available_pages[-1] if available_pages else None

    error: str | None = None
    notice: str | None = None
    if available_pages and page_num not in available_pages:
        # Prevent "dead-end" pages (e.g. user requests page N with no HTML/parsed cache).
        # We clamp to the nearest available page so the UI stays functional.
        clamped_page_num = (
            max((p for p in available_pages if p < page_num), default=None)
            or min((p for p in available_pages if p > page_num), default=None)
            or first_page_num
        )
        notice = (
            f"Requested page {page_num} isn't available yet. "
            f"Showing page {clamped_page_num} instead."
        )
        page_num = clamped_page_num

    has_html_page = page_num in html_pages
    has_parsed_page = page_num in parsed_pages

    prev_page_num = max((p for p in available_pages if p < page_num), default=None)
    next_page_num = min((p for p in available_pages if p > page_num), default=None)

    parsed = None

    try:
        # Prefer showing parsed data even if HTML is missing; only error when both are missing.
        if not has_parsed_page and not has_html_page:
            raise FileNotFoundError(
                f"Missing HTML page: {HTML_DIR / f'page-{page_num}.html'}"
            )
        parsed = load_page(page_num)
    except FileNotFoundError as e:
        error = str(e)
        parsed = {"trades": [], "page": {"summary_text": "", "cursor_href": None}}

    trades: List[Dict[str, Any]] = parsed.get("trades") or []
    filtered: List[Dict[str, Any]] = []

    for trade in trades:
        if direction and trade.get("direction") != direction:
            continue

        if app_id:
            trade_ids = trade.get("trade_app_ids") or []
            trade_ids_str = {str(x) for x in trade_ids}
            if app_id not in trade_ids_str:
                continue

        if q:
            if q.lower() not in trade_to_haystack(trade):
                continue

        filtered.append(trade)

    all_app_ids = set()
    for trade in trades:
        for x in (trade.get("trade_app_ids") or []):
            if x:
                all_app_ids.add(str(x))

    page_meta = parsed.get("page") or {}
    protection_stats = compute_protection_stats_across_cached_pages()
    return render_template(
        "index.html",
        trades=filtered,
        page_num=page_num,
        total_trades=len(trades),
        filtered_trades=len(filtered),
        q=q,
        direction=direction,
        app_id=app_id,
        error=error,
        notice=notice,
        summary_text=page_meta.get("summary_text", ""),
        cursor_href=page_meta.get("cursor_href"),
        all_app_ids=sorted(all_app_ids),
        protected_trades_count=protection_stats.get("protected_trades_count", 0),
        protection_help_url="https://help.steampowered.com/wizard/HelpTradeRestore",
        prev_page_num=prev_page_num,
        next_page_num=next_page_num,
        first_page_num=first_page_num,
        last_page_num=last_page_num,
        has_html_page=has_html_page,
    )


@app.route("/parse/<int:page_num>", methods=["POST", "GET"])
def parse_page(page_num: int):
    try:
        load_page(page_num)
        flash(f"Loaded/parsing cached page {page_num}.")
    except FileNotFoundError as e:
        flash(str(e))
    return redirect(url_for("index", page=page_num))


def _steam_avatar_from_xml(xml_bytes: bytes) -> str:
    """
    Parse Steam profile `?xml=1` response and extract avatarMedium URL.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return ""

    # Steam XML usually has tags like <avatarMedium>...</avatarMedium>.
    for el in root.iter():
        tag = el.tag
        if isinstance(tag, str) and tag.endswith("avatarMedium"):
            if el.text:
                return el.text.strip()
    return ""


def _cache_path_for_profile(profile_path: str) -> Path:
    # profile_path is like "profiles/<steamid>" or "id/<custom>"
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", profile_path)
    return AVATAR_CACHE_DIR / f"{safe}.json"


def _read_avatar_cache(profile_path: str) -> str:
    cache_path = _cache_path_for_profile(profile_path)
    if not cache_path.is_file():
        return ""
    try:
        payload = jsonlib.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    fetched_at = payload.get("fetched_at", 0)
    if not isinstance(fetched_at, (int, float)):
        return ""

    if time.time() - float(fetched_at) > AVATAR_CACHE_TTL_SECONDS:
        return ""

    avatar_url = payload.get("avatar_medium_url", "") or ""
    return avatar_url if isinstance(avatar_url, str) else ""


def _write_avatar_cache(profile_path: str, avatar_medium_url: str) -> None:
    cache_path = _cache_path_for_profile(profile_path)
    payload = {
        "profile_path": profile_path,
        "avatar_medium_url": avatar_medium_url,
        "fetched_at": time.time(),
    }
    cache_path.write_text(jsonlib.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_or_fetch_avatar_medium_url(profile_path: str) -> str:
    cached = _read_avatar_cache(profile_path)
    if cached:
        return cached

    url = STEAM_PROFILE_XML_URL_TEMPLATE.format(profile_path=profile_path)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (prototype; local UI) Python urllib",
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
    except Exception:
        return ""

    avatar_url = _steam_avatar_from_xml(xml_bytes)
    if avatar_url:
        _write_avatar_cache(profile_path, avatar_url)
    return avatar_url


@app.route("/avatar/<path:profile_path>", methods=["GET"])
def steam_avatar(profile_path: str):
    profile_path = str(profile_path).lstrip("/")
    if not profile_path or not (
        profile_path.startswith("profiles/") or profile_path.startswith("id/")
    ):
        return ("invalid profile path", 400)

    avatar_url = get_or_fetch_avatar_medium_url(profile_path)
    if not avatar_url:
        # Foolproof fallback: always redirect to a local default image.
        return redirect(url_for("static", filename=DEFAULT_AVATAR_STATIC_FILENAME))

    # Redirect so the browser receives the actual image.
    return redirect(avatar_url)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=True, host="127.0.0.1", port=port)

