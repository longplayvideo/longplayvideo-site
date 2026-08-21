# -*- coding: utf-8 -*-
"""
Regenerates "Long Play Video - Season 2 Letterboxd Import.csv" from live
data - the RSS feed plus the Drinks/Snacks and Film Stats Google Sheets -
so the CSV can be refreshed any time new Season 2 episodes air without
redoing the title-matching/data-gathering work by hand.

What's automatic:
  - which films have aired and are ranked (pulled straight from the RSS feed)
  - each film's year, episode link, and real drink/snack pairings (pulled
    straight from the sheets, via the same fuzzy title-matching
    generate_site.py uses on the site itself)
  - the star rating (rank-based formula, same one Season 1 used)
  - CSV row order (rank ascending, so the best-ranked aired film lands in
    row 2 - see the "Strategy Note" in the original Season 1 spec for why)

What's NOT automatic, on purpose:
  - the description text. These are meant to read as hand-written, varied,
    "sound human" copy (that was the brief for Season 1 too), not
    templated fill-in-the-blanks - so a real description has to be written
    for each newly-aired film before it can go in the CSV. This script
    keeps a running store of already-written descriptions
    (s2_letterboxd_descriptions.json, keyed by rank) and reuses them as-is;
    for any aired+ranked film with no entry yet, it skips that row and
    lists it at the end instead of inventing filler copy or guessing.

Usage:
    python build_s2_letterboxd_csv.py

Typical flow when new episodes have aired:
    1. Run this script.
    2. If it reports missing descriptions, ask Claude to write them (real
       drink/snack/rank/link data for each missing film is printed out
       ready to hand over) and add the results to
       s2_letterboxd_descriptions.json.
    3. Run this script again - now every aired film has a row.
    4. Re-import the CSV into Letterboxd (Edit List -> Import, or however
       Letterboxd's current UI phrases "update from CSV" - that step has
       no API, so it stays a manual drag-and-drop).
"""
import csv
import json
import os

import generate_site as gs

ROOT = os.path.dirname(os.path.abspath(__file__))
DESCRIPTIONS_PATH = os.path.join(ROOT, "s2_letterboxd_descriptions.json")
OUTPUT_PATH = os.path.join(ROOT, "Long Play Video - Season 2 Letterboxd Import.csv")


def round_half(x):
    """Nearest 0.5, matching the rating formula used for the Season 1 CSV."""
    return round(x * 2) / 2


def rating_for_rank(rank):
    # Same absolute rank/100 scale as Season 1, kept for consistency across
    # both lists. Note: while Season 2 has only aired its bottom-ranked
    # films so far, this will only ever produce 3.0-3.5 stars - it opens up
    # to the full 3.0-5.0 range as higher-ranked (better) films air.
    return round_half(5.0 - (rank - 1) / 99 * 2.0)


def main():
    config = gs.load_config()
    drinks_snacks = gs.load_drinks_snacks(config["drinks_sheet_url"])
    film_stats = gs.load_film_stats(config["film_stats_sheet_url"])
    episodes = gs.fetch_episodes(
        config["rss_url"], config["omdb_api_key"], drinks_snacks, film_stats
    )

    aired = [
        ep for ep in episodes
        if ep.get("season") == 2 and ep.get("film_title") and ep.get("rank")
    ]
    aired.sort(key=lambda e: e["rank"])

    with open(DESCRIPTIONS_PATH, encoding="utf-8") as f:
        descriptions = json.load(f)

    rows = []
    missing = []
    for ep in aired:
        rank = ep["rank"]
        entry = descriptions.get(str(rank))
        if not entry:
            missing.append(ep)
            continue
        desc = entry["desc"].format(link=ep["link"])
        rows.append([entry["title"], ep["year"], desc, rating_for_rank(rank)])

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Title", "Year", "Description", "Rating"])
        w.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")

    if missing:
        print()
        print(f"{len(missing)} aired/ranked film(s) have no description yet "
              f"in {os.path.basename(DESCRIPTIONS_PATH)}, so they were left "
              f"out of the CSV:")
        for ep in missing:
            print()
            print(f"  #{ep['rank']}  {ep['film_title']} ({ep['year']})")
            print(f"    link: {ep['link']}")
            print(f"    Kev's drink:  {ep.get('kev_drink_name')} "
                  f"({ep.get('kev_glass_name')})")
            print(f"    Andy's drink: {ep.get('andy_drink_name')} "
                  f"({ep.get('andy_glass_name')})")
            print(f"    Snack:        {ep.get('snack_name')}")
        print()
        print("Hand this list to Claude to get fresh descriptions written, "
              "add them to " + os.path.basename(DESCRIPTIONS_PATH) +
              " (keyed by rank), then re-run this script.")


if __name__ == "__main__":
    main()
