"""
Scryfall integration.

Turns a user-provided reference (a scryfall.com card link, an api URL, a
direct image URL, or just a card name) into one or more downloaded PNG
images ready to be upscaled.
"""

import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests

from config import (
    SCRYFALL_API,
    SCRYFALL_HEADERS,
    SCRYFALL_DELAY,
    TEMP_FOLDER,
)


class ScryfallError(Exception):
    pass


# Scryfall language codes (used to detect the /lang/ segment in card URLs)
SCRYFALL_LANGS = {
    "en", "es", "fr", "de", "it", "pt", "ja", "ko", "ru",
    "zhs", "zht", "he", "la", "grc", "ar", "sa", "ph", "qya",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Filesystem-safe version of a card name (keeps spaces, drops junk)."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _get(url: str, **kwargs) -> requests.Response:
    time.sleep(SCRYFALL_DELAY)
    r = requests.get(url, headers=SCRYFALL_HEADERS, timeout=30, **kwargs)
    return r


def looks_like_scryfall(text: str) -> bool:
    text = text.strip().lower()
    return (
        "scryfall.com" in text
        or "scryfall.io" in text
        or not _is_path(text)
    )


def _is_path(text: str) -> bool:
    try:
        return Path(text).exists()
    except OSError:
        return False


# --------------------------------------------------------------------------
# resolving a reference to a Scryfall card object
# --------------------------------------------------------------------------

def _card_from_reference(ref: str) -> dict:
    ref = ref.strip()

    # 1) A scryfall.com card page URL:
    #    English:  https://scryfall.com/card/<set>/<number>/<slug>
    #    Foreign:  https://scryfall.com/card/<set>/<number>/<lang>/<slug>
    m = re.search(
        r"scryfall\.com/card/([^/?#]+)/([^/?#]+)(?:/([^/?#]+))?", ref, re.I)
    if m:
        setcode, number, third = m.group(1), m.group(2), m.group(3)
        lang = third.lower() if third and third.lower() in SCRYFALL_LANGS else None

        url = f"{SCRYFALL_API}/cards/{setcode}/{number}"
        if lang and lang != "en":
            url += f"/{lang}"

        r = _get(url)
        if r.status_code != 200:
            where = f"{setcode}/{number}" + (f"/{lang}" if lang else "")
            raise ScryfallError(f"Card not found for {where}")
        return r.json()

    # 2) An api.scryfall.com/cards/... URL -> hit it directly
    if "api.scryfall.com/cards" in ref:
        r = _get(ref)
        if r.status_code != 200:
            raise ScryfallError(f"Scryfall API returned {r.status_code}")
        return r.json()

    # 3) A decklist-style line "Name (SET) number [tag]" -> EXACT printing.
    #    This is what identifies alternate arts / promos, so it must win over
    #    the fuzzy name search (which would return the default printing).
    entries, _ = parse_decklist(ref)
    if entries:
        e = entries[0]
        r = _get(f"{SCRYFALL_API}/cards/{e['set']}/{e['number']}")
        if r.status_code == 200:
            return r.json()
        # fall through to name search if that exact printing doesn't exist

    # 4) A plain card name -> fuzzy named search
    r = _get(f"{SCRYFALL_API}/cards/named", params={"fuzzy": ref})
    if r.status_code == 404:
        raise ScryfallError(f'No card matched "{ref}"')
    if r.status_code != 200:
        raise ScryfallError(f"Scryfall API returned {r.status_code}")
    return r.json()


def _png_urls(card: dict) -> list[tuple[str, str]]:
    """
    Returns a list of (image_url, suffix) for a card.
    Double-faced cards yield two entries (front / back).
    """
    results = []

    if "image_uris" in card and card["image_uris"].get("png"):
        results.append((card["image_uris"]["png"], ""))
    elif "card_faces" in card:
        faces = card["card_faces"]
        labels = ["-front", "-back"] if len(faces) == 2 else \
                 [f"-face{i+1}" for i in range(len(faces))]
        for face, label in zip(faces, labels):
            uris = face.get("image_uris") or {}
            if uris.get("png"):
                results.append((uris["png"], label))

    if not results:
        raise ScryfallError("No PNG image available for this card")

    return results


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def fetch(ref: str, status_callback=None) -> tuple[list[Path], dict]:
    """
    Resolve `ref` and download the full-resolution PNG(s) into TEMP_FOLDER.

    Returns (paths, meta):
      paths -> local files (one per card face)
      meta  -> {"released_at": "YYYY-MM-DD" | None, "set": code | None}
               (used by Auto mode to pick the model)
    """
    ref = ref.strip()

    # Direct image URL (cards.scryfall.io/...png) -> just download it
    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https") and parsed.path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp")
    ):
        if status_callback:
            status_callback("Downloading image...")
        name = Path(parsed.path).name
        return [_download(ref, TEMP_FOLDER / name)], {"released_at": None, "set": None}

    # Gatherer links resolve via multiverse id / set+number+lang, with the
    # Gatherer image itself as a last-resort fallback
    g = _gatherer_parse(ref)
    if g:
        return _fetch_gatherer(g, status_callback)

    if status_callback:
        status_callback("Looking up card...")

    card = _card_from_reference(ref)

    # keep the finish marker (*E* / *F*) in the filename if one was given
    _, finish = _extract_tags(ref)

    return _download_card(card, status_callback, finish)


def _download_card(card: dict, status_callback=None, finish=None):
    """Download all faces of a resolved card object. Returns (paths, meta)."""
    base = f"{_slug(card['name'])}-{card.get('set','')}-{card.get('collector_number','')}"
    if card.get("lang") and card["lang"] != "en":
        base += f"-{card['lang']}"
    if finish:
        base += f"-{finish}"

    paths = []
    for url, label in _png_urls(card):
        target = TEMP_FOLDER / f"{base}{label}.png"
        if status_callback:
            status_callback(f"Downloading {card['name']}{label}...")
        paths.append(_download(url, target))

    return paths, {"released_at": card.get("released_at"), "set": card.get("set")}


def _download(url: str, target: Path) -> Path:
    r = _get(url)
    if r.status_code != 200:
        raise ScryfallError(f"Download failed ({r.status_code})")
    target.write_bytes(r.content)
    return target


# ==========================================================================
# GATHERER LINKS
# ==========================================================================
# Two URL styles:
#   classic: gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid=226399
#   new:     gatherer.wizards.com/M11/ja-jp/149/lightning-bolt
#
# Resolution: prefer Scryfall (its /cards/multiverse/{id} endpoint keeps the
# printed language, and its images are the best available). If Scryfall lacks
# the card or its image, fall back to Gatherer's own image handler, which
# serves 744px WEBP since the 2024+ redesign.

GATHERER_IMAGE = ("https://gatherer.wizards.com/Handlers/Image.ashx"
                  "?multiverseid={mid}&type=card")

# gatherer locale segment -> scryfall language code
_GATHERER_LOCALES = {
    "en-us": "en", "ja-jp": "ja", "ko-kr": "ko", "ru-ru": "ru",
    "zh-cn": "zhs", "zh-tw": "zht", "pt-br": "pt", "es-es": "es",
    "fr-fr": "fr", "de-de": "de", "it-it": "it",
}


def _gatherer_parse(ref: str):
    """Returns {'mid': int} or {'set','number','lang'} for Gatherer URLs."""
    if "gatherer.wizards.com" not in ref.lower():
        return None

    m = re.search(r"multiverseid=(\d+)", ref, re.I)
    if m:
        return {"mid": int(m.group(1))}

    m = re.search(
        r"gatherer\.wizards\.com/([^/?#]+)/([^/?#]+)/([^/?#]+)", ref, re.I)
    if m and m.group(1).lower() not in ("pages", "handlers"):
        setcode, locale, number = m.groups()
        lang = _GATHERER_LOCALES.get(locale.lower(), locale.lower()[:2])
        return {"set": setcode.lower(), "number": number, "lang": lang}

    return None


def _fetch_gatherer(g: dict, status_callback=None):
    if status_callback:
        status_callback("Resolving Gatherer link...")

    card = None
    if "mid" in g:
        r = _get(f"{SCRYFALL_API}/cards/multiverse/{g['mid']}")
        if r.status_code == 200:
            card = r.json()
    else:
        url = f"{SCRYFALL_API}/cards/{g['set']}/{g['number']}"
        if g["lang"] and g["lang"] != "en":
            url += f"/{g['lang']}"
        r = _get(url)
        if r.status_code == 200:
            card = r.json()

    if card:
        try:
            return _download_card(card, status_callback)
        except ScryfallError:
            # Scryfall knows the card but has no image -> try Gatherer's
            mids = card.get("multiverse_ids") or []
            if "mid" in g:
                mids = [g["mid"]] + mids
            if mids:
                base = f"{_slug(card['name'])}-{card.get('set','')}-{card.get('collector_number','')}"
                if card.get("lang") and card["lang"] != "en":
                    base += f"-{card['lang']}"
                paths = _gatherer_image(mids[0], base, status_callback)
                return paths, {"released_at": card.get("released_at"),
                               "set": card.get("set")}
            raise

    # Scryfall doesn't know this printing at all
    if "mid" in g:
        paths = _gatherer_image(g["mid"], f"gatherer-{g['mid']}", status_callback)
        return paths, {"released_at": None, "set": None}

    raise ScryfallError("Card not found on Scryfall for that Gatherer link")


def _gatherer_image(mid: int, base: str, status_callback=None) -> list[Path]:
    """Download Gatherer's card image (webp) and convert it to PNG."""
    import io
    from PIL import Image

    if status_callback:
        status_callback("Downloading from Gatherer...")

    r = requests.get(
        GATHERER_IMAGE.format(mid=mid),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=30,
    )
    if r.status_code != 200 or r.content[:5].lower().startswith(b"<html"):
        raise ScryfallError(f"Gatherer image not available for id {mid}")

    target = TEMP_FOLDER / f"{base}.png"
    Image.open(io.BytesIO(r.content)).convert("RGBA").save(target, "PNG")
    return [target]


# ==========================================================================
# DECKLIST IMPORT
# ==========================================================================
# Parses lines exported by deckbuilders / proxy sites, e.g.:
#
#     1 Winota, Joiner of Forces (PRM) 80807 [matte]
#     3 Plains (MSC) 866 [matte]
#     1 Ajani, Nacatl Pariah // Ajani, Nacatl Avenger (MH3) 442 [matte]
#
# and resolves each to the EXACT printing on Scryfall via set + collector
# number (so promos / alternate arts come out right, not the default print).

# qty (optional) | name | (SET) | collector-number | [tag] (optional, ignored)
# Trailing markers we accept and ignore for lookup, in any order/quantity:
#   [matte] [foil] ...   -> print-finish notes from the proxy site
#   *E* *F* ...          -> deckbuilder markers (Etched / Foil)
# The collector number already identifies the exact printing (an etched card
# has its own number), so these only affect the label we show.
_TAGS_RE = re.compile(r"(?:\s+(?:\[[^\]]*\]|\*[^*]*\*))+\s*$")

_DECK_RE = re.compile(
    r"^\s*(?:(\d+)\s*[xX]?\s+)?(.+?)\s+\(([^)]+)\)\s+(\S+)\s*$"
)

_FINISH_LABELS = {"E": "etched", "F": "foil", "S": "surge foil"}


def _extract_tags(s: str):
    """Split trailing [..] / *..* markers off a line. Returns (line, finish)."""
    finish = None
    m = _TAGS_RE.search(s)
    if m:
        tags = m.group(0)
        s = s[: m.start()].strip()
        for code, label in _FINISH_LABELS.items():
            if f"*{code}*" in tags.upper():
                finish = label
                break
    return s, finish


def parse_decklist(text: str):
    """Return (entries, bad_lines). Entry: dict(qty, name, set, number, finish)."""
    entries, bad = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        s, finish = _extract_tags(s)
        m = _DECK_RE.match(s)
        if not m:
            bad.append(line.strip())
            continue
        qty = int(m.group(1)) if m.group(1) else 1
        entries.append({
            "qty": qty,
            "name": m.group(2).strip(),
            "set": m.group(3).strip().lower(),
            "number": m.group(4).strip(),
            "finish": finish,
        })
    return entries, bad


def _post_collection(identifiers: list[dict]) -> dict:
    time.sleep(SCRYFALL_DELAY)
    r = requests.post(
        f"{SCRYFALL_API}/cards/collection",
        headers={**SCRYFALL_HEADERS, "Content-Type": "application/json"},
        json={"identifiers": identifiers},
        timeout=30,
    )
    if r.status_code != 200:
        raise ScryfallError(f"Scryfall collection API returned {r.status_code}")
    return r.json()


def resolve_decklist(text: str, status_callback=None):
    """
    Parse and resolve a decklist.

    Returns (cards, not_found, bad_lines) where each card is:
        {
          "display":   "3x Plains"  (or just the name),
          "qty":       int,
          "downloads": [(basename, png_url), ...]   # 2 entries for DFCs
        }
    """
    entries, bad = parse_decklist(text)

    # dedupe by (set, number), summing quantities, preserving order
    seen, order = {}, []
    for e in entries:
        key = (e["set"], e["number"])
        if key in seen:
            seen[key]["qty"] += e["qty"]
        else:
            seen[key] = dict(e)
            order.append(key)

    ids = [{"set": s, "collector_number": n} for (s, n) in order]

    resolved = {}
    not_found = []
    for i in range(0, len(ids), 75):
        chunk = ids[i:i + 75]
        if status_callback:
            status_callback(f"Resolving cards {i + len(chunk)}/{len(ids)}…")
        data = _post_collection(chunk)
        for c in data.get("data", []):
            resolved[(c["set"], c["collector_number"])] = c

    cards = []
    for key in order:
        e = seen[key]
        c = resolved.get(key)
        if not c:
            not_found.append(f'{e["name"]} ({e["set"].upper()}) {e["number"]}')
            continue

        base = f"{_slug(c['name'])}-{c['set']}-{c['collector_number']}"
        if c.get("lang") and c["lang"] != "en":
            base += f"-{c['lang']}"
        if e.get("finish"):
            base += f"-{e['finish']}"

        try:
            downloads = [(f"{base}{label}", url) for url, label in _png_urls(c)]
        except ScryfallError:
            not_found.append(f'{c["name"]} ({c["set"].upper()}) {c["collector_number"]} — no image')
            continue

        display = f'{e["qty"]}x {c["name"]}' if e["qty"] > 1 else c["name"]
        if e.get("finish"):
            display += f' ({e["finish"]})'
        cards.append({
            "display": display,
            "qty": e["qty"],
            "downloads": downloads,
            "released_at": c.get("released_at"),
            "set": c.get("set"),
        })

    return cards, not_found, bad


def download_to_temp(basename: str, url: str) -> Path:
    """Download a resolved PNG url into TEMP_FOLDER as <basename>.png."""
    return _download(url, TEMP_FOLDER / f"{basename}.png")


# ==========================================================================
# DECK SITE URLS (Archidekt / Moxfield)
# ==========================================================================

def deck_url_kind(text: str):
    """'archidekt' | 'moxfield' | None for a pasted URL."""
    t = text.strip().lower()
    if "archidekt.com/decks/" in t:
        return "archidekt"
    if "moxfield.com/decks/" in t:
        return "moxfield"
    return None


def fetch_archidekt(url: str, status_callback=None) -> str:
    """
    Fetch a public Archidekt deck and return it as decklist text
    ("N Name (SET) number"), ready for resolve_decklist().
    """
    m = re.search(r"archidekt\.com/decks/(\d+)", url, re.I)
    if not m:
        raise ScryfallError("Could not find a deck id in that Archidekt URL")

    if status_callback:
        status_callback("Fetching deck from Archidekt...")

    r = requests.get(f"https://archidekt.com/api/decks/{m.group(1)}/",
                     headers={"User-Agent": SCRYFALL_HEADERS["User-Agent"]},
                     timeout=30)
    if r.status_code != 200:
        raise ScryfallError(f"Archidekt returned {r.status_code} "
                            "(is the deck public?)")
    deck = r.json()

    lines = []
    for entry in deck.get("cards", []):
        qty = entry.get("quantity", 1)
        cats = entry.get("categories") or []
        if any(c.lower() in ("maybeboard", "sideboard considering") for c in cats):
            continue
        card = entry.get("card") or {}
        oracle = card.get("oracleCard") or {}
        name = oracle.get("name") or card.get("displayName") or ""
        setcode = (card.get("edition") or {}).get("editioncode") or ""
        number = card.get("collectorNumber") or ""
        if name and setcode and number:
            lines.append(f"{qty} {name} ({setcode.upper()}) {number}")

    if not lines:
        raise ScryfallError("No printable cards found in that deck")
    return "\n".join(lines)
