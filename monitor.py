#!/usr/bin/env python3
"""
One Piece TCG stock monitor for Greek stores.

Scrapes the One Piece listing page of each store, normalizes the stock status of
every OP main-set box and EB extra-booster box, compares it to the previous run
(state.json) and sends a Telegram message for:
  - a newly listed product (e.g. OP-20 appears / pre-order opens somewhere)
  - a status change (SOLD_OUT -> PREORDER_OPEN / IN_STOCK = became orderable)
  - a price change

Fetching uses curl_cffi (impersonates a real Chrome TLS fingerprint) so it gets
past the Cloudflare/WAF protection on animeworld and rollntrade.
"""

import json
import os
import re
import sys
import time
import urllib.parse

from curl_cffi import requests as cf
from bs4 import BeautifulSoup

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Track only OP (main sets) and EB (extra boosters) BOXES - not singles, packs,
# starter decks, double packs or illustration boxes.
TRACK_PREFIXES = ("OP", "EB", "PRB")

# Only English boxes: skip anything flagged as Japanese / non-English / Asia-region.
EXCLUDE_PATTERN = re.compile(r"japanese|japan|\bjp\b|non[-\s]?english|asia[-\s]region|asian", re.I)

# A tracked product's name must contain one of these "box" hints. Stores word it
# very differently, so we cover them all (incl. Greek "Κουτί"):
#   eFantasy   "OP-20 Booster Box (24 packs)"
#   AnimeWorld "Box One Piece Card Game [OP18] (ENG)"
#   RollnTrade "OP-17 ... Booster Κουτί (24 Πακέτα)"
BOX_HINT = re.compile(r"\bbox\b|κουτ|\(24\s*(?:packs|πακ)|booster\s*box|display", re.I)

# Matches every wording of a set code, upper/lower, with/without dash/zeros/brackets:
# OP18 OP-18 op 18 [OP18] (OP18) EB6 EB06 eb-06 ... numbers up to 999 (future-proof).
# Nothing is hard-coded: the number is captured dynamically and normalized to NN.
CODE_RE = re.compile(r"(?<![A-Za-z])(OP|EB|ST|PRB|DP|IB)[-\s_–—]*0*(\d{1,3})(?![0-9])", re.I)
QUARTER_RE = re.compile(r"Q[1-4],?\s*20\d\d", re.I)

# Ignore old sets: track OP only from 17 up, EB only from 06 up. (Overridden if
# WATCH_CODES is set.) Change these numbers to widen/narrow the range.
MIN_SET = {"OP": 17, "EB": 6, "PRB": 3}

# Optional: only alert for these exact codes, e.g. "OP-20,EB-06". Empty = use the
# MIN_SET ranges above for all OP/EB boxes.
WATCH_CODES = [c.strip().upper() for c in os.environ.get("WATCH_CODES", "").split(",") if c.strip()]

IN_STOCK = "IN_STOCK"
PREORDER_OPEN = "PREORDER_OPEN"
SOLD_OUT = "SOLD_OUT"
STATUS_EMOJI = {IN_STOCK: "✅", PREORDER_OPEN: "🟡", SOLD_OUT: "⛔"}
STATUS_GR = {IN_STOCK: "διαθέσιμο τώρα", PREORDER_OPEN: "pre-order ΑΝΟΙΧΤΟ", SOLD_OUT: "sold out"}


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def extract_code(name):
    m = CODE_RE.search(name or "")
    return f"{m.group(1).upper()}-{int(m.group(2)):02d}" if m else None


def sale_price(text):
    """From a WooCommerce .price block (may contain original + sale), keep the
    last euro amount = the actual current price."""
    text = clean(text)
    amounts = re.findall(r"[\d.,]+\s*€", text)
    return clean(amounts[-1]) if amounts else text


# --------------------------------------------------------------------------- #
# Parsers  ->  list of {store, url, name, code, price, status, release}
# --------------------------------------------------------------------------- #

def parse_efantasy(html, base, store):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for el in soup.select("div.product"):
        link = el.select_one(".product-title a")
        if not link:
            continue
        name = clean(link.get_text(" ", strip=True))
        stock_txt = clean((el.select_one(".product-stock span") or el).get_text(" ")).lower()
        if "preorder" in stock_txt:
            status = PREORDER_OPEN
        else:
            m = re.search(r"available:\s*([0-9]+)", stock_txt)
            if m:
                status = SOLD_OUT if int(m.group(1)) == 0 else IN_STOCK
            elif "10+" in stock_txt:
                status = IN_STOCK
            else:
                status = SOLD_OUT
        price_el = el.select_one(".product-price strong")
        q = QUARTER_RE.search(el.get_text(" ", strip=True))
        items.append({
            "store": store, "name": name,
            "url": urllib.parse.urljoin(base, link.get("href", "")),
            "code": extract_code(name),
            "price": clean(price_el.get_text()) if price_el else "",
            "status": status, "release": q.group(0) if q else "",
        })
    return items


def parse_woocommerce(html, base, store):
    """Any WooCommerce shop (animeworld, rollntrade, mythicvault). Stock is encoded
    in the product card's CSS class: instock / onbackorder / outofstock. Handles
    both li.product (standard) and div.product.product-grid-item (WoodMart)."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for el in soup.select("li.product, div.product"):
        classes = el.get("class", [])
        if "outofstock" in classes:
            status = SOLD_OUT
        elif "onbackorder" in classes:
            status = PREORDER_OPEN
        elif "instock" in classes:
            status = IN_STOCK
        else:
            continue
        title = el.select_one(".wd-entities-title, .woocommerce-loop-product__title, h2, h3")
        name = clean(title.get_text(" ", strip=True)) if title else ""
        link = (el.select_one("a.wd-product-img-link, a.woocommerce-LoopProduct-link")
                or (title.find_parent("a") if title else None)
                or el.find("a", href=True))
        price_el = el.select_one(".price")
        items.append({
            "store": store, "name": name,
            "url": urllib.parse.urljoin(base, link.get("href", "")) if link else "",
            "code": extract_code(name),
            "price": sale_price(price_el.get_text(" ")) if price_el else "",
            "status": status, "release": "",
        })
    return items


def parse_cardshive(html, base, store):
    """OpenCart shop (cardshive). The listing does NOT expose stock status, so we
    track presence: a listed box is treated as available. (No reliable sold-out
    detection here - see README.)"""
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for el in soup.select(".product-thumb"):
        link = el.select_one(".caption .name a, .name a, .caption a")
        if not link:
            continue
        url = urllib.parse.urljoin(base, link.get("href", ""))
        if url in seen:
            continue
        seen.add(url)
        name = clean(link.get_text(" ", strip=True))
        price_el = el.select_one(".price-new") or el.select_one(".price")
        price = clean(price_el.get_text(" ")) if price_el else ""
        m = re.search(r"[\d.,]+\s*€", price)
        items.append({
            "store": store, "name": name, "url": url,
            "code": extract_code(name),
            "price": m.group(0).replace(" ", "") if m else price,
            "status": IN_STOCK, "release": "",
        })
    return items


STORES = [
    # eFantasy shows the whole One Piece category on a single page -> no pagination.
    {"name": "eFantasy", "parser": parse_efantasy, "paginate": False,
     "url": "https://www.efantasy.gr/en/products/card-games/sc-2183-one-piece-card-game/sort=id-desc"},
    # WooCommerce archives split products across pages (/page/2/, /page/3/ ...),
    # so we must follow every page - the boxes can be on any of them.
    {"name": "AnimeWorld", "parser": parse_woocommerce, "paginate": True,
     "url": "https://animeworld.gr/brand/one-piece-tcg/"},
    {"name": "RollnTrade", "parser": parse_woocommerce, "paginate": True,
     "url": "https://rollntrade.com/el/product-category/paichnidia-karton-el/one-piece-paichnidi-karton-synallagon/"},
    # MythicVault: WooCommerce behind Cloudflare, but curl_cffi gets through.
    # Use the "sealed boxes" sub-category (boxes only).
    # MythicVault: use the CATEGORY price (the public price a normal visitor sees).
    # Do NOT read the product page price - a membership plugin (pmpro) puts a hidden
    # member-only price there that regular buyers never get.
    {"name": "MythicVault", "parser": parse_woocommerce, "paginate": True,
     "url": "https://mythicvault.com/el/product-category/trading-card-games-el/paichnidi-karton-one-piece/sfragismena-koutia-el-3-2/"},
    # CardsHive: OpenCart. Listing does not expose stock -> presence-based (status
    # assumed available). Pagination uses ?page=N (query), not /page/N/.
    {"name": "CardsHive", "parser": parse_cardshive, "paginate": True, "page_style": "query",
     "url": "https://www.cardshive.gr/tcg/tcg-boxes/one-piece-boxes/"},
]


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

def fetch(url):
    headers = {"Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
               "Referer": urllib.parse.urljoin(url, "/")}
    r = cf.get(url, headers=headers, impersonate="chrome", timeout=40)
    r.raise_for_status()
    return r.text


def page_url(store, n):
    """Build the URL for page n. WooCommerce uses /page/N/ (path); OpenCart uses
    ?page=N (query)."""
    base = store["url"]
    if n == 1:
        return base
    if store.get("page_style") == "query":
        return f"{base}{'&' if '?' in base else '?'}page={n}"
    return f"{base}page/{n}/"


def scrape(store, max_pages=20):
    """Fetch a store's products. For paginated stores, follow every page until one
    brings nothing new (or 404s)."""
    if not store.get("paginate"):
        return store["parser"](fetch(store["url"]), store["url"], store["name"])
    items, seen = [], set()
    for n in range(1, max_pages + 1):
        url = page_url(store, n)
        try:
            page = store["parser"](fetch(url), url, store["name"])
        except Exception:
            break  # 404 past the last page
        new = [it for it in page if it["url"] not in seen]
        if not new:
            break  # empty page or redirected back to page 1
        seen.update(it["url"] for it in new)
        items += new
        time.sleep(0.5)
    return items


def is_tracked(item):
    code = item["code"] or ""
    if not code.startswith(TRACK_PREFIXES):
        return False
    if EXCLUDE_PATTERN.search(item["name"]):
        return False  # English boxes only
    if not BOX_HINT.search(item["name"]):
        return False
    if WATCH_CODES:
        return code in WATCH_CODES
    prefix, _, num = code.partition("-")
    if prefix in MIN_SET and num.isdigit() and int(num) < MIN_SET[prefix]:
        return False  # older than the range we care about
    return True


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("!! Telegram not configured, printing instead:\n" + text + "\n")
        return
    r = cf.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},  # no big link previews
                impersonate="chrome", timeout=30)
    if not r.ok:
        print(f"!! Telegram error {r.status_code}: {r.text}")


def price_value(p):
    """Parse '139,99€' / '140,00 €' -> float, for finding the cheapest store."""
    m = re.search(r"[\d.,]+", p or "")
    if not m:
        return None
    s = m.group(0)
    s = s.replace(".", "").replace(",", ".") if ("," in s and "." in s) else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def buy_section(code, current, by_code):
    """Where-to-buy block for a set code. If 2+ stores have it orderable, show a
    price comparison with 🏆 on the cheapest; else a single link."""
    orderable = sorted(by_code.get(code, []), key=lambda x: (price_value(x["price"]) or 1e12))
    if len(orderable) >= 2:
        out = ["", "💰 Σύγκριση:"]
        for i, e in enumerate(orderable):
            out.append(f"{'🏆 ' if i == 0 else ''}{e['store']} — {e['price']}\n🛒 {e['url']}")
        return "\n".join(out)
    if len(orderable) == 1:
        e = orderable[0]
        return f"\n💶 {e['price']} · {e['store']}\n🛒 {e['url']}"
    e = current  # nothing orderable (e.g. new sold-out) -> show the item's own price/link
    return f"\n💶 {e['price']} · {e['store']}\n🛒 {e['url']}"


def main():
    prev = load_state()
    first_run = not prev  # empty state -> seed silently, no notification burst
    new_state = {}
    all_tracked = []

    for store in STORES:
        try:
            items = scrape(store)
        except Exception as e:
            print(f"!! {store['name']} failed: {e}")
            new_state.update({k: v for k, v in prev.items() if v.get("store") == store["name"]})
            continue
        tracked = [it for it in items if is_tracked(it)]
        print(f"{store['name']}: {len(items)} items, {len(tracked)} tracked boxes")
        for it in tracked:
            if it["url"] in new_state:
                continue  # de-dup same product listed twice on a page
            new_state[it["url"]] = {k: it[k] for k in ("store", "name", "code", "price", "status", "release")}
            all_tracked.append(it)
        time.sleep(1)

    if first_run:
        orderable = sorted(((k, v) for k, v in new_state.items() if v["status"] != SOLD_OUT),
                           key=lambda kv: (kv[1]["store"], kv[1]["code"] or ""))
        if orderable:
            lines = ["✅ <b>Ξεκίνησε η παρακολούθηση OP TCG</b>",
                     "Διαθέσιμα / pre-order αυτή τη στιγμή:", ""]
            for url, v in orderable:
                lines.append(f"{STATUS_EMOJI[v['status']]} <b>{v['code']}</b> — {v['price']} · {v['store']}\n🛒 {url}")
            lines.append("\nΑπό δω και πέρα: μήνυμα μόνο όταν κάτι αλλάζει.")
            send_telegram("\n".join(lines))
        save_state(new_state)
        print(f"First run: seeded {len(new_state)} products; digest of {len(orderable)} orderable sent.")
        return 0

    # price-comparison map: code -> list of orderable items across all stores
    by_code = {}
    for it in all_tracked:
        if it["status"] != SOLD_OUT:
            by_code.setdefault(it["code"], []).append(it)

    messages, notified = [], set()  # notified: codes already alerted as orderable this run
    for it in all_tracked:
        old = prev.get(it["url"])
        emoji, label = STATUS_EMOJI.get(it["status"], ""), STATUS_GR[it["status"]]
        rel = f" — release {it['release']}" if it["release"] else ""
        orderable_event = ((old is None or old["status"] == SOLD_OUT) and it["status"] != SOLD_OUT)
        if orderable_event and it["code"] in notified:
            continue  # avoid duplicate messages for the same set in one run

        if old is None:
            messages.append(f"🆕 <b>{it['name']}</b>\n{emoji} {label} — {it['price']}{rel}"
                            + buy_section(it["code"], it, by_code))
        elif old["status"] != it["status"]:
            head = "🟢 ΔΙΑΘΕΣΙΜΟ" if orderable_event else "🔔 Αλλαγή"
            messages.append(f"{head} <b>{it['name']}</b>\n"
                            f"{STATUS_EMOJI.get(old['status'],'')} {STATUS_GR[old['status']]} → {emoji} {label}{rel}"
                            + buy_section(it["code"], it, by_code))
        elif old.get("release") != it["release"] and it["release"]:
            messages.append(f"📅 <b>{it['code']}</b> — ημ/νία κυκλοφορίας: "
                            f"{old.get('release') or '—'} → {it['release']}\n🛒 {it['store']}: {it['url']}")
        elif old.get("price") != it["price"] and it["price"]:
            messages.append(f"💶 Αλλαγή τιμής <b>{it['name']}</b>\n{old.get('price')} → {it['price']}"
                            + buy_section(it["code"], it, by_code))
        else:
            continue
        if orderable_event:
            notified.add(it["code"])

    for msg in messages:
        send_telegram(msg)
        time.sleep(0.5)

    save_state(new_state)
    print(f"Done. {len(messages)} notification(s), {len(new_state)} tracked products.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
