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
   - `omdb_api_key` - powers the DVD cover art and IMDb links on the episode
     cards. Get a free key at **omdbapi.com/apikey.aspx** (pick the free
     1,000-requests/day tier, verify your email, they'll send you a key) and
     paste it in. If you skip this, the site still works fine - episodes just
     show a placeholder cover instead of the real one, and IMDb links fall
     back to a search link instead of the exact title page.
   - `sheet_url` - a share link to your Lists & Stats spreadsheet (optional)
   - `drinks_sheet_url` - a Google Sheet published to web as CSV, with the
     weekly drink + snack per film (optional - see "Drinks & Snacks sheet"
     below for the column format)
   - `contact_email` - your contact email (optional)
   - `merch_url` - your Inkthreadable store link, for the Shop tab (optional)
   - `outout_url` - OutOut's website, shown on the About page and in the
     footer (optional - I've guessed **outout.info**, double-check it's the
     right OutOut before going live)
   - `site_url` - the live site's full URL once it's deployed (e.g.
     `https://yourname.github.io/longplayvideo-site/`), used so social
     link previews (Open Graph) can find the preview image (optional)

   Leaving any of these as the `PASTE_...` placeholder just hides that
   section until you fill it in - nothing breaks.

   Commit the change.
5. Go to the repository's **Settings** tab → **Pages** (left sidebar). Under
   "Build and deployment", set **Source** to **GitHub Actions**.
6. Go to the **Actions** tab and click **Build and deploy site** → **Run
   workflow** to trigger the first build. After ~1 minute it'll finish and
   your site will be live at `https://<your-username>.github.io/<repo-name>/`.

From here on, the site rebuilds itself automatically every day, and instantly
whenever you push a change.

## Using your longplay.video domain instead of the github.io address

1. In the repository, go to **Settings → Pages**, and under "Custom domain"
   type `longplay.video`, then **Save**. (Because this site builds via a
   GitHub Actions workflow rather than publishing a branch directly, GitHub
   won't create a `CNAME` file for this - that's expected, nothing else to
   do here.)
2. Go to wherever you manage DNS for longplay.video (your domain
   registrar, e.g. GoDaddy, Namecheap, Google Domains, etc.) and add these
   four **A records** for the root/apex domain (usually entered as `@`):

   ```
   185.199.108.153
   185.199.109.153
   185.199.110.153
   185.199.111.153
   ```

   If your registrar also supports `AAAA` records and you want IPv6
   support too (optional):

   ```
   2606:50c0:8000::153
   2606:50c0:8001::153
   2606:50c0:8002::153
   2606:50c0:8003::153
   ```

   Remove any default "parking page" A record your registrar may have set
   first.
3. (Recommended) Also add a `www` version so `www.longplay.video` works
   too: create a **CNAME record** for `www` pointing to
   `<your-github-username>.github.io` (no repository name on the end).
4. DNS changes can take up to 24 hours to fully propagate, though it's
   often much faster. Once it's resolving, go back to **Settings → Pages**
   and tick **Enforce HTTPS** (this option only appears once GitHub has
   verified the domain, which can also take a little while).
5. Once it's live, update `site_url` in `config.json` to
   `https://longplay.video/` so social link previews (Open Graph) point at
   the right place.

## Drinks & Snacks sheet

To turn on the drink/snack tags on episode cards and the Drinks Menu panel
(inside Top Trumps & Stats), create a Google Sheet with these columns:

`Film | Kev Drink Name | Kev Drink Category | Kev Glass | Andy Drink Name | Andy Drink Category | Andy Glass | Snack Name | Snack Category`

Each episode card can show up to three tags - Kev's drink, Andy's drink,
and the snack - any of the three left blank for a film just doesn't show
that tag. `Film` just needs to roughly match the movie's name (casing and
punctuation don't matter, matching ignores both).

The drink tag's icon is pulled from the leading emoji in `Kev Glass` /
`Andy Glass` (e.g. type `🥃 Rocks glass`), with the rest of that cell shown
as a tooltip on hover - so the icon is whatever glass emoji you actually
type each week, no list to maintain. Leave a Glass cell blank and it falls
back to guessing an icon from the matching Category cell instead.

Then **File → Share → Publish to web**, choose CSV, copy the link into
`drinks_sheet_url` in `config.json`. Category-based fallback icons map by
keyword match, so close wording still works - beer, wine, cocktail, shot,
rocks/spirit/neat, sake, champagne/sparkling, hot drink/coffee/tea,
alcopop/RTD, non-alcoholic/mocktail/soft drink/highball for drinks; cinema
concession, movie easter egg, novelty/retro, regional/cultural, at-home
party snack, popcorn, nachos, pizza, candy, chocolate, ice cream, hot dog,
chips for snacks. Anything outside that list
falls back to a default icon. Leave the field as the placeholder and this
section just stays hidden.

## Shop images

Drop product photos into `static/shop/` (jpg/png/webp) and they show up
automatically in the Shop page's showcase gallery - same folder-drop
pattern as Top Trumps cards. The filename becomes the title (e.g.
`tote-bag.jpg` → "Tote Bag").

- **To replace an existing product photo**: just re-upload a file with the
  exact same filename via GitHub's "Add file → Upload files" - it'll
  detect the name match and offer to overwrite it.
- **To add a new product**: upload a new image with a new filename.
- **To remove a product**: delete its file from `static/shop/` in GitHub.
- **Category**: guessed automatically from the filename ending -
  `-tee` or `-hoodie` → Clothing, `-cap` → Headwear, anything else →
  Accessories & Extras (e.g. `logo-tee.jpg`, `season1-cap.jpg`).
- **Sizing**: images are cropped to the same tall card shape as the Top
  Trumps cards and zoomed to fill the frame (`object-fit: cover`), so a
  roughly portrait-oriented photo (about 2:3, same as a trading card)
  will look best - very wide or very square photos will get cropped at
  the sides/top-bottom to fit.
- **Front/back hover**: add a second image named `<same-name>-back.<ext>`
  (e.g. `stubby-holders.png` + `stubby-holders-back.png`) and hovering
  the card crossfades to it - handy for a tee's back print or the other
  side of an item. Optional, and only affects devices with a mouse -
  touch/mobile just shows the front image, there's no tap-to-flip yet.

## Top Trumps auto-linking

Episode cards automatically get a "Top Trumps" button whenever a card in
`static/toptrumps/` matches that episode's film title (ignoring
capitalisation and punctuation) - it links straight to that film's card
on the Stats page. Nothing to configure - just keep naming Top Trumps
files after the film, same as always.

## Homepage listen buttons

The hero's "listen" icons pull from `spotify_url`, `apple_podcasts_url`
and `amazon_music_url` in `config.json` - any of the three left as the
`PASTE_...` placeholder just won't show a button. The full platform list
still lives on the About page under "Also listen on".

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
