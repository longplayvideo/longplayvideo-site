# Long Play Video website (free, self-updating)

This is a complete website for the podcast that rebuilds itself automatically
from your Spotify RSS feed, hosted for free forever on GitHub Pages. No
Podpage subscription, no manual updates.

Setup is a one-time, ~10 minute job. After that you never have to touch it -
it checks your feed daily and rebuilds itself if there's a new episode.

## One-time setup

1. **Create a free GitHub account** at github.com/join (I can't do this step
   for you, it just needs an email address).
2. Click **New repository** (green button, top right). Name it whatever you
   like, e.g. `longplayvideo-site`. Leave it **Public**. Don't add a README,
   .gitignore, or license - this folder already has everything needed.
3. On the new repository's page, click **uploading an existing file**, then
   drag this entire folder's contents in (everything, including the hidden
   `.github` folder - if your browser hides it, see the note at the bottom).
   Commit the upload.
4. Open **config.json** in the repository (click it, then the pencil icon to
   edit) and fill in:
   - `rss_url` - from Spotify for Creators, go to **Settings → Availability**
     and copy the RSS feed link
   - `sheet_url` - a share link to your Lists & Stats spreadsheet (optional)
   - `contact_email` - your contact email (optional)

   Commit the change.
5. Go to the repository's **Settings** tab → **Pages** (left sidebar). Under
   "Build and deployment", set **Source** to **GitHub Actions**.
6. Go to the **Actions** tab and click **Build and deploy site** → **Run
   workflow** to trigger the first build. After ~1 minute it'll finish and
   your site will be live at `https://<your-username>.github.io/<repo-name>/`.

From here on, the site rebuilds itself automatically every day, and instantly
whenever you push a change. If you ever want your own domain
(longplay.video) instead of the github.io address, GitHub Pages supports
that for free too - just ask and I can walk you through pointing it.

## Extra images included

A couple of assets are sitting in `static/` ready to use but not wired into
any page yet:

- `season1-wide.jpg` - a landscape crop of the Season 1 video shop art
- `christmas-bonus.jpg` - the Christmas-special cover art

Drop either into a template (same pattern as the images already on the
About page) whenever you want to use them.

## Editing content later

- **Episodes** update themselves from your RSS feed - nothing to do.
- **About page bios**: edit `templates/about.html` directly in GitHub (click
  the file, click the pencil, edit, commit) - the site rebuilds within a
  minute of you saving.
- **Colours/branding**: `static/style.css`.
- **Cover image**: replace `static/cover.jpg`.

## Note on the hidden .github folder

Some drag-and-drop uploads on github.com skip folders starting with a dot.
If `.github/workflows/deploy.yml` doesn't show up after your upload, use
GitHub's **Add file → Create new file** button instead, type the path
`.github/workflows/deploy.yml` into the filename box (GitHub creates the
folders automatically), and paste in the contents of that file from this
folder.

## Running it locally (optional, not required)

If you ever want to preview changes before pushing:

```
pip install -r requirements.txt
python generate_site.py
```

This writes the finished site into `_site/`, which you can open directly in
a browser.
