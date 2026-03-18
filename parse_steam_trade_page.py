#!/usr/bin/env python3
"""
Parse one Steam trade history HTML page into structured JSON.

Usage:
    python3 parse_steam_trade_page.py page-1.html
    python3 parse_steam_trade_page.py page-1.html -o page-1.parsed.json

What it extracts per page:
- page cursor from the pager (?after_time=...&after_trade=...)
- page summary text
- each .tradehistoryrow as a structured object:
  - date/time text
  - partner name/url/miniprofile
  - event text
  - direction (+ / - => received / given)
  - protection info
  - item list
  - item colors / image / links when present
  - app_id per item when detectable
  - trade-level app_id summary
  - flags for empty/incomplete rows
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s+([.,!?;:])", r"\1", value)
    return value


def parse_style(style: Optional[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not style:
        return result

    for part in style.split(";"):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        result[key.strip().lower()] = val.strip()
    return result


def extract_cursor_from_href(href: str) -> Dict[str, Optional[str]]:
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    return {
        "after_time": qs.get("after_time", [None])[0],
        "after_trade": qs.get("after_trade", [None])[0],
        "prev": qs.get("prev", [None])[0],
        "language": qs.get("l", [None])[0],
    }


def extract_app_id_from_item_url(item_url: str) -> str:
    if not item_url:
        return ""

    # Example:
    # https://steamcommunity.com/id/nigol_/inventory/#730_2_50442950785
    # https://steamcommunity.com/id/nigol_/inventory/#440_2_13594618127
    m = re.search(r"#(\d+)_", item_url)
    return m.group(1) if m else ""


def infer_app_id_from_item_name(item_name: str) -> str:
    name = item_name or ""

    # TF2
    if "Mann Co. Supply Crate Key" in name:
        return "440"
    if name in {"Refined Metal", "Scrap Metal", "Reclaimed Metal"}:
        # TF2 materials: icon reconstruction needs an appid.
        return "440"

    # CS2 / CSGO-style names
    # Examples:
    # AK-47 | Redline
    # Charm | Lil' Cap Gun
    if "|" in name:
        return "730"

    return ""


def infer_rarity_from_name_color(name_color: str) -> str:
    """
    Steam uses a small set of color values to reflect weapon skin quality/rarity.
    We map the observed `item_name_color` values to human-readable grade names.

    Note: This is a prototype heuristic; if Steam introduces new palettes, those
    items will fall back to empty rarity.
    """
    c = (name_color or "").lower().strip()
    # Common CS2 quality palette (best-effort mapping based on observed values in page-1.html).
    # Gold (`#e4ae39`) is handled later using the item name (★ vs non-★).
    if c == "#b0c3d9":
        return "Consumer Grade"
    if c == "#5e98d9":
        return "Industrial Grade"
    if c == "#4b69ff":
        return "Mil-Spec Grade"
    if c == "#8847ff":
        return "Restricted Grade"
    if c == "#d32ce6":
        return "Classified Grade"
    if c == "#eb4b4b":
        return "Covert Grade"
    if c == "#e4ae39":
        return "Gold Grade"
    # TF2 trade-history quality colors for appid=440.
    # Steam seems to use `name_color` as a quality hint for TF2 items.
    # These palettes are observed from this prototype dataset.
    if c == "#7d6d00":
        return "TF2 Weapon"
    if c == "#cf6a32":
        return "TF2 Strange"
    if c == "#476291":
        return "TF2 Vintage"
    if c == "#fafafa":
        return "TF2 Special"
    return ""


def classify_gold_rarity(item_name: str) -> tuple[str, str]:
    """
    Gold color is used for multiple special buckets.
    Distinguish using name markers.
    """
    n = item_name or ""
    is_rare_special = ("★" in n) or ("Knife" in n) or ("Glove" in n)
    if is_rare_special:
        return "★ Rare Special Item", "rare-special"
    return "Contraband", "contraband"


def rarity_class_from_label(rarity_label: str) -> str:
    """
    Map displayed rarity text to a stable CSS class.
    """
    mapping = {
        "Consumer Grade": "consumer",
        "Industrial Grade": "industrial",
        "Mil-Spec Grade": "milspec",
        "Restricted Grade": "restricted",
        "Classified Grade": "classified",
        "Covert Grade": "covert",
        "Gold Grade": "gold",
        "TF2 Weapon": "tf2-weapon",
        "TF2 Metal": "tf2-metal",
        "TF2 Strange": "tf2-strange",
        "TF2 Vintage": "tf2-vintage",
        "TF2 Special": "tf2-special",
    }
    return mapping.get(rarity_label, "unknown" if rarity_label else "")


def detect_item_app_id(item_url: str, item_name: str) -> str:
    return extract_app_id_from_item_url(item_url) or infer_app_id_from_item_name(item_name)


TRADE_ECON_IMAGE_BASE_URL = "https://community.akamai.steamstatic.com/economy/image"


def extract_history_inventory_icon_lookup(html: str) -> Dict[Tuple[str, str], str]:
    """
    Steam embeds a JS object `g_rgHistoryInventory` in the trade-history HTML.

    For some trade rows, the per-item DOM doesn't include an <img>, but the JS object
    still contains per-asset `icon_url`. We build a lookup:
        (appid, item_name) -> icon_url
    """
    try:
        # Use brace-matching extraction; the previous non-greedy regex can
        # truncate large JS objects, which breaks icon/wear lookups for some items
        # (e.g., stickers).
        obj_text = extract_js_object_literal(html, "g_rgHistoryInventory")
        if not obj_text:
            return {}
        inv = json.loads(obj_text)
    except Exception:
        # Best-effort: if Steam changes markup and JSON parsing fails, fall back gracefully.
        return {}

    lookup: Dict[Tuple[str, str], str] = {}
    if not isinstance(inv, dict):
        return lookup

    for appid, ctxs in inv.items():
        if not isinstance(ctxs, dict):
            continue
        appid_s = str(appid)

        for _contextid, assets in ctxs.items():
            if not isinstance(assets, dict):
                continue
            for _assetid, details in assets.items():
                if not isinstance(details, dict):
                    continue
                # Normalize whitespace so it matches what we parse from the DOM.
                # Some entries use double-spaces around separators (e.g. stickers).
                name = clean_text(details.get("name") or "")
                icon_url = details.get("icon_url") or ""
                if not name or not icon_url:
                    continue

                # item_name from the HTML tends to match `details["name"]` (without exterior wear),
                # so keying by (appid, name) gives good coverage for the prototype.
                key = (appid_s, str(name))
                lookup.setdefault(key, str(icon_url))

    return lookup


def extract_history_inventory_wear_lookup(html: str) -> Dict[Tuple[str, str], str]:
    """
    Steam embeds a JS object `g_rgHistoryInventory` in the trade-history HTML.

    For wearable skins, item details include `descriptions` entries such as:
      { "name": "exterior_wear", "value": "Exterior: Factory New" }

    Returns a lookup:
      (appid, item_name) -> wear_label
    """
    try:
        obj_text = extract_js_object_literal(html, "g_rgHistoryInventory")
        if not obj_text:
            return {}
        inv = json.loads(obj_text)
    except Exception:
        return {}

    wear_lookup: Dict[Tuple[str, str], str] = {}
    if not isinstance(inv, dict):
        return wear_lookup

    # Small helper to strip HTML tags from values.
    def _strip_html(value: str) -> str:
        # Remove tags, then normalize whitespace.
        no_tags = re.sub(r"<[^>]+>", "", value)
        no_tags = re.sub(r"\s+", " ", no_tags).strip()
        return no_tags

    for appid, ctxs in inv.items():
        if not isinstance(ctxs, dict):
            continue
        appid_s = str(appid)

        for _contextid, assets in ctxs.items():
            if not isinstance(assets, dict):
                continue
            for _assetid, details in assets.items():
                if not isinstance(details, dict):
                    continue

                item_name = clean_text(details.get("name") or "")
                if not item_name:
                    continue

                descriptions = details.get("descriptions") or []
                if not isinstance(descriptions, list):
                    continue

                wear_label = ""
                for d in descriptions:
                    if not isinstance(d, dict):
                        continue
                    desc_name = (d.get("name") or "").strip()
                    if desc_name not in {"exterior_wear", "wear"}:
                        continue

                    value = d.get("value") or ""
                    if not isinstance(value, str):
                        continue

                    plain = _strip_html(value)
                    # Example: "Exterior: Factory New"
                    m_wear = re.search(r"Exterior:\s*(.+)$", plain, flags=re.IGNORECASE)
                    if m_wear:
                        wear_label = m_wear.group(1).strip()
                    else:
                        wear_label = plain.strip()
                    break

                if wear_label:
                    wear_lookup[(appid_s, str(item_name))] = wear_label

    return wear_lookup


def extract_js_object_literal(html: str, var_name: str) -> str:
    """
    Extract a JS object literal assigned to `var <var_name> = {...};`
    using brace matching instead of fragile regex.

    Returns the `{...}` substring (suitable for `json.loads` when the object is JSON-compatible).
    """
    # Find `var <var_name> =`
    m = re.search(rf"var\s+{re.escape(var_name)}\s*=\s*", html)
    if not m:
        return ""

    start_idx = html.find("{", m.end())
    if start_idx < 0:
        return ""

    depth = 0
    in_quote: str | None = None
    escape = False

    for i in range(start_idx, len(html)):
        ch = html[i]

        if in_quote is not None:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == in_quote:
                in_quote = None
            continue

        # Not inside a quoted string
        if ch in ("'", '"', "`"):
            in_quote = ch
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start_idx : i + 1]

    return ""


def find_main_contents(soup: BeautifulSoup) -> Tag:
    main = soup.find(id="mainContents")
    if main is None:
        raise ValueError("Could not find #mainContents in HTML.")
    return main


def extract_page_pager(main: Tag) -> Dict[str, Any]:
    pager = main.find("div", class_="inventory_history_pagingrow")
    if pager is None:
        return {
            "cursor_href": None,
            "after_time": None,
            "after_trade": None,
            "summary_text": "",
        }

    next_btn_area = pager.find("div", class_="inventory_history_nextbtn")
    href: Optional[str] = None

    if next_btn_area:
        for a in next_btn_area.find_all("a", class_="pagebtn"):
            a_text = clean_text(a.get_text(" ", strip=True))
            candidate_href = a.get("href")
            if not candidate_href:
                continue

            if a_text == ">" or "&gt;" in str(a):
                href = candidate_href
                break

        if href is None:
            for a in next_btn_area.find_all("a", class_="pagebtn"):
                candidate_href = a.get("href")
                if not candidate_href:
                    continue
                if "after_time=" in candidate_href and "after_trade=" in candidate_href and "prev=1" not in candidate_href:
                    href = candidate_href
                    break

    full_pager_text = clean_text(pager.get_text(" ", strip=True))
    summary_match = re.search(r"(Showing .*? events)", full_pager_text, flags=re.IGNORECASE)
    summary_text = summary_match.group(1) if summary_match else full_pager_text

    cursor = extract_cursor_from_href(href) if href else {
        "after_time": None,
        "after_trade": None,
        "prev": None,
        "language": None,
    }

    return {
        "cursor_href": href,
        "after_time": cursor["after_time"],
        "after_trade": cursor["after_trade"],
        "summary_text": summary_text,
    }


def extract_protection_info(content: Tag) -> Dict[str, Any]:
    unsettled = content.find("div", class_="inventory_history_unsettled")
    if unsettled is None:
        return {
            "trade_protected": False,
            "trade_protected_text": "",
            "trade_protected_until_text": "",
        }

    text = clean_text(unsettled.get_text(" ", strip=True))
    m = re.search(r"until\s+(.+?)(?:\.|$)", text, flags=re.IGNORECASE)

    return {
        "trade_protected": True,
        "trade_protected_text": text,
        "trade_protected_until_text": clean_text(m.group(1)) if m else "",
    }


def extract_partner_info(content: Tag) -> Dict[str, Any]:
    desc = content.find("div", class_="tradehistory_event_description")
    if desc is None:
        return {
            "event_text": "",
            "partner_name": "",
            "partner_url": "",
            "partner_miniprofile": "",
            "partner_steamid64": "",
            # Used to fetch avatar from Steam even when partner_url is /id/<custom> (not /profiles/<steamid64>).
            "partner_avatar_profile_path": "",
        }

    event_text = clean_text(desc.get_text(" ", strip=True))
    event_text = event_text.replace('"', "")
    a = desc.find("a")

    partner_name = ""
    partner_url = ""
    partner_miniprofile = ""
    partner_steamid64 = ""
    partner_avatar_profile_path = ""

    if a:
        partner_name = clean_text(a.get_text(" ", strip=True)).strip('"')
        partner_url = a.get("href", "") or ""
        partner_miniprofile = a.get("data-miniprofile", "") or ""
        # partner_url is usually like:
        # https://steamcommunity.com/profiles/<steamid64>
        m = re.search(r"/profiles/(\d+)", partner_url)
        partner_steamid64 = m.group(1) if m else ""

        # partner_avatar_profile_path is the portion after `steamcommunity.com/`,
        # e.g. `profiles/7656119...` or `id/Steve419...`.
        parsed = urlparse(partner_url)
        partner_avatar_profile_path = parsed.path.lstrip("/")

    return {
        "event_text": event_text,
        "partner_name": partner_name,
        "partner_url": partner_url,
        "partner_miniprofile": partner_miniprofile,
        "partner_steamid64": partner_steamid64,
        "partner_avatar_profile_path": partner_avatar_profile_path,
    }


def extract_direction(content: Tag) -> Dict[str, Any]:
    pm = content.find("div", class_="tradehistory_items_plusminus")
    raw = clean_text(pm.get_text(" ", strip=True)) if pm else ""

    if raw == "+":
        direction = "received"
    elif raw in {"–", "-", "−"}:
        direction = "given"
    else:
        direction = "unknown"

    return {
        "direction_symbol": raw,
        "direction": direction,
    }


def extract_items(
    content: Tag,
    icon_lookup: Dict[Tuple[str, str], str],
    wear_lookup: Dict[Tuple[str, str], str],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    items_group_wrappers = content.find_all("div", class_="tradehistory_items_group")

    for wrapper in items_group_wrappers:
        candidates = wrapper.find_all(["a", "span"], class_=lambda c: c and "history_item" in c.split())
        for node in candidates:
            if not isinstance(node, Tag):
                continue

            name_el = node.find("span", class_="history_item_name")
            name_text = clean_text(name_el.get_text(" ", strip=True)) if name_el else ""

            img_el = node.find("img")
            node_style = parse_style(node.get("style"))
            name_style = parse_style(name_el.get("style") if name_el else None)
            img_style = parse_style(img_el.get("style") if img_el else None)

            border_color = img_style.get("border-color", "") or node_style.get("border-color", "")
            background_color = img_style.get("background-color", "") or node_style.get("background-color", "")
            name_color = name_style.get("color", "")
            rarity_label = infer_rarity_from_name_color(name_color)
            rarity_class = rarity_class_from_label(rarity_label)

            item_url = node.get("href", "") if node.name == "a" else ""
            app_id = detect_item_app_id(item_url, name_text)

            item_img_url = img_el.get("src", "") if img_el else ""
            item_has_image = bool(item_img_url)

            # Some rows omit the <img> tag, but the page HTML still contains `g_rgHistoryInventory`.
            # When we have no image URL from the DOM, try reconstructing it from the embedded lookup.
            if not item_has_image and app_id and name_text:
                icon_url = icon_lookup.get((app_id, name_text))
                if icon_url:
                    item_img_url = f"{TRADE_ECON_IMAGE_BASE_URL}/{icon_url}/120x40"
                    item_has_image = True

            # Split gold into Contraband vs ★ Rare Special Item.
            if rarity_label == "Gold Grade":
                rarity_label, rarity_class = classify_gold_rarity(name_text)

            # Override TF2 material bucket: these are common crafting components
            # and should not inherit the generic TF2 weapon color.
            if app_id and str(app_id) == "440" and name_text in {
                "Refined Metal",
                "Scrap Metal",
                "Reclaimed Metal",
            }:
                rarity_label, rarity_class = "TF2 Metal", "tf2-metal"

            item_wear = wear_lookup.get((app_id, name_text), "")

            item = {
                "item_dom_id": node.get("id", "") or "",
                "item_name": name_text,
                "item_url": item_url,
                "item_img_url": item_img_url,
                "item_name_color": name_color,
                "item_border_color": border_color,
                "item_background_color": background_color,
                "item_has_image": item_has_image,
                "item_raw_classes": node.get("class", []),
                "app_id": app_id,
                "item_rarity": rarity_label,
                "item_rarity_class": rarity_class,
                "item_wear": item_wear,
            }

            if item["item_name"] or item["item_url"] or item["item_dom_id"]:
                items.append(item)

    return items


def extract_date_time(row: Tag) -> Dict[str, str]:
    date_div = row.find("div", class_="tradehistory_date")
    if date_div is None:
        return {"date_text": "", "time_text": "", "datetime_text": ""}

    timestamp_div = date_div.find("div", class_="tradehistory_timestamp")
    time_text = clean_text(timestamp_div.get_text(" ", strip=True)) if timestamp_div else ""

    date_clone = BeautifulSoup(str(date_div), "html.parser")
    ts_in_clone = date_clone.find("div", class_="tradehistory_timestamp")
    if ts_in_clone:
        ts_in_clone.decompose()
    date_text = clean_text(date_clone.get_text(" ", strip=True))

    datetime_text = clean_text(f"{date_text} {time_text}".strip())
    return {
        "date_text": date_text,
        "time_text": time_text,
        "datetime_text": datetime_text,
    }


def summarize_trade_app_ids(items: List[Dict[str, Any]]) -> List[str]:
    app_ids = sorted({item["app_id"] for item in items if item.get("app_id")})
    return app_ids


def classify_trade_game_scope(app_ids: List[str]) -> str:
    if not app_ids:
        return "unknown"
    if len(app_ids) == 1:
        return app_ids[0]
    return "mixed"


def parse_trade_row(
    row: Tag,
    row_index: int,
    icon_lookup: Dict[Tuple[str, str], str],
    wear_lookup: Dict[Tuple[str, str], str],
) -> Dict[str, Any]:
    date_time = extract_date_time(row)

    content = row.find("div", class_="tradehistory_content")
    if content is None:
        return {
            "trade_row_index": row_index,
            **date_time,
            "event_text": "",
            "partner_name": "",
            "partner_url": "",
            "partner_miniprofile": "",
            "partner_steamid64": "",
            "partner_avatar_profile_path": "",
            "direction_symbol": "",
            "direction": "unknown",
            "trade_protected": False,
            "trade_protected_text": "",
            "trade_protected_until_text": "",
            "items_count": 0,
            "is_empty_trade": True,
            "is_incomplete_trade_row": True,
            "trade_app_ids": [],
            "trade_game_scope": "unknown",
            "items": [],
        }

    partner_info = extract_partner_info(content)
    protection_info = extract_protection_info(content)
    direction_info = extract_direction(content)
    items = extract_items(content, icon_lookup, wear_lookup)

    items_count = len(items)
    is_empty_trade = items_count == 0
    is_incomplete_trade_row = items_count == 0 and direction_info["direction"] == "unknown"
    trade_app_ids = summarize_trade_app_ids(items)
    trade_game_scope = classify_trade_game_scope(trade_app_ids)

    return {
        "trade_row_index": row_index,
        **date_time,
        **partner_info,
        **direction_info,
        **protection_info,
        "items_count": items_count,
        "is_empty_trade": is_empty_trade,
        "is_incomplete_trade_row": is_incomplete_trade_row,
        "trade_app_ids": trade_app_ids,
        "trade_game_scope": trade_game_scope,
        "items": items,
    }


def parse_trade_page(html: str, input_filename: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    main = find_main_contents(soup)
    pager = extract_page_pager(main)
    rows = main.find_all("div", class_="tradehistoryrow")

    icon_lookup = extract_history_inventory_icon_lookup(html)
    wear_lookup = extract_history_inventory_wear_lookup(html)
    parsed_rows = [
        parse_trade_row(row, idx, icon_lookup, wear_lookup) for idx, row in enumerate(rows)
    ]

    # Light diagnostics to help us understand why certain item icons are missing.
    # This is written to the JSON so the Flask layer can show a short "re-parse reason" flash.
    icon_lookup_entries = len(icon_lookup)
    wear_lookup_entries = len(wear_lookup)

    items_missing_img_with_appid = 0
    items_missing_img_stickers = 0
    items_missing_img_tf2_materials = 0

    for tr in parsed_rows:
        for it in tr.get("items") or []:
            item_name = it.get("item_name") or ""
            appid = it.get("app_id") or ""
            item_img_url = it.get("item_img_url") or ""
            if appid and item_name and not item_img_url:
                items_missing_img_with_appid += 1
                if isinstance(item_name, str) and item_name.startswith("Sticker |"):
                    items_missing_img_stickers += 1
                if (
                    str(appid) == "440"
                    and item_name in {"Refined Metal", "Scrap Metal", "Reclaimed Metal"}
                ):
                    items_missing_img_tf2_materials += 1

    return {
        "source_file": input_filename,
        "page": {
            "cursor_href": pager["cursor_href"],
            "after_time": pager["after_time"],
            "after_trade": pager["after_trade"],
            "summary_text": pager["summary_text"],
            "trade_rows_count": len(parsed_rows),
        },
        "trades": parsed_rows,
        "diagnostics": {
            "icon_lookup_entries": icon_lookup_entries,
            "wear_lookup_entries": wear_lookup_entries,
            "items_missing_img_with_appid": items_missing_img_with_appid,
            "items_missing_img_stickers": items_missing_img_stickers,
            "items_missing_img_tf2_materials": items_missing_img_tf2_materials,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse one Steam trade history HTML page into structured JSON.")
    parser.add_argument("input_html", help="Path to one saved Steam trade history HTML page")
    parser.add_argument("-o", "--output", help="Output JSON file path. Defaults to stdout.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    input_path = Path(args.input_html)
    if not input_path.is_file():
        print(f"ERROR: file not found: {input_path}", file=sys.stderr)
        return 1

    html = input_path.read_text(encoding="utf-8", errors="ignore")
    parsed = parse_trade_page(html, input_path.name)

    json_text = json.dumps(parsed, ensure_ascii=False, indent=2 if args.pretty or args.output else 2)

    if args.output:
        Path(args.output).write_text(json_text + "\n", encoding="utf-8")
    else:
        print(json_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
