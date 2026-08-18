#!/usr/bin/env python3
"""
Builds the Long Play Video static site into _site/ from config.json + the
show's Spotify RSS feed. Run manually with `python generate_site.py`, or let
the GitHub Actions workflow (.github/workflows/deploy.yml) run it on a
schedule so the site keeps itself up to date with no manual work.
"""
import calendar
import difflib
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


def fetch_episodes(rss_url, omdb_api_key, drinks_snacks=None, film_stats=None):
    drinks_snacks = drinks_snacks or {}
    film_stats = film_stats or {}
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
        published_ts = None
        if getattr(entry, "published_parsed", None):
            date_str = datetime(*entry.published_parsed[:6]).strftime("%d %b %Y")
            # Real publish-date timestamp, used to order episodes by actual
            # hosting/release date (see _hosting_order_key) - more reliable
            # than the itunes:episode number, since episodes aren't always
            # released in the exact order they were numbered/recorded in
            # (schedules shift, episodes get swapped, etc).
            published_ts = calendar.timegm(entry.published_parsed)

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
        stats = film_stats.get(normalize_title(film_title), {})

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
            "slug": slugify(film_title),
            "poster": cover.get("poster"),
            "imdb_link": imdb_link,
            "published_ts": published_ts,
            "genre": stats.get("genre"),
            "rt_audience": stats.get("rt_audience"),
            "rt_critics": stats.get("rt_critics"),
            "imdb_rating": stats.get("imdb_rating"),
            "guest": stats.get("guest"),
            "year": stats.get("year"),
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


def infer_missing_seasons(episodes):
    """
    Bonus episodes (e.g. "Bonus Episode - Kev's bottom 25") sometimes have
    no itunes:season tag AND no "S01"/"S02"/"Season N Finale" text in the
    title for parse_episode_title() to pick up - there's nothing anywhere
    in the feed saying which season they belong to. Left as season=None,
    an episode like that gets shunted into its own unlabeled "season 0"
    bucket at the very bottom of the Episodes page, instead of sitting in
    its actual chronological spot alongside the season it was really
    released during - which is exactly the "bonus episodes in the wrong
    place" problem.

    Since every bonus episode observed so far was published squarely in
    the middle of one season's run (surrounded on both sides by dated
    episodes from that same season), the nearest neighbour by publish date
    is a reliable stand-in for "which season was airing at the time" - so
    a missing season is filled in from whichever dated episode is closest
    in time. Episodes that already have a season (from the tag or the
    title) are left untouched.
    """
    dated = sorted(
        (ep for ep in episodes if ep.get("published_ts") is not None and ep.get("season") is not None),
        key=lambda ep: ep["published_ts"],
    )
    if not dated:
        return episodes

    for ep in episodes:
        if ep.get("season") is not None or ep.get("published_ts") is None:
            continue
        closest = min(dated, key=lambda d: abs(d["published_ts"] - ep["published_ts"]))
        ep["season"] = closest["season"]
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
    in. The real RSS publish timestamp (see fetch_episodes) is the ground
    truth here - episodes aren't always released in the exact order they
    were numbered/recorded in (recording schedules shift, episodes get
    swapped around), so relying on Spotify's itunes:episode number alone
    can put episodes in the wrong order even though each one individually
    has a "correct" number. Falls back to episode_num, then rank, only for
    the rare case a feed entry is missing a publish date entirely.
    """
    if ep.get("published_ts") is not None:
        return ep["published_ts"]
    if ep.get("episode_num") is not None:
        return ep["episode_num"]
    return -(ep.get("rank") or 0)


def group_by_decade(episodes):
    """
    Sub-groups a season's episodes into blocks of 10 by rank (#1-10,
    #11-20, ... #91-100) purely for a readable heading, in true newest-
    first hosting/release order (same convention as Spotify) - the show
    counts down in descending rank order, so ordering by rank value alone
    would put the season opener at the top of the page instead of the
    newest episode.

    Walks the whole season in real chronological order and only starts a
    new visual block when the decade label actually changes, rather than
    bucketing every same-decade episode together first and placing each
    bucket as one atomic unit. That distinction matters for episodes
    without a parsed rank (catch-up/recaps, and bonus episodes with no
    season/rank info at all) - with the old bucket-first approach, one of
    these could land in the same "Other Episodes" bucket as an unrelated
    episode from weeks apart and get pinned to the wrong end of it, even
    though its actual release date sat right between two ranked episodes.
    Walking date-order first means it always slots into its true position,
    even if that means the same "Ranks #71-80" heading appears twice with
    something else between them - which is a more honest reflection of
    what actually aired than silently misplacing an episode.
    """
    ordered = sorted(episodes, key=_hosting_order_key, reverse=True)

    groups = []
    for ep in ordered:
        rank = ep.get("rank")
        if rank:
            low = ((rank - 1) // 10) * 10 + 1
            label = f"Ranks #{low}-{low + 9}"
        else:
            label = "Other Episodes"

        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "episodes": []})
        groups[-1]["episodes"].append(ep)

    return groups


SEASON_TARGETS = {1: 100, 2: 100}  # displayed as a flat "of 100" per season,
# matching the "100 Movies" tagline - the real planned-list lengths (Andy's
# 99, Kev's 101) are correct but confusing to show to visitors, so the
# gauge always reads against 100 even though the true count can occasionally
# poke just over or under that round number.


def season_progress(seasons):
    """
    [{'number':1,'count':N,'target':100,'pct':...}, ...] for the progress meter.
    'count' is every published episode in the season, including recap/
    catch-up episodes like "Roll of the Dice" that don't map to a single
    ranked film. It's labelled "episodes released" (not "films watched") on
    the page specifically so it's fine for count to occasionally read
    higher than the target - that's honestly true (more episodes than films
    exist because of the recaps), rather than a films-vs-films mismatch that
    would look like a bug.
    """
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


def resolve_surprise_titles(titles_list, episodes):
    """
    Matches the freeform titles in titles.json against the real episode
    list so the "Surprise me" buttons can link straight to the matching
    episode card. The two lists don't always agree exactly - titles.json
    is a hand-typed want-to-watch list, so it sometimes drops "The",
    shortens a title, or has a small typo (a missing/doubled letter or
    word) - so this falls through three passes before giving up:
      1. exact match on the normalized title
      2. containment (handles "Matrix" vs "The Matrix", "Spring" vs the
         full "Spring, Summer, Autumn..." title, etc.)
      3. closest fuzzy match, accepted only above a safety threshold so it
         won't confidently link to the wrong film
    Returns {normalized titles.json title: episode slug}, used as a
    lookup table baked into the page so the client-side JS just does an
    exact key lookup - no fuzzy matching needed in the browser.
    """
    episode_norms = [(normalize_title(ep["film_title"]), ep["slug"]) for ep in episodes]
    lookup = {}
    for title in titles_list:
        key = normalize_title(title)
        if not key or key in lookup:
            continue

        exact = next((slug for norm, slug in episode_norms if norm == key), None)
        if exact:
            lookup[key] = exact
            continue

        contains = [(norm, slug) for norm, slug in episode_norms if key in norm or norm in key]
        if contains:
            contains.sort(key=lambda pair: abs(len(pair[0]) - len(key)))
            lookup[key] = contains[0][1]
            continue

        best_slug, best_ratio = None, 0.0
        for norm, slug in episode_norms:
            ratio = difflib.SequenceMatcher(None, key, norm).ratio()
            if ratio > best_ratio:
                best_slug, best_ratio = slug, ratio
        if best_ratio >= 0.82:
            lookup[key] = best_slug
        # else: genuinely not released yet (or too different a title to
        # safely guess) - left out of the lookup, JS shows "not released
        # yet" for it.
    return lookup


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


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val):
    f = _to_float(val)
    return int(f) if f is not None else None


def load_film_stats(sheet_url):
    """
    Reads a published-to-web Google Sheet CSV with per-film data (genre,
    Rotten Tomatoes audience/critic scores, IMDb rating, guest, release
    year) used to power the Stats page's interactive charts, the Top Trumps
    genre filter, and the Episodes page's genre/guest/year filters.
    Expected columns: Film, Genre, RT Audience Score, RT Critics Score,
    IMDb Rating, Guest, Release Year. RT scores can be entered either as a
    0-1 fraction (0.94) or a 0-100 percentage (94) - both are normalised to
    a 0-100 number here. Guest is optional - leave it blank for solo
    episodes, it just won't get a guest filter option.
    Returns {} until a real film_stats_sheet_url is set in config.json - the
    site works fine either way, it just falls back to the old static chart
    images and unfiltered grids until this is wired up.
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
            genre = (row.get("Genre") or "").strip() or None
            rt_audience = _to_float(row.get("RT Audience Score"))
            rt_critics = _to_float(row.get("RT Critics Score"))
            imdb_rating = _to_float(row.get("IMDb Rating"))
            # Tolerate scores entered either as 0-1 (0.94) or 0-100 (94).
            if rt_audience is not None and rt_audience <= 1:
                rt_audience *= 100
            if rt_critics is not None and rt_critics <= 1:
                rt_critics *= 100
            guest = (row.get("Guest") or "").strip() or None
            year = _to_int(row.get("Release Year"))
            out[normalize_title(film)] = {
                "genre": genre,
                "rt_audience": rt_audience,
                "rt_critics": rt_critics,
                "imdb_rating": imdb_rating,
                "guest": guest,
                "year": year,
            }
        return out
    except Exception as e:
        print(f"  Film stats sheet fetch failed: {e}")
        return {}


def attach_film_stats_to_toptrumps(toptrumps_cards, film_stats):
    """Stamps genre/rating data onto each Top Trumps card (matched by title,
    same loose normalize_title match used everywhere else) so the grid can
    be filtered/sorted client-side without any extra config."""
    for card in toptrumps_cards:
        stats = film_stats.get(normalize_title(card["title"]), {})
        card["genre"] = stats.get("genre")
        card["rt_audience"] = stats.get("rt_audience")
        card["imdb_rating"] = stats.get("imdb_rating")
    return toptrumps_cards


def compute_genre_breakdown(episodes):
    """Counts how many published episodes fall into each genre, for the
    Stats page's genre chart. Only counts episodes that matched a genre via
    the film stats sheet - returns [] (chart hidden) until that's wired up."""
    counts = {}
    for ep in episodes:
        genre = ep.get("genre")
        if genre:
            counts[genre] = counts.get(genre, 0) + 1
    return [{"genre": g, "count": c} for g, c in sorted(counts.items(), key=lambda x: -x[1])]


def compute_critics_vs_audience(episodes):
    """[{film, critics, audience}, ...] for the critics-vs-audience scatter
    chart - only episodes with both scores available."""
    points = []
    for ep in episodes:
        if ep.get("rt_critics") is not None and ep.get("rt_audience") is not None:
            points.append({
                "film": ep["film_title"],
                "critics": ep["rt_critics"],
                "audience": ep["rt_audience"],
            })
    return points


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
    film_stats = load_film_stats(config.get("film_stats_sheet_url", ""))
    episodes = fetch_episodes(config.get("rss_url", ""), config.get("omdb_api_key", ""), drinks_snacks, film_stats)
    episodes = infer_missing_seasons(episodes)
    toptrumps_cards = load_toptrumps_cards()
    toptrumps_cards = attach_film_stats_to_toptrumps(toptrumps_cards, film_stats)
    toptrumps_genres = sorted({c["genre"] for c in toptrumps_cards if c.get("genre")})
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
    genre_breakdown = compute_genre_breakdown(episodes)
    critics_vs_audience = compute_critics_vs_audience(episodes)
    episode_genres = sorted({ep["genre"] for ep in episodes if ep.get("genre")})
    episode_guests = sorted({ep["guest"] for ep in episodes if ep.get("guest")})
    episode_years = sorted({ep["year"] for ep in episodes if ep.get("year")}, reverse=True)
    # Lets the "Surprise me" buttons (which pick a title from the original
    # planned lists in titles.json) link straight to the matching episode
    # card if it's been released already, rather than just naming it.
    episode_slug_lookup = resolve_surprise_titles(titles.get("kev", []) + titles.get("andy", []), episodes)

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
        "genre_breakdown": genre_breakdown,
        "critics_vs_audience": critics_vs_audience,
        "toptrumps_genres": toptrumps_genres,
        "episode_genres": episode_genres,
        "episode_guests": episode_guests,
        "episode_years": episode_years,
        "episode_slug_lookup": episode_slug_lookup,
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
