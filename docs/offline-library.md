# Offline reference library and photo identification

Aegis can answer from a reference library stored on the server itself — a full
Wikipedia, medical references, Army field manuals and other survival / first-aid
/ repair manuals, Stack Exchange archives, how-to sites — with **no internet**.
It can also look at a photo and tell you what it shows (plant, mushroom, insect,
spider, snake, animal sign, an object, or the text on a label or page).

Nothing here is required: without a library Aegis behaves exactly as before.

## What you get

| Where | What happens |
|---|---|
| Web search (the globe toggle, the agent's `web_search`) | Unchanged while the internet is up; the library is tried last, after the web providers. **When the internet is down the library is searched first and only**, so "search" keeps answering. The reply says the results came from the offline library. |
| Agent mode | A `library_search` tool. The assistant uses it for settled knowledge and how-to questions, with or without internet, and cites the article or the manual page. Offered whenever a message is a question or would get web search. |
| `/lib <words>` | Quick lookup: a list of matching articles and manual pages with links. `/lib` alone shows what is installed. Aliases: `/kiwix`, `/lookup`, `/ref`, `/offline`. |
| The **camera button** on the message box, or `/identify [what you know]` | Take or choose a photo; the answer names the most likely match, what to check in hand, lookalikes (dangerous ones flagged) and what the library says about edibility or hazards, with reference photos. Text in a photo is transcribed. Aliases: `/id`, `/whatis`, `/ocr`. |
| **Reader** (`/read`) | Anything in the library opened for reading on Aegis's own page: your place kept, highlights, notes, and *Remember* to hand a passage to Aegis's memory. Links to the library in a chat open here. See [Reader and Kitchen](reader.md). |
| **Kitchen** (`/kitchen`) | A recipe from the library (or pasted, or from the web) set out for cooking, old measures translated. |
| Browser | The whole library is also browsable at `kiwix_public_url` (Kiwix's own viewer) — the place for collections whose front page is a menu drawn by script. |

The library has two halves:

1. **Kiwix collections** (ZIM files) served by `kiwix-serve`: every HTML page is
   full-text searchable.
2. **A PDF-manual index** built by Aegis. Kiwix's search does not look inside
   PDFs, and several of the most useful collections — the Army Publishing
   Directorate dump, the military-medicine set, the water / knots / post-disaster
   packs — are PDFs wrapped in a ZIM. `scripts/aegis-library index` reads every
   PDF out of every ZIM and indexes the text page by page (SQLite FTS5), so a
   question lands on "TC 4-02.1, page 37" and the answer links to that page.

## Setting it up

Packages (Arch names): `kiwix-tools` (kiwix-serve, kiwix-manage), `zim-tools`
(zimdump, used by the PDF indexer) and `aria2`. PyMuPDF is already in the venv.

```bash
mkdir -p ~/library/zim                       # or set AEGIS_LIBRARY_DIR

# 1. Choose collections (needs internet). Sizes are in the output.
scripts/aegis-library catalog survival
scripts/aegis-library catalog wikipedia_en_all            # names match too
scripts/aegis-library catalog wikipedia_en_all --flavour maxi --urls >> ~/library/zim-list.txt

# 2. Download (resumable, checksum-verified), then catalog + index.
setsid nohup scripts/library/fetch-zims.sh > ~/library/zim-download.log 2>&1 &
tail -f ~/library/zim-status.txt

# 3. Serve it.
sed -e "s|__USER__|$USER|g" -e "s|__LIBRARY_DIR__|$HOME/library|g" \
    scripts/library/kiwix.service.template > /tmp/kiwix.service
sudo install -m 644 /tmp/kiwix.service /etc/systemd/system/kiwix.service
sudo systemctl daemon-reload && sudo systemctl enable --now kiwix

# 4. Let a phone reach the reader (Tailscale shown; any reverse proxy works).
tailscale serve --bg --https=8443 http://127.0.0.1:8095
```

Settings (`data/settings.json`, all optional):

| Key | Default | Meaning |
|---|---|---|
| `library_enabled` | `true` | Master switch. |
| `kiwix_url` | `http://127.0.0.1:8095` | Where Aegis reaches kiwix-serve. |
| `kiwix_public_url` | empty = `kiwix_url` | Base of the links shown to you — an address your phone can open, e.g. `https://<host>.ts.net:8443`. |
| `library_dir` | `$AEGIS_LIBRARY_DIR` or `~/library` | Holds `zim/`, `library.xml`, `index/manuals.db`. |

Environment: `AEGIS_KIWIX_URL`, `AEGIS_LIBRARY_DIR`; `AEGIS_LIBRARY_DISABLED=1`
switches the library off regardless of settings (the test suite sets it).
`AEGIS_ASSUME_OFFLINE=1` makes Aegis behave as if the internet were down — the
way to rehearse an outage: start Aegis with it and ask something.

**Serve the reader on its own origin.** Archive pages carry their own scripts;
do not reverse-proxy the library under Aegis's hostname *and* port. The one
thing Aegis serves itself is `GET /api/reference/asset`, which passes through
raster images only (the reference photos in identification answers).

### What to put in it

A selection that covers "the internet is down and I need to do things", about
210 GB:

- `wikipedia_en_all` (maxi, 127 GB) — with images, which species identification
  leans on. `nopic` is 53 GB if space is short.
- Medical: `wikipedia_en_medicine` (WikiMed), `mdwiki_en_all`,
  `medlineplus.gov_en_all`, `nhs.uk_en_medicines`, `zimgit-medicine_en`,
  `irp.fas.org_en_military-medicine`, `wikivet.net_en_all`, `libretexts.org_en_med`.
- Survival and preparedness: `armypubs_en_all`, `zimgit-post-disaster_en`,
  `zimgit-water_en`, `zimgit-food-preparation_en`, `zimgit-knots_en`,
  `ready.gov_en_all`, `trueprepper.com_en_all`, `gutenberg_en_lcc-u` (military science).
- Food, farming, nature: `gutenberg_en_lcc-s` (agriculture, hunting, fishing,
  beekeeping), `usda-2015_en` (home canning), `gardenology_en_all`, the
  gardening / cooking / outdoors / homebrew Stack Exchange sites.
- Repair and building: `ifixit_en_all`, `appropedia_en_all`,
  `energypedia_en_all`, `gutenberg_en_lcc-t` (technology), the diy / mechanics /
  electronics / ham / woodworking Stack Exchange sites.
- Keeping the server itself running: `archlinux_en_all`, unix / superuser Stack
  Exchange, a few `devdocs_en_*`.
- `maps_en_united-states-of-america` (16 GB, road maps) or your own country's.

For reading rather than looking things up — about 55 GB more:

- Books by subject from Project Gutenberg: `gutenberg_en_lcc-q` (science, 12 GB),
  `-r` (medicine), `-g` (geography, travel), `-h` (social sciences), `-j` / `-k`
  (politics, law), `-l` (education), `-v` (naval science). The whole of
  Gutenberg in English is 221 GB; fiction alone (`lcc-p*`) is the bulk of it.
- `wikisource_en_all` (8 GB) — source texts: constitutions, speeches, old
  reference works.
- Textbooks: the `libretexts.org_en_*` shelves (biology, chemistry, physics,
  math, engineering, geosciences, statistics, humanities, social sciences,
  business, workforce trades, K-12) and `wikiversity_en_all`.
- Cooking: `publicdomainrecipes.com_en_all`, `based.cooking_en_all`,
  `foss.cooking_en_all`, `grimgrains_en_all`, and the cookbooks in
  `gutenberg_en_lcc-t` — which the [Kitchen](reader.md#kitchen) turns into
  recipes you can follow.

Everything Kiwix publishes is free to download and keep
(<https://library.kiwix.org>). Other lawful sources of whole books, to add as
PDFs: Standard Ebooks, the Internet Archive's public-domain texts, OpenStax
and the Open Textbook Library, the Directory of Open Access Books, NCBI
Bookshelf.

Not in Kiwix's catalog at all: modern field guides to plants, mushrooms and
insects. Wikipedia's species articles (with photos) carry that load; free
government and extension-service PDFs can be added as described next.

### Adding documents by asking

An administrator can grow the library from a chat, in agent mode:

> *Take these PDFs and add them to the survival library.* (with the files attached)
> *Add this to the library, in the water section: https://…/emergency-disinfection.pdf*
> *Go out to the internet, find an official first-aid guide for snake bites and add it.*

The assistant's `manage_library` tool keeps each document under
`<library>/added/<section>/`, reads it page by page into the same index the
manuals use, and it is found by the very next search — no repacking, no
restart. Each one is recorded in `added/catalogue.json` with where it came
from, who added it and when.

- **PDFs** from a link or from the chat's attachments, up to 300 MB each. The
  same file is never stored twice. Password-protected files are refused.
- **A link to a web page** is refused with the list of PDF files that page
  links to, so the right one can be added; asking for the page itself keeps
  its text as a document (`"as_page": true`).
- **Scanned PDFs** are read with OCR in the background (about a second per
  page); until that finishes they are in the library but their text cannot be
  searched. Reading that a restart cut short is picked up again.
- **Downloads are guarded** the same way the assistant's web fetch is: public
  addresses only, checked again on every redirect.
- Links to added documents are paths on Aegis itself
  (`/api/reference/added/…#page=N`), open to anyone signed in; adding and
  removing is for administrators. `manage_library` `list` shows what was
  added, `remove` takes one out.

Added documents live beside the packed collections, not inside them, so the
Kiwix reader does not list them — Aegis's own search, answers and links do.

### Adding a whole folder of PDFs

Anything served by kiwix-serve is indexed, so a folder of PDFs can also be
wrapped in a collection of its own:

```bash
# ~/library/manuals/<section>/<file>.pdf   — first-level folders become sections
venv/bin/python scripts/library/build-manuals-zim.py
venv/bin/python scripts/library/build-manuals-zim.py --source ~/pdfs --name my-guides_en_all --title "My guides"
```

It screens each file (not a PDF, encrypted, or marked for restricted
distribution → left out and listed), writes an index page, packs the ZIM into
`zim/`, refreshes the catalog and indexes the pages. Re-run it after adding
files; the old edition is replaced. An optional `sources.tsv` in the folder
(columns `category`, `filename`, `title`, `publisher`, `year`) gives the index
page proper titles.

Scanned PDFs without a text layer are counted in `aegis-library status`
(`scanned_documents`); `aegis-library index --ocr` runs Tesseract over them
(about a second per page).

Free, legitimate sources worth mining: armypubs.army.mil (current manuals
marked approved for public release), archive.org (superseded field manuals and
pre-1930 books), state wildlife agencies (trapper and hunter education manuals,
track and snake guides), university extension services (game processing, food
preservation, plant and mushroom guides), USDA Forest Service (fungi and tree
field guides), CDC / WHO / the Joint Trauma System (medical), hesperian.org
(*Where There Is No Doctor*, by chapter), fishwildlife.org (trapping best
practices), CISA (NIFOG / AUXFOG radio guides).

## Shelves, and your own files

Everything is filed as category › shelf — cookbooks by style of cooking,
history and literature by era, manuals by skill — and PDFs or e-books copied
into any folder inside the library folder are read in, filed and searched
like the rest. A search can be kept to one shelf, and novels are left out of
a factual lookup unless their shelf is asked for. All of that is in
[The library's shelves, and your own files](library-shelves.md).

## Command line

```bash
scripts/aegis-library status                        # collections, article and manual counts
scripts/aegis-library search "purify water iodine"  # what the assistant would get
scripts/aegis-library search "snare" --scope manuals --text
scripts/aegis-library read <result url> --query "…" # the excerpt for one result
scripts/aegis-library index [--ocr] [--only BOOK]   # resumable; unchanged collections are skipped
```

## How search behaves

- Kiwix requires **every** word of a pattern, so questions are reduced to
  keywords first ("how do I tell water hemlock from Queen Anne's lace" →
  `water hemlock queen anne lace`). If that finds too little, Aegis retries
  with one word dropped (any one), then with adjacent pairs, and ranks results
  found by several of those queries first.
- Exact article titles win ("Amanita phalloides" opens that article);
  encyclopedias are searched separately from the Q&A archives so forum posts
  cannot crowd them out; band / album / film pages that merely share a word are
  pushed down.
- Manual pages must carry at least half of the question's *weight* (rare words
  weigh more), otherwise they are not returned — a wrong manual page is worse
  than none.
- Long articles are not quoted from the top: the lead plus the sections that
  match the question are.

## Photo identification

Three parts, each optional except the vision model:

1. **Species classifier** — [BioCLIP 2](https://imageomics.github.io/bioclip-2/)
   (MIT) scores the photo against ~867,000 taxa and returns the best species,
   genera and families with scores. CPU only: ~10 s to load, ~0.4 s per photo.
   It runs behind llama-swap as `species-id`:

   ```bash
   mkdir -p $ENGINE_DIR/speciesid && cd $ENGINE_DIR/speciesid
   python3 -m venv venv
   venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   venv/bin/pip install pybioclip fastapi uvicorn python-multipart pillow
   cp <repo>/scripts/engine-templates/speciesid-server.py server.py
   venv/bin/python server.py --prefetch      # ~5 GB into the Hugging Face cache, once, online
   ```

   The `species-id` block is in `docs/llama-swap.linux.yaml` (cpu group).
   `scripts/setup-engine.sh` does all of this unless run with `--skip-speciesid`.
2. **Vision model** — `identify_model` if set, else `vision_model`. Reasoning is
   switched off for these calls (a local model that thinks first spends a
   minute before its first useful token). Any served model with a vision
   projector works; pick the fastest one.
3. **The library** — the leading candidates' articles supply field marks,
   lookalikes, toxicity and a reference photo.

When the classifier is confident (species score ≥ 0.5 or genus ≥ 0.85) its
candidates go straight to the write-up — one model call. Otherwise the vision
model first describes what it sees, with the candidates as hints. Either way
the answer is written from the photo plus the library notes, and is told to add
nothing from memory.

Settings: `species_id_enabled` (`true`), `species_id_model` (`species-id`),
`identify_model` (empty).

**How far to trust it.** On reference photos the classifier named the right
species for 10 of 14 subjects and the right genus for 13; the miss was a small
brown mushroom, which is what small brown mushrooms are like. The answer shows
its confidence and the classifier's scores, names dangerous lookalikes and says
what to check in hand. Use it to know what you are probably looking at and what
to verify — not as the last word before eating something.

## Troubleshooting

- **`/lib` says the library is not running** — `systemctl status kiwix`;
  `journalctl -u kiwix`. A unit with `RestrictAddressFamilies` must include
  `AF_NETLINK` or kiwix-serve exits at startup ("getifaddrs: Address family not
  supported").
- **kiwix-serve segfaults ("Snowball slice invalid")** — a thread-safety bug in
  3.8 when several full-text searches overlap. Aegis sends its searches one at a
  time and retries across a restart; the unit restarts the server in 2 s.
- **A new ZIM does not show up** — it is skipped while a `.aria2` file sits next
  to it; then run `scripts/library/rebuild-library.sh` (kiwix-serve reloads the
  catalog by itself).
- **Links open on the server but not on the phone** — set `kiwix_public_url`.
- **Identification takes a minute the first time** — the vision model is being
  loaded (and, behind llama-swap, the chat model unloaded). Later photos take
  20–40 s.
- **Identification works but shows no reference notes** — the library has no
  encyclopedia collection (`wikipedia_en_*`), or kiwix-serve is down.
