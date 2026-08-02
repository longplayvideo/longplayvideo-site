#!/usr/bin/env python3
"""
Builds the Long Play Video static site into _site/ from config.json + the
show's Spotify RSS feed. Run manually with `python generate_site.py`, or let
the GitHub Actions workflow (.github/workflows/deploy.yml) run it on a
schedule so the site keeps itself up to date with no manual work.
"""
import json
import os
import shutil
import sys
from datetime import datetime

import feedparser
from jinja2 import Environment, FileSystemLoader

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "_site")


def load_config():
    with open(os.path.join(ROOT, "config.json")) as f:
        return json.load(f)


def fetch_episodes(rss_url):
    if not rss_url or "PASTE_" in rss_url:
        print("No RSS feed URL set in config.json yet - building site with an empty episode list.")
        return []

    feed = feedparser.parse(rss_url)
    if feed.bozo and not feed.entries:
        print(f"Warning: couldn't parse RSS feed ({feed.bozo_exception}). Building with an empty episode list.")
        return []

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

        summary = entry.get("summary", "")
        if len(summary) > 400:
            summary = summary[:400].rsplit(" ", 1)[0] + "..."

        episodes.append({
            "title": entry.get("title", "Untitled episode"),
            "date": date_str,
            "duration": duration,
            "summary": summary,
            "link": link,
        })

    return episodes


def build():
    config = load_config()
    episodes = fetch_episodes(config.get("rss_url", ""))

    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    shutil.copytree(os.path.join(ROOT, "static"), os.path.join(OUT, "static"))

    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")))
    common = {"config": config, "episodes": episodes, "year": datetime.now().year, "root": ""}

    pages = ["index.html", "episodes.html", "about.html", "lists-stats.html", "contact.html"]
    for page in pages:
        template = env.get_template(page)
        with open(os.path.join(OUT, page), "w") as f:
            f.write(template.render(**common))

    print(f"Built {len(pages)} pages with {len(episodes)} episodes into {OUT}/")


if __name__ == "__main__":
    build()
