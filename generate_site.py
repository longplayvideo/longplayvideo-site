#!/usr/bin/env python3
"""
Builds the Long Play Video static site into _site/ from config.json + the
show's Spotify RSS feed. Run manually with `python generate_site.py`, or let
the GitHub Actions workflow (.github/workflows/deploy.yml) run it on a
schedule so the site keeps itself up to date with no manual work.
"""
import html
import json
import os
import re
import shutil
import sys
import urllib.parse
from datetime import datetime

import feedparser
from jinja2 import Environment, FileSystemLoader

try:
    import requests
except ImportError:
    requests = None

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "_site")

# Matches "S02 E36 - #65 Beverley Hills Cop" -> season=2, rank=65, film="Beverley Hills Cop".
# The "Ep?" tolerates both "E36" and "Ep36" since episode titles haven't always
# been typed consistently - without this, an odd-one-out title silently loses
# its season/rank/cover art/tags instead of erroring loudly, so it's worth
# being forgiving here rather than relying on perfectly consistent typing.
TITLE_RE = re.compile(r"S0?(\d+)\s*Ep?\d+\s*-\s*#\s*(\d+)\s*(.+)")

# Fallback for episodes that don't cover a single ranked film - catch-up/recap
# formats like "S01 E96 - Roll of the Dice #2" or "S02 E00 - Intro to Season 2"
# have a season/episode number but no "#rank" - without this fallback these
# episodes parse to season=None and silently drop out of their season's page
# entirely (they still exist, just filed under an unlabelled "season 0" bucket
# instead of showing up under Season 1/2 where a reader would expect them).
SEASON_EP_RE = re.compile(r"^S0?(\d+)\s*Ep?\d+\s*-\s*", re.I)
# Catches "Season 1 Finale - The Rundown" style titles, which have no episode
# number at all, just a season name.
SEASON_FINALE_RE = re.compile(r"Season\s*(\d+)\s*Finale", re.I)

# Optional trailing "(Film Title)" on a catch-up/recap episode that has no
# "#rank" - e.g. "S01 E96 - Roll of the Dice #2 (Grosse Pointe Blank)" - lets
# that episode get real cover art, an IMDb link and Top Trumps/drink matching
# even though it isn't an official ranked pick. Add this to a Spotify episode
# title any time and the next rebuild will pick it up automatically.
PAREN_FILM_RE = re.compile(r"\(([^)]+)\)\s*$")


def slugify(text):
    """'The Great Mouse Detective' -> 'the-great-mouse-detective', for anchor links."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "card"


def normalize_title(text):
    """Loose match key for comparing film titles regardless of punctuation/casing."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    """
    Strips HTML tags and unescapes entities from RSS show-notes text.
    Podcast descriptions often contain real HTML markup (bold/italic,
    links, a "Quote:" callout) - that's fine in a podcast app, but naively
    slicing the raw string at a fixed character count (for the compact
    card preview) can cut off mid-tag and leave dangling/unclosed markup
    that bleeds formatting into the rest of the card. Stripping tags first
    keeps the preview plain, clean text everywhere it's used.
    """
    text = TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def load_config():
    with open(os.path.join(ROOT, "config.json")) as f:
        return json.load(f)


def parse_episode_title(title):
    """
    Pull season number, rank, and film name out of an episode title.
    Most episodes are "S01 E36 - #65 Film Title" (season + rank + film).
    Recap/catch-up episodes like "S01 E96 - Roll of the Dice #2" or
    "S02 E00 - Intro to Season 2" have a season but no single ranked film -
    these still get tagged with their season (so they show up on the right
    Season page) but rank comes back None. "Season 1 Finale - The Rundown"
    has no episode number at all, just a season name.

    For these no-rank cases, an optional trailing "(Film Title)" on the
    title - e.g. "S01 E96 - Roll of the Dice #2 (Grosse Pointe Blank)" -
    is used as the film name instead of the generic recap text, so the
    episode still gets real cover art / IMDb / Top Trumps matching even
    though it's not an official ranked pick.
    """
    m = TITLE_RE.search(title)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3).strip()

    paren_match = PAREN_FILM_RE.search(title)
    paren_film = paren_match.group(1).strip() if paren_match else None

    m2 = SEASON_EP_RE.match(title)
    if m2:
        cleaned = SEASON_EP_RE.sub("", title, count=1).strip()
        return int(m2.group(1)), None, paren_film or cleaned or title

    m3 = SEASON_FINALE_RE.search(title)
    if m3:
        return int(m3.group(1)), None, paren_film or title

    return None, None, paren_film or title


def imdb_search_link(film_title):
    """Zero-setup fallback that always works, no API key required."""
    return "https://www.imdb.com/find/?q=" + urllib.parse.quote(film_title)


def fetch_cover(film_title, api_key, cache):
    """
    Look up a film's poster + IMDb ID via the free OMDb API
    (https://www.omdbapi.com/apikey.aspx). Falls back to None/None if no
    API key is set, the lookup fails, or `requests` isn't installed -
    callers should handle missing posters gracefully.
    Results are cached in-memory per build so the same film (e.g. featured
    on both the homepage and the episodes page) is only fetched once.
    """
    if film_title in cache:
        return cache[film_title]

    result = {"poster": None, "imdb_id": None}
    if requests and api_key and "PASTE_" not in api_key:
        try:
            resp = requests.get(
                "https://www.omdbapi.com/",
                params={"t": film_title, "apikey": api_key},
                timeout=8,
            )
            data = resp.json()
            if data.get("Response") != "True":
                # Exact-title lookup failed - the episode title doesn't always match
                # OMDb's official wording (e.g. "Kill Bill" vs "Kill Bill: Vol. 1", or
                # "&" vs "and" in "Indiana Jones & the Last Crusade"). Fall back to
                # OMDb's fuzzier search endpoint and take its top match instead of
                # giving up and showing no cover art at all.
                search_resp = requests.get(
                    "https://www.omdbapi.com/",
                    params={"s": film_title, "apikey": api_key},
                    timeout=8,
                )
                search_data = search_resp.json()
                if search_data.get("Response") == "True" and search_data.get("Search"):
                    data = search_data["Search"][0]

            poster = data.get("Poster")
            if poster and poster != "N/A":
                result["poster"] = poster
            imdb_id = data.get("imdbID")
            if imdb_id:
                result["imdb_id"] = imdb_id
        except Exception as e:
            print(f"  OMDb lookup failed for '{film_title}': {e}")

    cache[film_title] = result
    return result


def fetch_episodes(rss_url, omdb_api_key, drinks_snacks=None):
    drinks_snacks = drinks_snacks or {}
    if not rss_url or "PASTE_" in rss_url:
        print("No RSS feed URL set in config.json yet - building site with an empty episode list.")
        return []

    feed = feedparser.parse(rss_url)
    if feed.bozo and not feed.entries:
        print(f"Warning: couldn't parse RSS feed ({feed.bozo_exception}). Building with an empty episode list.")
        return []

    cover_cache = {}
    episodes = []
    for entry in feed.entries:
        date_str = ""
        if getattr(entry, "published_parsed", None):
            date_str = datetime(*entry.published_parsed[:6]).strftime("%d %b %Y")

        duration = getattr(entry, "itunes_duration", "")

        link = entry.get("link", "")
        if not link:
            for l in entry.get("links", []):
                if l.get("type", "").startswith("audio"):
                    link = l.get("href", "")
                    break

        summary = strip_html(entry.get("summary", ""))
        if len(summary) > 400:
            summary = summary[:400].rsplit(" ", 1)[0] + "..."

        title = entry.get("title", "Untitled episode")
        season, rank, film_title = parse_episode_title(title)

        # Spotify for Creators lets you set a numeric "Season number" and
        # "Episode number" per episode (these come through the feed as the
        # itunes:season / itunes:episode tags) - these are far more reliable
        # than parsing the season out of freeform title text, so prefer them
        # when present. episode_num also gives a true chronological sort key
        # for episodes that don't have a single ranked film (recap/catch-up
        # episodes), instead of relying on RSS feed order.
        raw_season = entry.get("itunes_season")
        if raw_season not in (None, ""):
            try:
                season = int(raw_season)
            except (TypeError, ValueError):
                pass

        episode_num = None
        raw_episode = entry.get("itunes_episode")
        if raw_episode not in (None, ""):
            try:
                episode_num = int(raw_episode)
            except (TypeError, ValueError):
                episode_num = None

        cover = fetch_cover(film_title, omdb_api_key, cover_cache)
        imdb_link = (
            f"https://www.imdb.com/title/{cover['imdb_id']}/"
            if cover.get("imdb_id")
            else imdb_search_link(film_title)
        )

        drink_snack = drinks_snacks.get(normalize_title(film_title), {})

        episodes.append({
            "title": title,
            "date": date_str,
            "duration": duration,
            "summary": summary,
            "link": link,
            "season": season,
            "rank": rank,
            "episode_num": episode_num,
            "film_title": film_title,
            "poster": cover.get("poster"),
            "imdb_link": imdb_link,
            "kev_drink_name": drink_snack.get("kev_drink_name"),
            "kev_drink_emoji": drink_snack.get("kev_drink_emoji"),
            "kev_glass_name": drink_snack.get("kev_glass_name"),
            "kev_drink_ingredients": drink_snack.get("kev_drink_ingredients"),
            "andy_drink_name": drink_snack.get("andy_drink_name"),
            "andy_drink_emoji": drink_snack.get("andy_drink_emoji"),
            "andy_glass_name": drink_snack.get("andy_glass_name"),
            "andy_drink_ingredients": drink_snack.get("andy_drink_ingredients"),
            "snack_name": drink_snack.get("snack_name"),
            "snack_emoji": drink_snack.get("snack_emoji"),
            "snack_ingredients": drink_snack.get("snack_ingredients"),
        })

    return episodes


def load_toptrumps_cards():
    """
    Every image dropped into static/toptrumps/ automatically shows up on the
    Top Trumps page - no config or code change needed. The card's title is
    guessed from the filename (my-film.jpg -> "My Film"), so name new files
    accordingly, e.g. static/toptrumps/the-thing.jpg -> "The Thing".
    """
    folder = os.path.join(ROOT, "static", "toptrumps")
    if not os.path.isdir(folder):
        return []
    cards = []
    for fname in sorted(os.listdir(folder)):
        if not fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        title = os.path.splitext(fname)[0].replace("-", " ").replace("_", " ").title()
        cards.append({"file": fname, "title": title, "slug": slugify(title)})
    return cards


def attach_toptrumps_links(episodes, toptrumps_cards):
    """
    Matches each episode's film title against the Top Trumps card titles
    (ignoring case/punctuation) and stamps a `toptrumps_slug` onto the
    episode dict when a card exists, so episode cards can link straight to
    that film's card on the Stats page. No config needed - just relies on
    the Top Trumps filename matching the film title closely enough.
    """
    by_title = {normalize_title(c["title"]): c["slug"] for c in toptrumps_cards}
    for ep in episodes:
        ep["toptrumps_slug"] = by_title.get(normalize_title(ep.get("film_title", "")))
    return episodes


def group_by_season(episodes):
    seasons = {}
    for ep in episodes:
        key = ep["season"] or 0
        seasons.setdefault(key, []).append(ep)
    # newest episode first within each season, seasons ascending
    return [{"number": k, "episodes": v} for k, v in sorted(seasons.items())]


def _hosting_order_key(ep):
    """
    Sort key that reflects the order episodes were actually hosted/released
    in, using Spotify's structured episode number (see fetch_episodes) -
    lower episode_num = released earlier. Falls back to rank if
    episode_num is somehow missing, since the show counts down in
    descending rank order (starts around #91-100, ends at the #1-10
    finale) - a higher rank number was released earlier.
    """
    if ep.get("episode_num") is not None:
        return ep["episode_num"]
    return -(ep.get("rank") or 0)


def group_by_decade(episodes):
    """
    Sub-groups a season's episodes into blocks of 10 by rank (#1-10,
    #11-20, ... #91-100) purely for a readable heading. The blocks
    themselves, and the episodes within each, are ordered newest-first by
    actual hosting/release date (same convention as Spotify) rather than
    by the rank value - the show counts down in descending rank order, so
    ordering by rank value alone would put the season opener at the top of
    the page instead of the newest episode. Episodes without a parsed rank
    (catch-up/recap episodes, e.g. "Roll of the Dice") get their own
    "Other Episodes" bucket, but it's slotted into its real chronological
    position among the ranked blocks rather than always being pinned to
    the end.
    """
    buckets = {}
    other = []
    for ep in episodes:
        rank = ep.get("rank")
        if not rank:
            other.append(ep)
            continue
        low = ((rank - 1) // 10) * 10 + 1
        high = low + 9
        buckets.setdefault((low, high), []).append(ep)

    groups = []
    for (low, high), eps in buckets.items():
        eps = sorted(eps, key=_hosting_order_key, reverse=True)
        groups.append({"label": f"Ranks #{low}-{high}", "episodes": eps, "_key": _hosting_order_key(eps[0])})
    if other:
        other = sorted(other, key=_hosting_order_key, reverse=True)
        groups.append({"label": "Other Episodes", "episodes": other, "_key": _hosting_order_key(other[0])})

    groups.sort(key=lambda g: g["_key"], reverse=True)
    for g in groups:
        del g["_key"]
    return groups


SEASON_TARGETS = {1: 99, 2: 101}  # Andy's and Kev's full list lengths


def season_progress(seasons):
    """[{'number':1,'count':N,'target':99,'pct':...}, ...] for the progress meter."""
    progress = []
    for group in seasons:
        num = group["number"]
        if not num:
            continue
        target = SEASON_TARGETS.get(num, 100)
        count = len(group["episodes"])
        pct = min(100, round(100 * count / target)) if target else 0
        progress.append({"number": num, "count": count, "target": target, "pct": pct})
    return progress


def load_titles():
    """Full 100-film ranked lists for the 'Surprise me' picker."""
    path = os.path.join(ROOT, "titles.json")
    if not os.path.exists(path):
        return {"kev": [], "andy": []}
    with open(path) as f:
        return json.load(f)


# Keyword-based (not exact-match) so new/slightly-different category wording
# still picks a sensible icon instead of silently falling back to default.
DRINK_EMOJI_KEYWORDS = [
    ("beer", "\U0001F37A"), ("cigar", "\U0001F6AC"), ("none", "\U0001F6AC"),
    ("wine", "\U0001F377"), ("champagne", "\U0001F942"), ("sparkling", "\U0001F942"),
    ("sake", "\U0001F376"), ("shot", "\U0001F943"), ("rocks", "\U0001F943"),
    ("spirit", "\U0001F943"), ("neat", "\U0001F943"),
    ("hot drink", "☕"), ("coffee", "☕"), ("tea", "☕"),
    ("alcopop", "\U0001F379"), ("rtd", "\U0001F379"),
    ("non-alcoholic", "\U0001F964"), ("non alcoholic", "\U0001F964"),
    ("mocktail", "\U0001F964"), ("soft drink", "\U0001F964"),
    ("highball", "\U0001F964"), ("cocktail", "\U0001F378"),
]
SNACK_EMOJI_KEYWORDS = [
    ("cinema concession", "\U0001F37F"), ("movie easter egg", "\U0001F95A"),
    ("novelty", "\U0001F36C"), ("retro", "\U0001F36C"),
    ("regional", "\U0001F30D"), ("cultural", "\U0001F30D"),
    ("at-home party", "\U0001F389"), ("party snack", "\U0001F389"),
    ("popcorn", "\U0001F37F"), ("nachos", "\U0001F9C0"), ("pizza", "\U0001F355"),
    ("candy", "\U0001F36C"), ("chocolate", "\U0001F36B"), ("ice cream", "\U0001F366"),
    ("hot dog", "\U0001F32D"), ("chips", "\U0001F35F"),
]
DEFAULT_DRINK_EMOJI = "\U0001F37B"
DEFAULT_SNACK_EMOJI = "\U0001F37F"


def _emoji_for(category, keyword_list, default):
    c = (category or "").strip().lower()
    for keyword, emoji in keyword_list:
        if keyword in c:
            return emoji
    return default


# Matches a leading run of emoji characters at the start of a "Glass" cell,
# e.g. "🥃 Rocks glass" -> icon "🥃", name "Rocks glass". Lets Kev/Andy's own
# emoji choice drive the tag icon each week instead of a fixed keyword list -
# nothing to maintain here as new glass types get used.
GLASS_RE = re.compile(r"^([\U0001F000-\U0001FFFF☀-➿️‍]+)\s*(.*)$")


def parse_glass(text):
    """Returns (emoji, name) from a 'Glass' cell, or (None, None) if blank/unparseable."""
    text = (text or "").strip()
    if not text or text == "—":
        return None, None
    m = GLASS_RE.match(text)
    if not m:
        return None, text  # descriptive text with no leading emoji - keep as tooltip only
    icon, name = m.group(1)[0], m.group(2).strip()
    return icon, (name or None)


def load_drinks_snacks(sheet_url):
    """
    Reads a published-to-web Google Sheet CSV with columns: Film,
    Kev Drink Name, Kev Drink Category, Kev Glass, Kev Drink Ingredients,
    Andy Drink Name, Andy Drink Category, Andy Glass, Andy Drink Ingredients,
    Snack Name, Snack Category, Snack Ingredients. Returns a dict keyed by a
    normalized film title (see normalize_title) so small punctuation/casing
    differences between the sheet and the RSS episode title still match.
    Returns {} until a real sheet_url is set in config.json - the site works
    fine either way, episode cards just won't show drink/snack tags until
    this is wired up.

    The drink tag's icon comes from the leading emoji in that host's
    "Glass" cell (e.g. "🥃 Rocks glass") when present, since that's a more
    accurate per-drink icon than a guessed category - the glass name shows
    as a hover tooltip. Falls back to a keyword match on the Category
    column when the Glass cell is blank or has no emoji.

    The three "Ingredients" columns are entirely optional - leave them out
    of the sheet (or leave cells blank) and the site works exactly as
    before, just without the hover/tap ingredients popover on that tag.
    """
    if not sheet_url or "PASTE_" in sheet_url or not requests:
        return {}
    try:
        resp = requests.get(sheet_url, timeout=8)
        resp.raise_for_status()
        import csv
        import io
        reader = csv.DictReader(io.StringIO(resp.text))
        out = {}
        for row in reader:
            film = (row.get("Film") or "").strip()
            if not film:
                continue
            kev_drink = (row.get("Kev Drink Name") or "").strip()
            andy_drink = (row.get("Andy Drink Name") or "").strip()
            snack = (row.get("Snack Name") or "").strip()

            kev_icon, kev_glass_name = parse_glass(row.get("Kev Glass"))
            andy_icon, andy_glass_name = parse_glass(row.get("Andy Glass"))

            kev_ingredients = (row.get("Kev Drink Ingredients") or "").strip()
            andy_ingredients = (row.get("Andy Drink Ingredients") or "").strip()
            snack_ingredients = (row.get("Snack Ingredients") or "").strip()

            out[normalize_title(film)] = {
                "kev_drink_name": kev_drink,
                "kev_drink_emoji": (kev_icon or _emoji_for(row.get("Kev Drink Category"), DRINK_EMOJI_KEYWORDS, DEFAULT_DRINK_EMOJI)) if kev_drink else None,
                "kev_glass_name": kev_glass_name if kev_drink else None,
                "kev_drink_ingredients": (kev_ingredients or None) if kev_drink else None,
                "andy_drink_name": andy_drink,
                "andy_drink_emoji": (andy_icon or _emoji_for(row.get("Andy Drink Category"), DRINK_EMOJI_KEYWORDS, DEFAULT_DRINK_EMOJI)) if andy_drink else None,
                "andy_glass_name": andy_glass_name if andy_drink else None,
                "andy_drink_ingredients": (andy_ingredients or None) if andy_drink else None,
                "snack_name": snack,
                "snack_emoji": _emoji_for(row.get("Snack Category"), SNACK_EMOJI_KEYWORDS, DEFAULT_SNACK_EMOJI) if snack else None,
                "snack_ingredients": (snack_ingredients or None) if snack else None,
            }
        return out
    except Exception as e:
        print(f"  Drinks/snacks sheet fetch failed: {e}")
        return {}


# Brand names that .title() would otherwise mangle (e.g. "outout" -> "Outout").
TITLE_FIXUPS = {"Outout": "OutOut"}


# Shop items are grouped into a category guessed from the filename ending -
# "-tee"/"-hoodie" -> Clothing, "-cap" -> Headwear, everything else falls
# into the catch-all last category. Rename a file to end in one of these
# suffixes to move it between categories - no other config needed. Order
# here also controls display order on the page.
SHOP_CATEGORIES = [
    ("Clothing", ("-tee", "-hoodie")),
    ("Headwear", ("-cap",)),
]
DEFAULT_SHOP_CATEGORY = "Accessories & Extras"


def categorize_shop_item(fname):
    stem = os.path.splitext(fname)[0].lower()
    for label, suffixes in SHOP_CATEGORIES:
        if stem.endswith(suffixes):
            return label
    return DEFAULT_SHOP_CATEGORY


def load_shop_items():
    """Same automated pattern as Top Trumps - drop an image into
    static/shop/ and it shows up on the Shop page automatically, grouped
    into a category guessed from the filename (see categorize_shop_item)."""
    folder = os.path.join(ROOT, "static", "shop")
    if not os.path.isdir(folder):
        return []
    grouped = {}
    for fname in sorted(os.listdir(folder)):
        if not fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        title = os.path.splitext(fname)[0].replace("-", " ").replace("_", " ").title()
        for wrong, right in TITLE_FIXUPS.items():
            title = title.replace(wrong, right)
        category = categorize_shop_item(fname)
        grouped.setdefault(category, []).append({"file": fname, "title": title})

    # Note: deliberately "products", not "items" - Jinja2's dot-notation
    # attribute lookup on a dict tries real dict methods first, so a key
    # literally named "items" would silently resolve to dict.items()
    # instead of the list, and break the template's {% for %} loop.
    category_order = [label for label, _ in SHOP_CATEGORIES] + [DEFAULT_SHOP_CATEGORY]
    return [
        {"label": label, "products": grouped[label]}
        for label in category_order
        if label in grouped
    ]


# The curated "new here? start with these" picks - matched against the real
# episode list at build time (by film title) so each pick gets the same
# cover art, listen icons, IMDb link, Top Trumps link and drink/snack tags
# as everywhere else, rather than being a hand-typed dead end. Add/replace a
# title here any time; if it isn't found (not yet published, or the title
# doesn't match closely enough) it's just skipped rather than breaking the
# page - check the build log for a note when that happens.
START_HERE_PICKS = [
    (1, "The Video Shop Era", ["Chef", "Point Break"]),
    (2, "Cocktails and Shattered Dreams", ["Roadhouse", "Piece by Piece"]),
]


def load_start_here_picks(episodes):
    by_title = {normalize_title(ep["film_title"]): ep for ep in episodes}
    groups = []
    for season_number, label, titles in START_HERE_PICKS:
        picks = []
        for title in titles:
            ep = by_title.get(normalize_title(title))
            if ep:
                picks.append(ep)
            else:
                print(f"  Start Here: couldn't find an episode matching '{title}', skipping it.")
        if picks:
            groups.append({"season": season_number, "label": label, "episodes": picks})
    return groups


def build():
    config = load_config()
    drinks_snacks = load_drinks_snacks(config.get("drinks_sheet_url", ""))
    episodes = fetch_episodes(config.get("rss_url", ""), config.get("omdb_api_key", ""), drinks_snacks)
    toptrumps_cards = load_toptrumps_cards()
    episodes = attach_toptrumps_links(episodes, toptrumps_cards)
    seasons = group_by_season(episodes)
    for group in seasons:
        group["decades"] = group_by_decade(group["episodes"])
    shop_items = load_shop_items()
    titles = load_titles()
    progress = season_progress(seasons)
    episodes_with_drinks = [ep for ep in episodes if ep.get("kev_drink_name") or ep.get("andy_drink_name") or ep.get("snack_name")]
    episodes_with_snacks = [ep for ep in episodes if ep.get("snack_name")]
    start_here_picks = load_start_here_picks(episodes)

    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    shutil.copytree(os.path.join(ROOT, "static"), os.path.join(OUT, "static"))

    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")))
    common = {
        "config": config,
        "episodes": episodes,
        "seasons": seasons,
        "toptrumps_cards": toptrumps_cards,
        "shop_items": shop_items,
        "kev_list": titles.get("kev", []),
        "andy_list": titles.get("andy", []),
        "season_progress": progress,
        "episodes_with_drinks": episodes_with_drinks,
        "episodes_with_snacks": episodes_with_snacks,
        "start_here_picks": start_here_picks,
        "year": datetime.now().year,
        "root": "",
    }

    pages = [
        "index.html", "episodes.html", "about.html", "stats.html",
        "shop.html", "watchfollow.html", "starthere.html",
    ]
    for page in pages:
        template = env.get_template(page)
        with open(os.path.join(OUT, page), "w") as f:
            f.write(template.render(**common))

    print(f"Built {len(pages)} pages with {len(episodes)} episodes into {OUT}/")


if __name__ == "__main__":
    build()
