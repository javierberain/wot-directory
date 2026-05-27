# The Pattern — A Wheel of Time Character Directory

A personal tool that turns Wheel of Time EPUBs into a searchable
character directory: per-character profiles, per-chapter cast lists,
and an interactive relationship graph. You feed it one chapter at a
time and the directory grows.

## How it works

```
   EPUB  ──parse_epub.py──▶  chapters table
                                  │
                          extract_chapter.py   (calls Claude API)
                                  │
                          data/extractions/*.json   ◀── you inspect this
                                  │
                          reconcile.py   (matches names, commits)
                                  │
      characters · aliases · appearances · relationships · factions
                                  │
                              app.py  ──▶  web UI
```

The design separates **extraction** (the LLM reads a chapter and
proposes structured data) from **reconciliation** (names are matched
against the existing roster and committed). That separation lets you
check the LLM's output before it touches your database.

## Data model

| Table                | Holds                                                         |
|----------------------|---------------------------------------------------------------|
| `books`              | one row per book                                              |
| `chapters`           | one row per chapter, including the raw text                   |
| `characters`         | stable traits — `character_type` (species), nationality, age, filiations, `personality`, etc. |
| `aliases`            | every name a character is known by, **typed**                 |
| `appearances`        | one row per character per chapter — chapter-specific facts including `demeanor` |
| `relationships`      | character-to-character edges for the network graph (includes `warder_bond`) |
| `factions`           | Ajahs, orders, houses, clans, societies — typed groups        |
| `character_factions` | join: who belongs to which faction, with `role` (member/leader) |
| `review_queue`       | extraction items the pipeline was unsure about                |

The `aliases` table is what handles the "Mat" vs "Matrim Cauthon"
problem and the disguise problem. Each alias has an `alias_type`:
`given_name`, `title`, `nickname`, `disguise`, or `epithet`. A name a
character uses to hide their identity is stored as a `disguise` and is
shown differently in the UI.

### Species, personality, demeanor

- **`characters.character_type`** is the species (`human`, `ogier`,
  `trolloc`, `myrddraal`, `horse`, `wolf`, `other`). Default `human`.
  This used to be wrongly stuffed into the free-text `nationality`
  field, which now only ever holds an in-world human nationality
  (Andoran, Cairhienin, Aiel, Two Rivers…). See Design Decisions for
  why this exact set of values matters and where it must be kept in sync.
- **`characters.personality`** is a stable disposition — Mat as
  reluctant and gambling-prone, Nynaeve as stubborn. Enriched the same
  way as other stable traits: filled when empty, never overwritten.
- **`appearances.demeanor`** is how the character presents or behaves
  in that specific chapter (drunk, terrified, jubilant). WoT characters
  evolve, so a static blurb goes stale; this captures the snapshot.

### Factions

Ajah membership and similar group memberships used to live in the
free-text `associations` column on `characters`. They are now
first-class rows:

- **`factions`**: `(faction_id, name, name_norm, faction_type,
  description)`. `faction_type` is one of `ajah`, `order`, `house`,
  `clan`, `society`, `other`. `name_norm` is the lower-cased lookup
  key, mirroring `aliases.alias_norm`.
- **`character_factions`**: `(character_id, faction_id, role,
  first_chapter_id, notes)`. The reconciler matches factions
  generously by normalised name, creating new rows on miss — the
  same spirit as the character reconciliation.

`characters.associations` is kept for backward compatibility but is
no longer written. Reach for the `factions` tables instead.

### Warder bond

The Aes Sedai / Warder bond is a *character-to-character* link, not a
faction, so it lives in `relationships` with `relationship_type =
'warder_bond'`. It is a directed edge: `character_a` is the Aes Sedai
who holds the bond, `character_b` is the Warder bonded to her. A
single Aes Sedai can have several Warders — each pair is its own row.

## Design Decisions

### Character types

The valid `character_type` values are exactly: `human`, `ogier`,
`trolloc`, `myrddraal`, `horse`, `wolf`, `other`. `horse` and `wolf`
are explicit types rather than falling under `other` because they are
not incidental to the series: horses like Bela recur as named
individuals across all fourteen books, and wolves are central to
Perrin's arc in ways that warrant distinct classification. All other
named non-human creatures fall under `other`.

An earlier `creature_collective` type (intended for groups like
"Trollocs") and a general `animal` type were both considered and
deliberately removed. Groups are not characters (see below), so
`creature_collective` had no valid use; `animal` was too broad to carry
meaning. The remaining seven values reflect what the series actually
needs.

**These values must stay identical in three places:** the
`character_type` field description in the system prompt in
`extract_chapter.py`, the `VALID_CHAR_TYPES` set in `reconcile.py`,
and the `CHECK` constraint in `db/schema.sql`. A mismatch between the
prompt and the validator causes the LLM to emit a value that the
reconciler silently coerces to `other`, losing data without any error.
A mismatch between the validator and the schema raises a constraint
violation at commit time and aborts the reconcile run. Update all three
together, or not at all.

### Groups are never characters

A group, army, organization, or collective — human or non-human — is
never given a character row. The reason is practical: a character row
implies an individual that can have aliases, relationships, and be
matched across chapters; none of that applies cleanly to a collective.

Human organizations (the Children of the Light, an Ajah, a noble
House) are modelled as **factions** on the individual members who
belong to them. An unnamed creature mass (a horde of Trollocs attacking
a farm) is treated as an **event**: it is not given any row, and its
actions are described in the `notable_actions` and `demeanor` fields of
the named characters present during it. Only named, individually-acting
beings get character rows.

### Aliases must be identifying

An alias is a name or named title that picks out a specific individual:
a given name, surname, formal name, unique epithet ("the Dragon
Reborn"), or a title held by one person at a time ("the Amyrlin Seat").
Generic forms of address that could apply to anyone — `boy`, `lad`,
`girl`, `sister`, `my lord`, `mistress`, "the gleeman" used as a role
rather than a name — are not recorded as aliases.

The rule exists because aliases feed the matching index used by the
reconciler. A generic role-noun recorded as an alias pollutes that index
and creates false merge targets: a later character referred to by the
same role could incorrectly match against any character carrying it.

### Identity matching and the review queue

The pipeline has two distinct phases: **extraction** (Claude reads a
chapter and proposes characters, including a `likely_matches_existing`
pointer for characters it believes are already in the roster) and
**reconciliation** (the reconciler matches those proposals against the
database and commits them).

The reconciler does not trust the LLM's pointer blindly. Before
accepting a pointer match it computes the best `difflib.SequenceMatcher`
ratio between the incoming `name_used_in_text` and every known alias of
the pointed-at character. If that ratio falls below `POINTER_THRESHOLD`
(0.6), the match is rejected and the item is sent to the review queue
with kind `suspicious_llm_match`, recording the incoming name, the
rejected target's primary name, and the similarity score. This catches
cases where the model points a name at a completely unrelated roster
entry.

The review queue is a **human worklist**, not an error log. Items in it
are not committed to the directory tables; they wait until a person
looks at them and either merges the character manually, adds a missing
alias, or confirms the character is genuinely new. Resolving an item
means editing the database directly and setting `resolved = 1`.

### Book one identity rulings

These are examples of the kind of judgment the review queue exists to
surface, recorded here so the reasoning is not lost:

- **"the gleeman" → Thom Merrilin.** Early chapters referred to Thom
  only by his role before naming him. The role-noun was incorrectly
  used as the primary name and a duplicate character row was created;
  the duplicate was merged into Thom Merrilin and "the gleeman" was
  moved to his aliases with type `epithet`.
- **Ba'alzamon / Ishamael → Elan Morin Tedronai.** Ba'alzamon is the
  name used in-text for the figure who appears in visions and
  confrontations; Ishamael is a second alias. Both were pointing at the
  same person, whose true name is Elan Morin Tedronai. All three names
  were merged into a single character row.
- **The Dark One stays separate.** "The Dark One" (aliases: Shai'tan,
  the Great Lord of the Dark, etc.) is a distinct character from
  Ishamael / Elan Morin Tedronai, even though book one's text
  sometimes conflates them in characters' perceptions. They were
  deliberately kept as separate rows because they are separate entities
  in series canon. Collapsing them would corrupt every later book.

## Setup

```bash
pip install -r requirements.txt
```

The `static/cytoscape.min.js` file is vendored so the graph works
without a CDN. Nothing else needs downloading.

### Migrations

Schema changes are tracked as numbered files under `db/migrations/`.
A fresh database created from `db/schema.sql` already includes every
migration; existing databases need each migration run in order:

```bash
sqlite3 db/wot.db < db/migrations/001_species_personality_factions.sql
```

Each migration is one-shot — SQLite has no `ADD COLUMN IF NOT EXISTS`,
so re-running a file errors on the ALTER statements. That is the
intended signal that the migration has already been applied.

## Usage

### 1. Parse a book

```bash
python scripts/parse_epub.py "The Eye of the World.epub" \
    --order 1 --title "The Eye of the World"
```

This creates `db/wot.db` on first run and loads every chapter. The
parser is tuned to the EPUB layout where chapters are split across
`index_split_NNN.html` files and a `toc.ncx` navMap marks where each
chapter begins. If a future book's EPUB is laid out differently, the
parser may need adjusting — check the chapter list it prints.

### 2. Extract characters from a chapter

```bash
export ANTHROPIC_API_KEY=sk-...
python scripts/extract_chapter.py --book 1 --chapter 4
```

This calls the Claude API. It feeds the current character roster into
the prompt so the model can match against characters already known.
The result is written to `data/extractions/b1_c4.json`. It does **not**
write to the directory tables yet — open that JSON and check it.

### 3. Reconcile (commit) the extraction

```bash
python scripts/reconcile.py --book 1 --chapter 4
```

This matches each extracted character against the roster:
exact alias match, then the LLM's own pointer, then a fuzzy match as a
safety net. Confident items are committed; anything ambiguous goes to
the review queue instead of being guessed. Add `--auto` to commit
high-confidence items without pausing.

Check the review queue any time:

```bash
python scripts/reconcile.py --review
```

### 4. Or do a whole book at once

```bash
python scripts/run_book.py --book 1            # pauses per chapter
python scripts/run_book.py --book 1 --auto     # runs straight through
```

It walks chapters in order, so the roster grows naturally and later
chapters benefit from characters found earlier.

### 5. Run the web app

```bash
python app.py
# open http://127.0.0.1:5000
```

Three views: **Profile** (search any name or alias, see traits,
personality, faction memberships, per-chapter appearances with
demeanor, and relationships), **Chapter Cast** (pick a book and
chapter, see who features in it), and **The Web** (the relationship
graph, filterable to a single chapter's cast, with a colour-by toggle
for default / faction / species).

Clicking a faction chip on a profile opens that faction's roster
(also available at `GET /api/faction/<id>`).

## Notes on accuracy

- Identity matching improves as you ingest more chapters. Early on the
  roster is thin, so the LLM has less to anchor against. By a few
  chapters in it is matching reliably.
- The `review_queue` is where you catch mistakes. A character split in
  two, or two merged into one, shows up there as a `possible_duplicate`
  or `ambiguous_character`. Resolve those by editing the database.
- Re-running extraction on a chapter overwrites its JSON file but does
  not duplicate appearances — `reconcile.py` uses `INSERT OR REPLACE`.

## Review Queue Cleanup

After running a full book, work the queue in two passes.

**Pass 1 — bulk fixes.** Look for characters that were bounced into the
queue from many chapters in a row — this is almost always the same
person that the reconciler failed to match because of a missing alias or
a bad primary name. Fix those first: merge the duplicate character rows
in the database, add the missing aliases, then re-reconcile every
affected chapter by running `reconcile.py --book N --chapter M` again.
Re-reconciling a chapter costs no API call — it replays the saved JSON
from `data/extractions/` against the updated roster. The reconciler uses
`INSERT OR REPLACE` throughout, so appearances and relationships that
were already committed correctly are not duplicated, and appearances that
were previously dropped because the character was unresolved are now
written in.

**Pass 2 — individual judgments.** Handle the remaining one-off items:
`suspicious_llm_match` entries, low-confidence new characters,
genuinely ambiguous identities. Each requires a human decision: merge
into an existing character, confirm as new, or discard. Set
`resolved = 1` when done. Running `reconcile.py --review` lists
everything still open.

## Database Maintenance Tools

Two scripts handle post-ingestion cleanup. Run them after finishing a book's
worth of reconciliation and review-queue work.

### hygiene_audit.py

A strictly **read-only** auditor. Opens the database with SQLite's `mode=ro`
URI flag so any write attempt raises immediately at the driver level. It never
modifies anything — all findings are printed for human review.

```bash
python scripts/hygiene_audit.py              # run all three checks
python scripts/hygiene_audit.py --with-llm  # add Claude advisory verdicts
python scripts/hygiene_audit.py --detail 42 # full dossier for character_id 42
```

**What it checks:**

- **Check A — generic aliases.** Aliases whose entire normalised text is a
  generic form of address (`sister`, `my lord`, `the innkeeper`, etc.).
  These pollute the reconciler's matching index and should be removed.
- **Check B1 — title/group primary names.** Characters whose `primary_name`
  starts with `the` and is not on the allow-list (e.g. `the Creator`,
  `the Dark One`). Often a formal title or group name that ended up as a
  character row.
- **Check B2 — descriptor placeholders.** Primary names that look like
  extractor-invented stand-ins for unnamed walk-ons: names ending in person-
  nouns (`the weaselly man`, `a serving woman`) or matching a role-noun list.
- **Check C — non-individual rows.** Trolloc/Myrddraal rows whose name
  contains the species word or starts with a bare article, indicating a
  collective or generic creature label rather than a named individual.

A **LIKELY RENAMES** sub-section follows Check B1: any B1 character with more
than two appearances is almost certainly a real character with a wrong
`primary_name` rather than a row to delete.

The `--with-llm` flag sends each ambiguous row (B2 and B1-with-appearances)
to the Claude API with the character's first chapter text and asks for a
`KEEP / REMOVE / UNCERTAIN` verdict. Advisory only — the script still writes
nothing.

The `--detail <character_id>` flag prints a complete pre-deletion dossier for
one character: every column of the characters row, all aliases, all
appearances with book/chapter context, all relationships showing the other
party by name, and all faction memberships. Use this before deciding to delete
a row.

The editable wordlists (`GENERIC_ALIAS_EXACT`, `PLACEHOLDER_TAIL_WORDS`,
`ROLE_NOUN_EXACT`) and the `PRIMARY_NAME_ALLOWLIST` are grouped at the top of
the script for easy tuning between book runs.

### delete_characters.py

A **report-then-confirm** deletion tool. It never deletes anything without
first printing exactly what it will remove and waiting for you to type the
word `DELETE`. There is no `--auto` or `--force` mode.

```bash
python scripts/delete_characters.py
```

The target list is hard-coded as `TARGET_IDS` near the top of the file —
18 confirmed non-character rows (generic creatures, unnamed crowd-scene
placeholders) approved for removal. To add ids to that list, run
`hygiene_audit.py --detail <id>` first and verify the character has no
legitimate dependent data.

**What it does, in order:**

1. Writes a backup to `db/wot.db.pre-deletions-auto.bak` (including WAL
   sidecar files if present) before touching anything.
2. Prints a full dossier for each target: the characters row, every aliases
   row, every appearances row (with book/chapter label), every relationships
   row (showing the other character's id and name so you can see which real
   characters lose an edge), and every character_factions row.
3. Prints a summary: total characters, aliases, appearances, relationships,
   and character_factions rows to be deleted.
4. Prompts for confirmation. You must type `DELETE` exactly. Anything else —
   or a non-interactive stdin — aborts with no changes made.
5. On confirmation, deletes all rows inside a **single transaction** in
   foreign-key-safe order: `character_factions` → `appearances` →
   `relationships` → `aliases` → `characters`. Commits once at the end. Any
   error rolls back the entire transaction.
6. Re-queries to confirm all target ids are gone, then prints a final
   row-count per table.

`PRAGMA foreign_keys = ON` is set on the connection before any statement
runs. The script only ever touches the character_ids in `TARGET_IDS` and
their direct dependents — no other rows are read for modification.

## Adding the next book

```bash
python scripts/parse_epub.py "The Great Hunt.epub" --order 2 \
    --title "The Great Hunt"
python scripts/run_book.py --book 2 --auto
```

The roster carries across books automatically, since extraction always
reads every character already in the database.

## Known Limitations / Future Work

- **Long API outages can still stop a run.** The extraction script
  retries transient errors with exponential backoff (two layers: the
  SDK's built-in retry and an explicit outer loop), but a sustained
  outage will eventually exhaust all attempts and exit. The run is
  fully resumable: chapters that already have a JSON file in
  `data/extractions/` can be reconciled immediately with no API call;
  only the remaining un-extracted chapters need re-running.
- **The roster prompt grows with each book.** Every extraction call
  sends the full character roster to the model. Across fourteen books
  this will become very large and expensive. The current approach is
  adequate for books one and two; beyond that, a relevance-based subset
  (characters seen in recent chapters, or characters confirmed to appear
  in the current book) will be needed instead of the full roster.
- **Deployment target is Amazon Lightsail.** Lightsail was chosen
  because it provides a persistent disk, which lets SQLite work without
  any modification to the data layer. A serverless or ephemeral host
  would require migrating to an external database service and reworking
  the connection and migration machinery.
