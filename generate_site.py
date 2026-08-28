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
import time
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
# A trailing "(2025)"-style bare year is a date annotation, not a film
# title override (e.g. "Bonus Episode - Christmas Movies (2025)") - only
# treat the parenthetical as a film override when it isn't just a year.
YEAR_ONLY_RE = re.compile(r"^(19|20)\d{2}$")


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
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
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
    if paren_film and YEAR_ONLY_RE.match(paren_film):
        paren_film = None

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


FILM_YEARS_PATH = os.path.join(ROOT, "film_years.json")


def load_film_years():
    """
    {normalized title: release year}, sourced from the hosts' own ranked
    lists (titles.json's originals, via the Top 100 spreadsheet) - used to
    tell OMDb which release year to match when a title has been remade
    (e.g. "Road House" 1989 vs 2024, "The Running Man" 1987 vs 2025).
    Without a year hint, OMDb's exact-title lookup tends to return whichever
    version is most recent/popular, which silently shows the wrong film's
    poster and IMDb link for anything with a same-named remake.
    """
    if not os.path.exists(FILM_YEARS_PATH):
        return {}
    try:
        with open(FILM_YEARS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def lookup_film_year(film_title, film_years):
    """
    Exact -> fuzzy fallback, since the hosts' spreadsheet title and the
    Spotify episode's parsed film_title don't always match exactly (e.g.
    "Running Man" in the spreadsheet vs the episode's actual "The Running
    Man" - the fuzzy pass's length-aware ratio handles that case fine on
    its own, no separate containment pass needed).

    This used to have a middle "containment" pass (does one normalized
    title contain the other as a raw substring), matching
    resolve_surprise_titles' three-pass structure - but film_years.json's
    keys are pre-normalized to one alphanumeric blob with no word
    boundaries left to check, so unlike resolve_surprise_titles this
    couldn't be fixed to require whole-word containment, only removed.
    It's what let the short, real, unrelated title "Once" collide with
    "Everything, Everywhere, All At Once" over in resolve_surprise_titles;
    the equivalent risk here is a short film title's normalized text
    coincidentally appearing inside a longer, different film's, silently
    handing back the wrong year and - via fetch_cover's year filter - the
    wrong poster/IMDb link. Dropping it in favour of the ratio-based fuzzy
    pass (which naturally penalises exactly this kind of length mismatch,
    unlike a raw substring check) removes that risk.
    """
    key = normalize_title(film_title)
    if not key or not film_years:
        return None
    if key in film_years:
        return film_years[key]
    best_year, best_ratio = None, 0.0
    for k, v in film_years.items():
        ratio = difflib.SequenceMatcher(None, key, k).ratio()
        if ratio > best_ratio:
            best_year, best_ratio = v, ratio
    return best_year if best_ratio >= 0.82 else None


COVER_CACHE_PATH = os.path.join(ROOT, "cover_cache.json")


def load_cover_cache():
    """
    Persistent disk cache of {film title: {"poster":..., "imdb_id":...}},
    committed to the repo so a poster - once found - doesn't need to be
    re-fetched from OMDb on every single rebuild. Without this, the build
    was re-querying all ~145 films from scratch every time it ran; the free
    OMDb tier caps out at 1,000 lookups/day, and with this site rebuilding
    repeatedly that quota got exhausted, which is exactly what made every
    cover on the site disappear at once. With the cache in place, daily
    OMDb usage drops to roughly "however many new episodes came out since
    the last build" instead of the full list every time.
    """
    if not os.path.exists(COVER_CACHE_PATH):
        return {}
    try:
        with open(COVER_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cover_cache(cache):
    # Only persist genuine successes. A "not found" or rate-limited result
    # stays out of the saved file so it's naturally retried on the next
    # build (cheap, since almost everything else is already cached) rather
    # than being stuck with no cover forever because of a transient failure.
    persistable = {title: result for title, result in cache.items() if result.get("poster")}
    with open(COVER_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(persistable, f, indent=2, sort_keys=True)


_omdb_quota_exceeded = False  # set for the rest of this build once OMDb reports its daily cap is hit

# A handful of films where the episode's actual title (from Spotify) just
# doesn't lead OMDb to the right entry, no matter how many fallbacks
# fetch_cover() tries - OMDb's own canonical title differs enough that its
# search endpoint doesn't even surface the real film as a candidate, not
# just differently-punctuated/worded enough for it to still turn up (which
# the normal fallback chain already handles fine on its own). Keyed by
# normalize_title() of the episode's parsed title, same as everywhere else
# in this file. Add an entry here if a future film's cover/IMDb link won't
# resolve despite everything fetch_cover() already tries.
OMDB_TITLE_OVERRIDES = {
    normalize_title("A Cock and Bull Story"): "Tristram Shandy",
    normalize_title("Spring, Summer, Autumn, Winter...and Spring"): "Spring, Summer, Fall, Winter... and Spring",
}


def _omdb_request(params, api_key):
    """One OMDb call. Returns the parsed JSON dict, or None on a network
    error or once the daily quota's been hit (sets _omdb_quota_exceeded so
    the rest of this build stops trying)."""
    global _omdb_quota_exceeded
    if _omdb_quota_exceeded:
        return None
    try:
        resp = requests.get("https://www.omdbapi.com/", params=dict(params, apikey=api_key), timeout=8)
        data = resp.json()
        if data.get("Error") == "Request limit reached!":
            _omdb_quota_exceeded = True
            print("  OMDb daily request limit reached - skipping remaining cover lookups this run.")
            return None
        return data
    except Exception as e:
        print(f"  OMDb lookup failed: {e}")
        return None


def fetch_cover(film_title, api_key, cache, year=None):
    """
    Look up a film's poster + IMDb ID via the free OMDb API
    (https://www.omdbapi.com/apikey.aspx). Falls back to None/None if no
    API key is set, the lookup fails, or `requests` isn't installed -
    callers should handle missing posters gracefully.
    `cache` is pre-loaded from cover_cache.json (see load_cover_cache), so
    a film already found on a previous build is never re-fetched.
    `year`, when known (see load_film_years), is passed to OMDb to pick the
    right film when the title has been remade - e.g. "Road House" 1989 vs
    2024 - since an unqualified title lookup otherwise tends to return
    whichever version is newest/most popular.

    Tries up to four ways of asking OMDb, stopping at the first one that
    actually returns a poster (not just stopping at the first one that
    returns *a match*):
      1. exact title + year
      2. exact title, no year
      3. fuzzy search + year, best-scoring result
      4. fuzzy search, no year, best-scoring result
    A plain "Response: True" isn't enough signal on its own to stop at -
    OMDb's database includes a lot of low-quality/fan-content entries that
    coincidentally match a real film's title (sometimes even its year):
    e.g. "The Royal Tenenbaums" + year 2001 exact-matches a posterless fan
    edit before it'd ever reach the real film (which OMDb itself files
    under 2002), and "Ant Man and The Wasp" (no hyphen, matching how
    Spotify's episode title reads) exact-matches a posterless YouTube
    video of the same name rather than the real "Ant-Man and the Wasp".
    Both are only found by working through the fallbacks to the search
    endpoint. `year`, similarly, can disagree with OMDb's own year for a
    title (overseas release date vs. the US date OMDb tracks) strictly
    enough that the year-qualified attempts find nothing at all - hence
    also trying unqualified.
    """
    cache_key = f"{film_title}|{year}" if year else film_title
    if cache_key in cache:
        return cache[cache_key]

    omdb_title = OMDB_TITLE_OVERRIDES.get(normalize_title(film_title), film_title)

    result = {"poster": None, "imdb_id": None}
    if requests and api_key and "PASTE_" not in api_key:
        candidates = []
        film_title = omdb_title

        if year:
            data = _omdb_request({"t": film_title, "y": year}, api_key)
            if data and data.get("Response") == "True":
                candidates.append(data)

        if not any(c.get("Poster") not in (None, "N/A") for c in candidates):
            data = _omdb_request({"t": film_title}, api_key)
            if data and data.get("Response") == "True":
                candidates.append(data)

        if not any(c.get("Poster") not in (None, "N/A") for c in candidates):
            if year:
                data = _omdb_request({"s": film_title, "y": year}, api_key)
                if data and data.get("Response") == "True":
                    candidates.extend(data.get("Search", []))

        if not any(c.get("Poster") not in (None, "N/A") for c in candidates):
            data = _omdb_request({"s": film_title}, api_key)
            if data and data.get("Response") == "True":
                candidates.extend(data.get("Search", []))

        # First candidate with a real poster wins; if none of them have
        # one, still keep an IMDb link from whatever we did find (a link
        # with no cover beats no link at all).
        with_poster = next((c for c in candidates if c.get("Poster") not in (None, "N/A")), None)
        chosen = with_poster or (candidates[0] if candidates else None)
        if chosen:
            if with_poster:
                result["poster"] = chosen["Poster"]
            if chosen.get("imdbID"):
                result["imdb_id"] = chosen["imdbID"]

    cache[cache_key] = result
    return result


def fetch_episodes(rss_url, omdb_api_key, drinks_snacks=None, film_stats=None, film_years=None):
    drinks_snacks = drinks_snacks or {}
    film_stats = film_stats or {}
    film_years = film_years or {}
    if not rss_url or "PASTE_" in rss_url:
        print("No RSS feed URL set in config.json yet - building site with an empty episode list.")
        return []

    feed = feedparser.parse(rss_url)
    if feed.bozo and not feed.entries:
        print(f"Warning: couldn't parse RSS feed ({feed.bozo_exception}). Building with an empty episode list.")
        return []

    cover_cache = load_cover_cache()
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

        year_hint = lookup_film_year(film_title, film_years)
        cover = fetch_cover(film_title, omdb_api_key, cover_cache, year=year_hint)
        imdb_link = (
            f"https://www.imdb.com/title/{cover['imdb_id']}/"
            if cover.get("imdb_id")
            else imdb_search_link(film_title)
        )

        drink_snack = _fuzzy_title_lookup(film_title, drinks_snacks)
        stats = _fuzzy_title_lookup(film_title, film_stats)

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
            "sub_genre": stats.get("sub_genre"),
            "rt_audience": stats.get("rt_audience"),
            "rt_critics": stats.get("rt_critics"),
            "imdb_rating": stats.get("imdb_rating"),
            "guest": stats.get("guest"),
            "year": stats.get("year"),
            "runtime": stats.get("runtime"),
            "explosions": stats.get("explosions"),
            "deaths": stats.get("deaths"),
            "animated_chars": stats.get("animated_chars"),
            "budget_adj": stats.get("budget_adj"),
            "box_office_adj": stats.get("box_office_adj"),
            "dom_rev_adj": stats.get("dom_rev_adj"),
            "intl_rev_adj": stats.get("intl_rev_adj"),
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

    save_cover_cache(cover_cache)
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
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _title_word_list(text):
    """Word-tokenized title, leading article stripped ("the"/"a"/"an") -
    used only by resolve_surprise_titles' containment pass below. Unlike
    normalize_title() (which collapses a title to one alphanumeric blob
    and is relied on everywhere else in this file), this keeps word
    boundaries, because "does one title contain the other" needs to mean
    "do their words line up", not "is one a raw substring of the other" -
    see resolve_surprise_titles for why that distinction actually matters
    here, not just in theory."""
    words = re.sub(r"[^a-z0-9\s]", " ", text.lower()).split()
    while words and words[0] in ("the", "a", "an"):
        words = words[1:]
    return words


def _is_word_prefix(shorter, longer):
    return bool(shorter) and longer[:len(shorter)] == shorter


def _fuzzy_title_lookup(film_title, data_by_norm_title):
    """
    Looks up film_title in a {normalize_title(key): value} dict - used for
    the drinks/snacks and film-stats sheets - falling back past a failed
    exact match the same way resolve_surprise_titles does: word-boundary
    containment, then a fuzzy ratio above a safety threshold. Without this,
    a small wording difference between the episode's parsed title and the
    sheet's own spelling silently drops real data that's actually there
    under a slightly different key - e.g. "Indiana Jones & the Last
    Crusade" (episode title) vs "...and the Last Crusade" (sheet), "Kill
    Bill" vs the sheet's "Kill Bill: Volume 1", or "Se7en" vs "Seven".
    Requires each dict value to carry its own original, non-normalized
    title in a "_source_title" key (both load_drinks_snacks and
    _film_stats_entry stamp this on) - the normalized dict keys themselves
    have no word boundaries left to check, same limitation lookup_film_year
    ran into with film_years.json. Returns {} if nothing clears the bar,
    same as a plain dict.get() would.
    """
    if not data_by_norm_title:
        return {}
    key = normalize_title(film_title)
    if key in data_by_norm_title:
        return data_by_norm_title[key]

    key_words = _title_word_list(film_title)
    candidates = []
    for norm_key, value in data_by_norm_title.items():
        source = value.get("_source_title") or ""
        source_words = _title_word_list(source)
        if _is_word_prefix(key_words, source_words) or _is_word_prefix(source_words, key_words):
            candidates.append((norm_key, value))
    if candidates:
        candidates.sort(key=lambda pair: abs(len(pair[0]) - len(key)))
        return candidates[0][1]

    best_value, best_ratio = None, 0.0
    for norm_key, value in data_by_norm_title.items():
        ratio = difflib.SequenceMatcher(None, key, norm_key).ratio()
        if ratio > best_ratio:
            best_value, best_ratio = value, ratio
    # 0.78, not the usual 0.82 used elsewhere (lookup_film_year,
    # resolve_surprise_titles): "Se7en" vs the sheet's "Seven" only
    # scores 0.80, and "Ferris Bueller's Day Off" vs the sheet's
    # shortened "Ferris Bueller" only scores 0.788, so 0.82 was
    # silently dropping real data for those two. Checked against every
    # title in both sheets first (see conversation) - the next-highest
    # ratio for a genuinely different film is 0.70 ("The Visitor" vs
    # "The Insider"), so 0.78 leaves a clear safety margin.
    return best_value if best_ratio >= 0.78 else {}


def resolve_surprise_titles(titles_list, episodes):
    """
    Matches the freeform titles in titles.json against the real episode
    list so the "Surprise me" buttons can link straight to the matching
    episode card. The two lists don't always agree exactly - titles.json
    is a hand-typed want-to-watch list, so it sometimes drops "The",
    shortens a title, or has a small typo (a missing/doubled letter or
    word) - so this falls through three passes before giving up:
      1. exact match on the normalized title
      2. containment - one title's words are a leading match for the
         other's, once a leading "The"/"A"/"An" is stripped from both.
         Handles "Matrix" vs "The Matrix", "Spring" vs the full "Spring,
         Summer, Autumn..." title, etc. This has to be a *word*-boundary
         check, not a raw substring check - a raw substring check let the
         real, already-published film "Once" wrongly match inside
         "Everything, Everywhere, All At Once" (a different, not-yet-aired
         film) purely because the normalized text of the first happens to
         appear at the tail end of the second's. Requiring whole leading
         words to line up rules that out while still matching "Matrix"
         inside "The Matrix" (same film, an omitted article) correctly.
      3. closest fuzzy match, accepted only above a safety threshold so it
         won't confidently link to the wrong film
    Returns {normalized titles.json title: {"slug":..., "season":...}},
    used as a lookup table baked into the page so the client-side JS just
    does an exact key lookup - no fuzzy matching needed in the browser.
    "season" is the *real* season the matched episode actually aired in -
    used so the result always says e.g. "Season 1 pick" for a film that
    aired in Season 1, even if it was picked from the Season 2 list (which
    happens when the same film appears on both hosts' want-to-watch lists -
    see the "surprise-me duplicate titles" build warning for the full list).
    """
    episode_norms = [
        (normalize_title(ep["film_title"]), _title_word_list(ep["film_title"]), ep["slug"], ep.get("season"))
        for ep in episodes
    ]
    lookup = {}
    for title in titles_list:
        key = normalize_title(title)
        if not key or key in lookup:
            continue
        key_words = _title_word_list(title)

        exact = next(((slug, season) for norm, words, slug, season in episode_norms if norm == key), None)
        if exact:
            lookup[key] = {"slug": exact[0], "season": exact[1]}
            continue

        contains = [
            (norm, slug, season) for norm, words, slug, season in episode_norms
            if _is_word_prefix(key_words, words) or _is_word_prefix(words, key_words)
        ]
        if contains:
            contains.sort(key=lambda tup: abs(len(tup[0]) - len(key)))
            lookup[key] = {"slug": contains[0][1], "season": contains[0][2]}
            continue

        best_slug, best_season, best_ratio = None, None, 0.0
        for norm, words, slug, season in episode_norms:
            ratio = difflib.SequenceMatcher(None, key, norm).ratio()
            if ratio > best_ratio:
                best_slug, best_season, best_ratio = slug, season, ratio
        if best_ratio >= 0.82:
            lookup[key] = {"slug": best_slug, "season": best_season}
        # else: genuinely not released yet (or too different a title to
        # safely guess) - left out of the lookup, JS shows "not released
        # yet" for it.
    return lookup


def warn_about_surprise_title_duplicates(titles):
    """
    Prints a build-log warning (doesn't fail the build) when the same film
    appears on both Kev's and Andy's want-to-watch lists. Not necessarily a
    mistake - either host might genuinely want it on their own list too -
    but worth a human glance, since it means clicking "Surprise me" for one
    season can land on a film that actually aired under the other season
    (the on-page label now shows the real season either way, but the
    underlying duplicate is still worth a look).
    """
    andy_norms = {normalize_title(t): t for t in titles.get("andy", [])}
    dupes = [(k, andy_norms[normalize_title(k)]) for k in titles.get("kev", []) if normalize_title(k) in andy_norms]
    if dupes:
        print(f"  Note: {len(dupes)} title(s) appear on both the Kev and Andy want-to-watch lists:")
        for kev_title, andy_title in dupes:
            print(f"    - {kev_title!r} (Kev's list) / {andy_title!r} (Andy's list)")


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


def _fetch_sheet_text(sheet_url, timeout=8, retries=3, backoff=1.5):
    """GETs a published-to-web Google Sheets CSV, retrying a couple of times
    with a short backoff before giving up. These fetches see transient
    read timeouts often enough in practice (this exact "Read timed out"
    on doc-0s-60-sheets.googleusercontent.com has shown up repeatedly
    during ordinary local runs) that a single-attempt fetch occasionally
    drops real, already-working data for an entire build - which is
    exactly what happened live once already: a timeout on the Top Trumps
    stats sheet alone silently took out Card Clash's container AND the
    Chart.js script tag it shares with the Dice Battle radar, breaking
    both features from one flaky request. Raises on final failure -
    every caller already wraps this in its own try/except and logs it.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(sheet_url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


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
        text = _fetch_sheet_text(sheet_url)
        import csv
        import io
        reader = csv.DictReader(io.StringIO(text))
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
                "_source_title": film,
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
    if isinstance(val, str):
        val = val.strip().replace("%", "").replace("$", "").replace(",", "")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val):
    f = _to_float(val)
    return int(f) if f is not None else None


def _film_stats_entry(genre, rt_audience, rt_critics, imdb_rating, year, guest=None,
                       sub_genre=None, runtime=None, explosions=None, deaths=None,
                       animated_chars=None, budget_adj=None, dom_rev_adj=None, intl_rev_adj=None,
                       source_title=None):
    rt_audience = _to_float(rt_audience)
    rt_critics = _to_float(rt_critics)
    # Tolerate scores entered either as 0-1 (0.94) or 0-100/"94%" - both
    # normalise to a 0-100 number here.
    if rt_audience is not None and rt_audience <= 1:
        rt_audience *= 100
    if rt_critics is not None and rt_critics <= 1:
        rt_critics *= 100
    budget_adj = _to_float(budget_adj)
    dom_rev_adj = _to_float(dom_rev_adj)
    intl_rev_adj = _to_float(intl_rev_adj)
    box_office_adj = None
    if dom_rev_adj is not None or intl_rev_adj is not None:
        box_office_adj = (dom_rev_adj or 0) + (intl_rev_adj or 0)
    return {
        "_source_title": source_title,
        "genre": (genre or "").strip() or None,
        "sub_genre": (sub_genre or "").strip() or None,
        "rt_audience": rt_audience,
        "rt_critics": rt_critics,
        "imdb_rating": _to_float(imdb_rating),
        "guest": (guest or "").strip() or None,
        "year": _to_int(year),
        "runtime": _to_int(runtime),
        "explosions": _to_int(explosions),
        "deaths": _to_int(deaths),
        "animated_chars": _to_int(animated_chars),
        "budget_adj": budget_adj,
        "box_office_adj": box_office_adj,
        "dom_rev_adj": dom_rev_adj,
        "intl_rev_adj": intl_rev_adj,
    }


def load_film_stats(sheet_url):
    """
    Reads the "Stats" tab of the shared Podcast S2 Google Sheet, published
    to web as CSV. It's laid out as two side-by-side blocks in one sheet -
    Kev's films in columns A-W, a blank spacer column, then Andy's films in
    columns Y-AL - rather than one film per row, so this reads by column
    position (not header name, since "Release Year"/"Main Genre"/etc appear
    twice) and pulls a film from either or both blocks per row.
    Used to power the Stats page's interactive charts, the Top Trumps genre
    filter, and the Episodes page's genre/guest/year filters.
    Returns {} until a real film_stats_sheet_url is set in config.json - the
    site works fine either way, it just falls back to the old static chart
    images and unfiltered grids until this is wired up.
    """
    if not sheet_url or "PASTE_" in sheet_url or not requests:
        return {}
    try:
        text = _fetch_sheet_text(sheet_url)
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text)))
        out = {}
        for row in rows[2:]:  # skip the "KEV'S FILMS/ANDY'S FILMS" banner row + the column-header row
            if len(row) < 5:
                continue
            get = lambda i: row[i] if i < len(row) else ""

            kev_title = get(4).strip()
            if kev_title:
                out[normalize_title(kev_title)] = _film_stats_entry(
                    genre=get(7), rt_audience=get(10), rt_critics=get(11),
                    imdb_rating=get(13), year=get(6),
                    sub_genre=get(8), runtime=get(9), explosions=get(14),
                    deaths=get(15), animated_chars=get(16),
                    budget_adj=get(18), dom_rev_adj=get(21), intl_rev_adj=get(22),
                    source_title=kev_title,
                )

            andy_title = get(26).strip()
            if andy_title:
                out[normalize_title(andy_title)] = _film_stats_entry(
                    genre=get(28), rt_audience=get(31), rt_critics=get(32),
                    imdb_rating=get(34), year=get(27),
                    sub_genre=get(29), runtime=get(30), explosions=get(35),
                    deaths=get(36), animated_chars=get(37),
                    # Andy's block didn't have budget/revenue columns
                    # originally, but they've since been added at AM-AR,
                    # mirroring Kev's block's layout at R-W exactly.
                    budget_adj=get(39), dom_rev_adj=get(42), intl_rev_adj=get(43),
                    source_title=andy_title,
                )
        return out
    except Exception as e:
        print(f"  Film stats sheet fetch failed: {e}")
        return {}


def load_toptrumps_stats(sheet_url):
    """
    Reads the "Top Trumps (update)" tab of the Podcast S2 sheet, published
    to web as CSV - one row per film across both seasons together (unlike
    the Stats tab's two-side-by-side-blocks layout), with the five numeric
    ratings that power the Top Trumps head-to-head radar chart on the DVD
    Extras page: First Watch, Rewatch, Romance, Action and Soundtrack (all
    out of 100).

    Column F is headed "Statham" in the sheet itself (an in-joke - Jason
    Statham as the site's mascot for action), read here into the "action"
    key and always labelled "Action" in the UI, since visitors won't have
    the context for the joke.

    Also carries "quote" (column H, "Line") along where it's filled in -
    it can't be a fifth radar axis (it's a line of dialogue, not a
    magnitude), so the front end shows it as flavour text under the chart
    instead.

    A film only makes it into the radar chart's film pickers if all five
    numeric ratings are present - a partially-filled row (common while
    this sheet is still being backfilled) would draw a lopsided, slightly
    misleading shape, so it's left out entirely rather than shown with
    gaps treated as zero.

    Returns {} until a real toptrumps_stats_sheet_url is set in
    config.json - the radar chart just doesn't render until then.
    """
    if not sheet_url or "PASTE_" in sheet_url or not requests:
        return {}
    try:
        text = _fetch_sheet_text(sheet_url)
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return {}
        # Looked up by header name rather than a fixed column position - a
        # "Reorder" column got inserted before "Title" at some point after
        # this was first written, which silently shifted every hardcoded
        # index by one and made every row fail the "5 numeric ratings"
        # check below (title landed on a number, ratings landed on text),
        # emptying radar_films and quietly taking out the Card Clash
        # comparison tool with no error anywhere. Reading by name survives
        # the sheet gaining, losing, or reordering columns in the future.
        header = [h.strip() for h in rows[0]]
        try:
            col = {
                "title": header.index("Title"),
                "first_watch": header.index("First Watch"),
                "rewatch": header.index("Rewatch"),
                "romance": header.index("Romance"),
                "action": header.index("Statham"),
                "soundtrack": header.index("Soundtrack"),
                "quote": header.index("Line"),
            }
        except ValueError as e:
            print(f"  Top Trumps stats sheet missing an expected column: {e}")
            return {}
        out = {}
        for row in rows[1:]:  # skip the header row
            get = lambda key: row[col[key]] if col[key] < len(row) else ""
            title = get("title").strip()
            if not title:
                continue
            first_watch = _to_int(get("first_watch"))
            rewatch = _to_int(get("rewatch"))
            romance = _to_int(get("romance"))
            action = _to_int(get("action"))
            soundtrack = _to_int(get("soundtrack"))
            if None in (first_watch, rewatch, romance, action, soundtrack):
                continue
            out[normalize_title(title)] = {
                "title": title,
                "first_watch": first_watch,
                "rewatch": rewatch,
                "romance": romance,
                "action": action,
                "soundtrack": soundtrack,
                "quote": get("quote").strip() or None,
            }
        return out
    except Exception as e:
        print(f"  Top Trumps stats sheet fetch failed: {e}")
        return {}


def load_dice_battle(sheet_url):
    """
    Reads the "Top Trumps Totals (update)" tab, published to web as CSV -
    the weekly game where each host scores the OTHER's pick: Andy rates
    that week's Kev S02 film, Kev rates the correspondingly-numbered Andy
    S01 film (paired by rank, e.g. rank 100's round is Andy scoring "The
    Usual Suspects" against Kev scoring "Baby Driver"), then a dice roll
    and a call on who won that round.

    Two side-by-side blocks like the Stats tab, but unlike that one there's
    no shared row count to rely on - every rank from 1 to 100 has a row
    (Andy's S01 side is fully populated, since Season 1 already finished
    airing), but the Kev S02 side - and therefore the dice roll/winner
    call for that round - is only filled in for ranks that have actually
    aired. A round only counts as "played" once it has a Winner (deliberately
    not just non-empty score cells, matching this sheet's own convention -
    every unplayed row's "TT Scores" cell shows the CURRENT running total
    copied down as a placeholder for what it'll become, not a real score,
    which is exactly why this function computes its own tally from the
    Winner column below rather than trusting that column).

    Returns [] until a real dice_battle_sheet_url is set in config.json.
    """
    if not sheet_url or "PASTE_" in sheet_url or not requests:
        return []
    try:
        text = _fetch_sheet_text(sheet_url)
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text)))
        out = []
        for row in rows[2:]:  # first two rows are the block headers
            get = lambda i: row[i] if i < len(row) else ""
            rank = _to_int(get(0))
            if rank is None:
                continue
            winner_raw = get(9).strip().lower()
            winner = {"andy": "andy", "a": "andy", "kev": "kev", "k": "kev"}.get(winner_raw)
            out.append({
                "rank": rank,
                "kev_film": get(1).strip() or None,
                "andy_scores_kev": {
                    "first_watch": _to_int(get(2)), "rewatch": _to_int(get(3)),
                    "romance": _to_int(get(4)), "action": _to_int(get(5)),
                    "soundtrack": _to_int(get(6)),
                },
                "andy_best_line": get(7).strip() or None,
                "dice_roll": get(8).strip() or None,
                "winner": winner,
                "andy_film": get(15).strip() or None,
                "kev_scores_andy": {
                    "first_watch": _to_int(get(16)), "rewatch": _to_int(get(17)),
                    "romance": _to_int(get(18)), "action": _to_int(get(19)),
                    "soundtrack": _to_int(get(20)),
                },
                "kev_best_line": get(21).strip() or None,
                "played": winner is not None,
            })
        return out
    except Exception as e:
        print(f"  Dice Battle sheet fetch failed: {e}")
        return []


def attach_posters_to_radar_films(radar_films, episodes):
    """Stamps a real poster URL and release year onto each Top Trumps
    Head-to-Head film (matched by title, same fuzzy lookup used everywhere
    else), for the Card Clash comparison UI's poster art. Both come
    straight from the episode's own OMDb-fetched data - no separate
    lookup or API call needed. A film that doesn't resolve to an episode
    (or resolves to one still missing a poster) just gets poster=None,
    which the front end falls back to a styled title card for, same as
    any other missing-art case on this site."""
    # _fuzzy_title_lookup's word-boundary-containment pass needs each
    # candidate's own non-normalized title under "_source_title" (see its
    # docstring) - episodes carry that as "film_title" instead, so it's
    # copied across rather than passing the raw episode dicts straight in.
    by_title = {
        normalize_title(ep["film_title"]): dict(ep, _source_title=ep["film_title"])
        for ep in episodes
    }
    for film in radar_films:
        ep = _fuzzy_title_lookup(film["title"], by_title) if by_title else {}
        film["poster"] = ep.get("poster")
        film["year"] = ep.get("year")
    return radar_films


def attach_film_stats_to_toptrumps(toptrumps_cards, film_stats, episodes=None):
    """Stamps genre/rating data onto each Top Trumps card (matched by title,
    same loose normalize_title match used everywhere else) so the grid can
    be filtered/sorted client-side without any extra config. Also stamps the
    matching episode's season (when known) so the Stats page's season
    toggle can filter the Top Trumps grid the same way it filters charts."""
    season_by_title = {}
    if episodes:
        for ep in episodes:
            if ep.get("season") is not None:
                season_by_title[normalize_title(ep["film_title"])] = ep["season"]
    for card in toptrumps_cards:
        key = normalize_title(card["title"])
        stats = _fuzzy_title_lookup(card["title"], film_stats)
        card["genre"] = stats.get("genre")
        card["rt_audience"] = stats.get("rt_audience")
        card["imdb_rating"] = stats.get("imdb_rating")
        card["season"] = season_by_title.get(key)
    return toptrumps_cards


def compute_genre_breakdown(episodes):
    """Counts how many published episodes fall into each genre, split by
    season, for the Stats page's genre chart + season toggle. Only counts
    episodes that matched a genre via the film stats sheet - returns []
    (chart hidden) until that's wired up."""
    counts = {}
    for ep in episodes:
        genre = ep.get("genre")
        if genre:
            key = (genre, ep.get("season"))
            counts[key] = counts.get(key, 0) + 1
    return [
        {"genre": g, "count": c, "season": s}
        for (g, s), c in sorted(counts.items(), key=lambda x: -x[1])
    ]


def compute_genre_films(episodes):
    """[{genre, season, film, rank}, ...] - one row per episode that has a
    genre, for the genre doughnut's tooltip (top 3 favourite-ranked films
    per slice - see stats.html). Kept as raw per-film rows rather than
    pre-computing the top 3 here, since the doughnut re-aggregates by
    whichever season is currently selected client-side (same pattern as
    compute_genre_breakdown's counts) and the top 3 need to be re-ranked
    for that same live selection, not fixed at build time. "rank" is the
    film's position on its host's original 100-film list (lower = higher
    favourite) - comparing ranks across Kev's and Andy's separate lists
    isn't a rigorous ranking, but it's a reasonable "favourite-ish" sort
    for a bit of tooltip trivia. None for episodes with no single ranked
    film (recaps, etc.) - those just sort last, never picked as a top 3."""
    return [
        {"genre": ep["genre"], "season": ep.get("season"), "film": ep["film_title"], "rank": ep.get("rank")}
        for ep in episodes
        if ep.get("genre")
    ]


def compute_critics_vs_audience(episodes):
    """[{film, critics, audience, season, genre, toptrumps_slug}, ...] for the
    critics-vs-audience scatter chart - only episodes with both scores
    available. genre/toptrumps_slug are along for the ride so the Stats page
    can click-filter this chart by genre and show each point's Top Trumps
    card in its hover tooltip, without a second lookup."""
    points = []
    for ep in episodes:
        if ep.get("rt_critics") is not None and ep.get("rt_audience") is not None:
            points.append({
                "film": ep["film_title"],
                "critics": ep["rt_critics"],
                "audience": ep["rt_audience"],
                "season": ep.get("season"),
                "genre": ep.get("genre"),
                "toptrumps_slug": ep.get("toptrumps_slug"),
            })
    return points


def compute_leaderboard(episodes, field):
    """[{film, value, season, genre, runtime}, ...] sorted highest-first for
    a numeric field (explosions, deaths, animated_chars, box_office_adj,
    ...) - powers the Stats page's interactive trivia leaderboard charts.
    Only includes episodes where that field is actually known.

    Carries "runtime" along (when known) so the trivia leaderboards (deaths/
    explosions/animated characters) can offer a per-minute-of-runtime view
    client-side, not just raw totals - without it, a 3-hour epic and a
    90-minute film are compared on totals alone, which mostly just measures
    "how long is this film" rather than anything about the film itself.

    Deliberately returns the FULL sorted list rather than truncating to a
    top N here - the Stats page slices out the top 10 or bottom 10 client
    side depending on the season/genre filter and the "Reverse Order"
    toggle, and it needs the low end of the list on hand to do that (e.g.
    "least explosive" or "biggest box-office flops"), not just the top 10
    with the truncated tail unavailable."""
    points = [
        {
            "film": ep["film_title"],
            "value": ep[field],
            "season": ep.get("season"),
            "genre": ep.get("genre"),
            "runtime": ep.get("runtime"),
        }
        for ep in episodes
        if ep.get(field) is not None
    ]
    points.sort(key=lambda p: p["value"], reverse=True)
    return points


def compute_box_office_leaderboard(episodes):
    """[{film, value, dom, intl, season, genre}, ...] sorted by total
    inflation-adjusted box office (dom + intl) - like compute_leaderboard,
    but carries the domestic/international split alongside the total so
    the Stats page's box office chart can switch between Total/Domestic/
    International without three separate server-computed lists. "value" is
    always the total, kept under that name so the chart's default (Total)
    view can reuse the same rendering code path as compute_leaderboard's
    other charts; "dom"/"intl" fall back to 0 (not the whole row being
    dropped) when only one half of a film's revenue is on file, since a
    missing half isn't a reason to hide a film that does have a real total."""
    points = [
        {
            "film": ep["film_title"],
            "value": ep["box_office_adj"],
            "dom": ep.get("dom_rev_adj") or 0,
            "intl": ep.get("intl_rev_adj") or 0,
            "season": ep.get("season"),
            "genre": ep.get("genre"),
        }
        for ep in episodes
        if ep.get("box_office_adj") is not None
    ]
    points.sort(key=lambda p: p["value"], reverse=True)
    return points


def compute_budget_vs_boxoffice(episodes):
    """[{film, budget, box_office, roi, audience, rank, season, genre,
    toptrumps_slug}, ...] for the budget-vs-box-office bubble chart - only
    films with both a budget and a box office figure on file (a budget
    alone, or box office alone, can't place a point on this chart).

    "roi" is (box_office - budget) / budget as a percentage - can go
    negative (a loss) - computed once here rather than client-side so the
    Total/ROI axis toggle is just picking which existing field to plot,
    same pattern as the box office leaderboard's Total/US/International
    toggle. "audience" (RT audience score, when known) drives the bubble
    colour and "rank" drives its size - both optional extras layered on
    top of the two axes that actually place the point, so a film missing
    either still shows up, just as a default-sized/coloured bubble."""
    points = []
    for ep in episodes:
        budget = ep.get("budget_adj")
        box_office = ep.get("box_office_adj")
        if budget is None or box_office is None or budget <= 0:
            continue
        points.append({
            "film": ep["film_title"],
            "budget": budget,
            "box_office": box_office,
            "roi": round((box_office - budget) / budget * 100, 1),
            "audience": ep.get("rt_audience"),
            "rank": ep.get("rank"),
            "season": ep.get("season"),
            "genre": ep.get("genre"),
            "toptrumps_slug": ep.get("toptrumps_slug"),
        })
    return points


# Brand names that .title() would otherwise mangle (e.g. "outout" -> "Outout").
TITLE_FIXUPS = {"Outout": "OutOut", "Lpv": "LPV"}


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
    into a category guessed from the filename (see categorize_shop_item).

    A file named "<name>-back.<ext>" is treated as the hover/back-side
    image for "<name>.<ext>" (e.g. "stubby-holders.png" +
    "stubby-holders-back.png") - hovering the card on the Shop page
    crossfades to it. Optional; a product with no "-back" file just shows
    the one image as always.
    """
    folder = os.path.join(ROOT, "static", "shop")
    if not os.path.isdir(folder):
        return []
    image_files = [f for f in sorted(os.listdir(folder)) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]

    back_by_stem = {}
    front_files = []
    for fname in image_files:
        stem = os.path.splitext(fname)[0]
        if stem.lower().endswith("-back"):
            back_by_stem[stem[: -len("-back")]] = fname
        else:
            front_files.append(fname)

    grouped = {}
    for fname in front_files:
        stem = os.path.splitext(fname)[0]
        title = stem.replace("-", " ").replace("_", " ").title()
        for wrong, right in TITLE_FIXUPS.items():
            title = title.replace(wrong, right)
        category = categorize_shop_item(fname)
        item = {"file": fname, "title": title}
        back_file = back_by_stem.get(stem)
        if back_file:
            item["back_file"] = back_file
        grouped.setdefault(category, []).append(item)

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
    film_years = load_film_years()
    episodes = fetch_episodes(config.get("rss_url", ""), config.get("omdb_api_key", ""), drinks_snacks, film_stats, film_years)
    episodes = infer_missing_seasons(episodes)
    toptrumps_cards = load_toptrumps_cards()
    toptrumps_cards = attach_film_stats_to_toptrumps(toptrumps_cards, film_stats, episodes)
    toptrumps_genres = sorted({c["genre"] for c in toptrumps_cards if c.get("genre")})
    episodes = attach_toptrumps_links(episodes, toptrumps_cards)
    seasons = group_by_season(episodes)
    for group in seasons:
        group["decades"] = group_by_decade(group["episodes"])
    shop_items = load_shop_items()
    titles = load_titles()
    warn_about_surprise_title_duplicates(titles)
    progress = season_progress(seasons)
    episodes_with_drinks = [ep for ep in episodes if ep.get("kev_drink_name") or ep.get("andy_drink_name") or ep.get("snack_name")]
    episodes_with_snacks = [ep for ep in episodes if ep.get("snack_name")]
    start_here_picks = load_start_here_picks(episodes)
    genre_breakdown = compute_genre_breakdown(episodes)
    genre_films = compute_genre_films(episodes)
    critics_vs_audience = compute_critics_vs_audience(episodes)
    explosions_leaderboard = compute_leaderboard(episodes, "explosions")
    deaths_leaderboard = compute_leaderboard(episodes, "deaths")
    animated_leaderboard = compute_leaderboard(episodes, "animated_chars")
    box_office_leaderboard = compute_box_office_leaderboard(episodes)
    budget_vs_boxoffice = compute_budget_vs_boxoffice(episodes)
    toptrumps_stats = load_toptrumps_stats(config.get("toptrumps_stats_sheet_url", ""))
    # Sorted alphabetically so the two Top Trumps radar dropdowns are easy
    # to scan - only films with a complete set of four ratings are in here
    # at all (see load_toptrumps_stats), so no extra filtering needed here.
    radar_films = sorted(toptrumps_stats.values(), key=lambda d: d["title"].lower())
    attach_posters_to_radar_films(radar_films, episodes)
    dice_battle_rounds = load_dice_battle(config.get("dice_battle_sheet_url", ""))
    # Ascending by rank = most-recently-aired round first (S02's countdown
    # airs highest rank number first, working down towards Kev's #1
    # favourite, so the lowest rank number reached so far is the newest
    # episode - see load_dice_battle's docstring for the full pairing
    # explanation).
    # A round only actually counts once the matching Kev S02 episode has
    # itself gone out publicly - the sheet's Winner column has been seen
    # filled in a rank ahead of the real RSS feed (looks like results get
    # logged as they're recorded, not when the episode airs), and showing
    # that round here would spoil a result for an episode nobody's heard
    # yet. Cross-checking against the real aired ranks, not just the
    # sheet's own "played" flag, is what keeps this spoiler-safe.
    aired_kev_ranks = {ep["rank"] for ep in episodes if ep.get("season") == 2 and ep.get("rank")}
    dice_battle_played = sorted(
        (r for r in dice_battle_rounds if r["played"] and r["rank"] in aired_kev_ranks),
        key=lambda r: r["rank"],
    )
    dice_battle_tally = {
        "andy": sum(1 for r in dice_battle_played if r["winner"] == "andy"),
        "kev": sum(1 for r in dice_battle_played if r["winner"] == "kev"),
    }
    episode_genres = sorted({ep["genre"] for ep in episodes if ep.get("genre")})
    episode_guests = sorted({ep["guest"] for ep in episodes if ep.get("guest")})
    episode_years = sorted({ep["year"] for ep in episodes if ep.get("year")}, reverse=True)
    # Sub-Genres is a comma-separated cell (e.g. "Crime, Drama") rather than
    # one value per film, so the dropdown lists every individual sub-genre
    # that appears anywhere, and the client-side filter checks whether the
    # selected one appears anywhere in an episode's full sub-genre string.
    episode_subgenres = sorted({
        s.strip() for ep in episodes for s in (ep.get("sub_genre") or "").split(",") if s.strip()
    })
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
        "genre_films": genre_films,
        "critics_vs_audience": critics_vs_audience,
        "explosions_leaderboard": explosions_leaderboard,
        "deaths_leaderboard": deaths_leaderboard,
        "animated_leaderboard": animated_leaderboard,
        "box_office_leaderboard": box_office_leaderboard,
        "budget_vs_boxoffice": budget_vs_boxoffice,
        "radar_films": radar_films,
        "dice_battle_played": dice_battle_played,
        "dice_battle_tally": dice_battle_tally,
        "toptrumps_genres": toptrumps_genres,
        "episode_genres": episode_genres,
        "episode_guests": episode_guests,
        "episode_years": episode_years,
        "episode_subgenres": episode_subgenres,
        "episode_slug_lookup": episode_slug_lookup,
        "year": datetime.now().year,
        "root": "",
    }

    pages = [
        "index.html", "episodes.html", "about.html", "stats.html",
        "toptrumps.html", "shop.html", "starthere.html",
    ]
    for page in pages:
        template = env.get_template(page)
        # encoding="utf-8" is required here on Windows - without it, open()
        # falls back to the system codepage (cp1252), which can't encode
        # the drink/glass emoji now flowing through the Drinks & Snacks
        # sheet and crashes the build the moment any page renders one.
        with open(os.path.join(OUT, page), "w", encoding="utf-8") as f:
            f.write(template.render(**common))

    print(f"Built {len(pages)} pages with {len(episodes)} episodes into {OUT}/")


if __name__ == "__main__":
    build()
