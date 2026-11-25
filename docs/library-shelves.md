# The library's shelves, and your own files

Everything in the [offline library](offline-library.md) is filed in one tree:
**category › shelf**. A cookbook is under *Cooking* by its style of cooking, a
history under *History* by its era, a novel under *Literature* by when it was
written, a manual under *Survival* by its skill. People browse the tree in the
[Reader](reader.md); the assistant uses it to know what kind of source it is
reading — and to keep a how-to question from being answered out of a novel.

```
Survival                Medical & first aid · Water · Food, foraging & preserving · Hunting, trapping & fishing ·
                        Plants · Mushrooms · Insects, snakes & pests · Shelter, fire & fieldcraft · Navigation ·
                        Communications & power · Defence & weapons · Security, escape & evasion · Disaster readiness
Medicine                Medical references · Textbooks & study · Veterinary · Old medical books
Cooking                 General cooking & technique · Baking & desserts · Preserving, canning & fermenting ·
                        Meat, fish & game · Vegetarian & vegan · Healthy & special diets · Quick & easy meals ·
                        Grilling, smoking & camp cooking · World & regional cuisines · Drinks & brewing
Homestead               Gardening · Farming & livestock · Off-grid power & building · Animals & pets · Household & everyday
Repair & making         Repair guides · Home & building · Vehicles & engines · Electronics & radio · Woodwork & crafts ·
                        Engineering & industry
Science                 Biology & nature · Chemistry · Physics & astronomy · Earth & weather · Mathematics & statistics ·
                        General science
Computing               Linux & system administration · Programming references · Hardware projects · Security & privacy
History                 Ancient world · Middle Ages · Early modern (1450-1750) · Modern (1750-1914) ·
                        World wars & 20th century · General histories · Lives & memoirs · Exploration & travel ·
                        Peoples, customs & archaeology
Literature              Ancient & classical · Medieval & Renaissance · 17th & 18th century · 19th century · 20th century ·
                        Children's & young readers · Myths, legends & folk tales · Collections & undated
Philosophy & religion   The Bible & Christianity · World religions & mythology · Philosophy & ethics ·
                        Psychology & the mind · Occult & the unexplained
Society                 Law · Government & politics · Money, business & economics · Education & parenting · Social questions
Military                Field manuals & doctrine · Technical manuals · Regulations & paperwork · Tactics, strategy & war ·
                        Ships & seamanship
Arts & leisure          Art & architecture · Music · Sports, games & hobbies · Fitness & martial arts
Reference               Encyclopedias · Dictionaries & language · Maps & travel · Source texts · Courses & study guides ·
                        Magazines & journals
```

You can add categories and shelves of your own ([below](#your-own-shelves-and-filing)).

## What gets filed, and how

| What | Filed by | Example |
|---|---|---|
| A whole **collection** (Wikipedia, a Stack Exchange site, iFixit) | its id | `cooking.stackexchange.com` → Cooking › General cooking & technique |
| Each **book inside a Project Gutenberg collection** | Gutenberg's own catalogue: subject class, categories, subjects, the author's dates | *Pride and Prejudice* → Literature › 19th century; a Roman history → History › Ancient world; a 1900s baking book → Cooking › Baking & desserts |
| Each **PDF of the curated manuals** | the section it is in, refined by its title | `medical/…` → Survival › Medical & first aid |
| Each **Army publication** | what it teaches when that is a survival skill, else what kind of publication it is | a first-aid circular → Survival › Medical & first aid; a regulation → Military › Regulations & paperwork |
| **Documents added by asking** and **your own files** | the folder, the title, the book's own subject headings — and a model for what those leave open | see below |

Nothing is moved on disk. Shelves are a catalogue (`shelf_items` in
`<library>/index/manuals.db`), rebuilt by `aegis-library shelve` in about a
second per collection. A collection the rules do not recognise is shown under
**Unsorted** rather than hidden.

Project Gutenberg books are filed from its catalogue file
(`pg_catalog.csv.gz`, about 6 MB), fetched once into `<library>/index/` the
first time a Gutenberg collection is shelved — the only step here that needs
the internet. Literature is filed by era from the author's dates (a
translation of an old work by its subject headings: *Beowulf* is medieval
whoever translated it); history by the era Gutenberg names, else by the
subject class and the dates in its subjects.

## Your own files

Copy PDFs and e-books into **any folder inside the library folder** —
`~/library/<anything>/…`, over SSH, a file share, however you like — and
take them in:

```bash
scripts/aegis-library files                 # read new files in, file them by folder and title
scripts/aegis-library files --ask-model     # …and let the local model file the ones that leaves unsure
scripts/aegis-library files --ocr           # …and read scanned PDFs with OCR (slow; run it overnight)
```

or ask the assistant to *"scan the library for new files"*, or press **Look
for new files** at the foot of the Reader's first page (administrators).

* Readable: `.pdf .epub .mobi .fb2 .txt .html`. Other files (audio, pictures,
  archives) are counted and left alone.
* Files stay exactly where you put them. Nothing is renamed, moved or copied.
* The same file under two names is read once (the copy without "(1)" in its
  name is kept). A file that later disappears is forgotten; a folder that is
  merely away (an unplugged disk) empties nothing.
* A file still being copied — changed within the last minute — waits for the
  next run. Running again costs one `stat` per file.
* Each page is searchable (`library_search`), each file opens in the Reader
  (`/api/reference/files/…` links open there; an e-book is laid out on a
  fixed page size so "page 41" always means the same place), and only files
  that were read in are ever served — to signed-in people only.
* `<library>/zim`, `index`, `manuals` and `added` are the library's own and
  are never scanned.

### How a file finds its shelf

1. **Its folder.** The nearest folder whose name says something wins:
   `Cookbooks/…` → Cooking, `Medical/…` → Survival › Medical & first aid,
   `03) Navigation, Reading Maps (9)/…` → Survival › Navigation. Plain names
   (`Books`, `Downloads`, `Pack 3`) say nothing and stop the search, so
   `survival/Books/Cookbooks pack/x.pdf` is a cookbook, not a survival manual.
2. **Its title** — for cooking, the style (*Sourdough at Home* → Baking &
   desserts); for survival, the skill.
3. **Its own subject headings**, for e-books (`Fiction / Fantasy` →
   Literature; `History / Ancient` → History › Ancient world).
4. **The local model**, for what is still unsure: unsorted, only a category
   known, a cookbook of no clear style, a story of unknown date. It is shown
   the file name, folder, metadata and the first page or two, and answers
   with a title, author, year, one sentence about it and a shelf *from the
   list*. An answer that names no real shelf is ignored. This runs on the
   machine's own model; nothing leaves it.

A file named by a reference code (`ADA267521.pdf`, `DOC-0041988-A.pdf`) gets
its title from its first lines, with the code kept in brackets so it can
still be searched for.

### Your own shelves and filing

`<library>/shelves.json` (optional, yours — it is not part of the app):

```json
{
  "categories": [
    {"name": "Family papers", "about": "Ours, not the world's",
     "shelves": [{"name": "Letters", "about": "Kept correspondence"}, {"name": "Deeds & records"}]},
    {"name": "Cooking", "shelves": ["Family recipes"]}
  ],
  "folders": {
    "papers/Letters": "Family papers/Letters",
    "books/Technical": "Repair & making",
    "books/Packs/Illustrated*": "Reference"
  },
  "collections": {"some-collection_en_all": "Science/General science"}
}
```

* `categories` adds categories, or shelves to a built-in category.
* `folders` says what a folder is, by its path inside the library (a trailing
  `*` matches the rest of a folder's name). A nearer folder with a telling
  name still wins over a mapped folder above it.
* `collections` files a whole collection somewhere else.
* Anything moved by hand (below) is remembered here under `items`, so a
  rebuild keeps it.
* `min_rating`, `keep_per_shelf` and `keep_whole` set the cut described under
  [Keeping the better-read books](#keeping-the-better-read-books).

To move one book or document: ask the assistant (*"put Field Notes under
Survival › Navigation"*), which calls `manage_library` `move`. What a person
filed, no rule and no model changes afterwards.

## Keeping the better-read books

Project Gutenberg comes by the tens of thousands, and much of it is filler:
single chapters filed as books, magazine issues, part-volumes, forgotten
serials. A cut keeps the shelves to what is worth having. In
`<library>/shelves.json`:

```json
{"min_rating": 880, "keep_per_shelf": 25}
```

* `min_rating` — keep Gutenberg books at least this well read. The figure is
  Project Gutenberg's own popularity number (its download counts), read from
  each collection when it is shelved. On the 2026-11 editions 880 keeps about
  the top fifth; 1,200 the top tenth; 600 the top half. `0`, or leaving it
  out, keeps everything.
* `keep_per_shelf` — whatever the cut, the best-read N of every shelf stay
  (default 25), so no shelf is emptied.
* `keep_whole` — categories the cut never touches. The default is the
  practical ones: `["Survival", "Cooking", "Homestead", "Repair & making"]` —
  an unread canning manual is still a canning manual.
* Always kept: every book Gutenberg tags a classic, anything a person or a
  model filed, a book with no figure, and everything that is not a Gutenberg
  book (the manuals, the collections, your own files).

Run `scripts/aegis-library shelve` after changing it (a second or two).

**Nothing is deleted.** A Gutenberg collection is one archive; a book cannot
be taken out of it. A book below the cut is left off the shelves, is not
found by title or author, and its text is not searched — and it comes back
the moment the cut is lowered. Each shelf says how many it leaves out
(*"1,306 less-read books are below your cut — Show them"* in the Reader;
`"all": true` for the assistant's `library_browse`; `--all` on the command
line), and a book asked for by name that is only below the cut is reported
as such rather than as missing.

## Searching by shelf

Every result of `library_search` says which shelf it is on, and for a book
who wrote it and when:

```
[1] Fundamental Principles of the Metaphysic of Morals
    Source: Project Gutenberg Library (book) — by Immanuel Kant, 1724–1804
    Shelf: Philosophy & religion › Philosophy & ethics
```

* `"shelf": "Cooking"` (a category) or `"shelf": "History/Ancient world"` (a
  shelf) keeps a search there. Names are read loosely: `cookbooks`,
  `history > ancient`, `first aid`.
* **Novels, poetry and plays are searched only when the shelf is Literature**
  (or one of its shelves). Left to itself the search covers everything else —
  a question about treating a snake bite is not answered from an adventure
  story. The results say so, and how to include them.
* A hit inside a Gutenberg book is quoted from the book's own text, not from
  its catalogue card.
* A book with an author's dates, or anything on an "Old …" shelf, is a
  historical text; the assistant is told to say when it is from and not to
  pass its medical or safety advice off as current.

Command line: `scripts/aegis-library search "sourdough starter" --shelf Cooking`.

## Browsing

* **Reader** (`/read`): *The library, by shelf* lists the categories; a
  category lists its shelves; a shelf lists its collections, books and
  documents, with a box to find by title or author and a search that stays
  inside the shelf. A search from the first page shows *books and documents
  by that name* above the passages found in the texts. *By collection* is the
  old flat list.
* **Assistant**: `library_browse` — no arguments for the tree, `shelf` for
  what is on one, `find` for a book or document by title or author
  (*"do we have Moby Dick?"*, *"anything by Dickens?"*, *"show me the
  cookbooks"*). `library_search` finds what the texts *say*; `library_browse`
  what the library *holds*.
* **Command line**:

  ```bash
  scripts/aegis-library shelves                          # the tree with counts
  scripts/aegis-library shelves "History/Ancient world"  # what is on a shelf
  scripts/aegis-library shelves --find "moby dick"       # by title or author
  scripts/aegis-library shelves "History" --all          # including what the cut leaves out
  scripts/aegis-library shelve                           # (re)file everything; --fetch-catalogue to refresh Gutenberg's
  ```

`aegis-library index` (run by `fetch-zims.sh` after downloads) refreshes the
shelves when it finishes, so a newly downloaded collection is shelved in the
same run.

## Downloading the classics

Kiwix publishes Project Gutenberg split by subject class, so the classics
come as separate collections (sizes of the 2026-11 editions):

| Collection | What | Size |
|---|---|---|
| `gutenberg_en_lcc-b` | Philosophy, psychology, religion — the Bible, the Church fathers, Plato to Nietzsche | 5.4 GB |
| `gutenberg_en_lcc-pa` `-pq` `-pt` `-pg` | Greek and Latin; French, Italian, Spanish; German and Scandinavian; Russian | 4.2 GB |
| `gutenberg_en_lcc-pr` `-ps` | English and American literature | 24.6 GB |
| `gutenberg_en_lcc-pn` `-pj` `-pk` `-pl` | Literature in general; Near Eastern, Indian, East Asian | 3.5 GB |
| `gutenberg_en_lcc-cdef` | History: world, European, British, American; biography; archaeology | 46.9 GB |
| `gutenberg_en_lcc-pz` | Fiction and children's books | 14.5 GB |

```bash
scripts/aegis-library catalog gutenberg_en_lcc --urls >> ~/library/zim-list.txt   # then delete the lines you do not want
AEGIS_LIBRARY_MIN_FREE_GB=100 setsid nohup ~/library/fetch-zims.sh >> ~/library/zim-download.log 2>&1 &
```

`fetch-zims.sh` leaves a collection for later when taking it would leave less
than `AEGIS_LIBRARY_MIN_FREE_GB` (default 60) free on the library's disk.
Everything on Project Gutenberg is in the public domain in the United States,
which in practice means written before about 1930.

## Limits worth knowing

* A scanned PDF has no text until it is read with OCR (`files --ocr`); until
  then it is on its shelf and opens as page pictures, but a search cannot see
  inside it.
* Shelves for whole collections are rules by id; a brand-new kind of
  collection shows under **Unsorted** until a rule or `shelves.json` places it.
* Literature by era follows the catalogue's author. A translation whose
  record names only the translator, with no telling subject, lands in
  *Collections & undated*.
* Audiobooks and other sound files are not catalogued.
