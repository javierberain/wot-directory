#!/usr/bin/env python3
"""
extract_chapter.py - Run one chapter through the Claude API and get back
structured character / appearance / relationship data as JSON.

This script does NOT write to the characters/appearances/relationships
tables. It only reads the chapter text and the current roster, calls the
API, and writes the result to data/extractions/. A separate reconcile
step reviews and commits that JSON. Keeping extraction and commit apart
means you can inspect the LLM output before it touches your data.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python extract_chapter.py --book 1 --chapter 4
    python extract_chapter.py --book 1 --chapter 4 --print   # show JSON

Requires: anthropic  (pip install anthropic)
"""
import argparse
import json
import os
import sqlite3
import sys
import time

import anthropic

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wot.db")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "extractions")
MODEL = "claude-sonnet-4-5"   # capable + cost-sensible for per-chapter runs

# Retry configuration
# Layer 1 – SDK built-in retries: handles brief blips (seconds).
# Layer 2 – explicit outer loop below: handles longer outages (minutes).
_SDK_MAX_RETRIES     = 5    # passed to anthropic.Anthropic(max_retries=...)
_API_MAX_ATTEMPTS    = 5    # outer loop attempts before giving up
_RETRY_BASE_DELAY    = 5    # seconds; first wait after a failure
_RETRY_MAX_DELAY     = 60   # seconds; backoff ceiling
_RETRYABLE_STATUSES  = {429, 529}  # rate-limit and overloaded; others fail fast

# ---------------------------------------------------------------
# The instruction block. The roster is injected at call time.
# ---------------------------------------------------------------
SYSTEM_PROMPT = """\
You are a literary analysis tool building a character directory for the
Wheel of Time series. You will be given the full text of ONE chapter and
a roster of characters already known from earlier chapters.

Your job: extract structured data about the characters in THIS chapter.

CRITICAL RULES ON IDENTITY:
- The roster lists characters we already know, each with all the names
  and aliases they are known by. When a character in this chapter matches
  a roster entry, set "is_new_character" to false and put the roster's
  primary name in "likely_matches_existing".
- Match generously across name variants. "Mat", "Matrim", "Matrim
  Cauthon", "Mat Cauthon" are the SAME person. A full or formal version
  of a name is not a new character.
- Only set "is_new_character" to true when the character genuinely does
  not appear on the roster under any name.
- Some characters use a FALSE NAME to disguise themselves. If the text
  shows a character deliberately using an alias to hide their identity,
  record that alias with alias_type "disguise" and explain in notes.
  Distinguish this from ordinary nicknames, titles, and epithets.
- If you are unsure whether a name is a known character or a new one,
  set "confidence" to "low" and explain your reasoning in "notes".

NAME_USED_IN_TEXT MUST BE A PROPER NAME:
- "name_used_in_text" must be the character's actual proper name (a given
  name, surname, or full name) whenever the chapter text supplies one,
  even if the text mostly refers to them by a role or description.
  Scan the entire chapter before filling this field; a name that appears
  once, late in the chapter, is still available.
- A pure role-noun such as "the gleeman", "the peddler", "the Wisdom",
  "the innkeeper", or "the stranger" may be used as name_used_in_text ONLY
  as a genuine last resort when the chapter genuinely never mentions a
  proper name for that character anywhere.
- When a proper name is available AND the text also uses a role-noun, put
  the proper name in name_used_in_text and put the role-noun in
  aliases_observed with alias_type "epithet" (or record it in notes).
  Never let the role-noun become the primary name when a proper name exists.
- If a character is referred to only by role or description in this chapter
  but you believe from context that they are a known roster character, set
  likely_matches_existing to the roster's primary name and is_new_character
  to false. Use the roster's primary name (not the role-noun) as
  name_used_in_text in that case.

ALIAS TYPES: given_name, title, nickname, disguise, epithet.
An alias must be a NAME or NAMED TITLE that specifically 
identifies this individual. Record a name only if it actually
picks out this person: a given name, surname, formal name, 
a unique epithet ("the Dragon Reborn", "Lord of the Morning"), 
a held title tied to them ("the Amyrlin Seat", "Lord Captain 
Commander"), or a false name used as a disguise. Do NOT record generic 
forms of address that could apply to anyone: "boy", "lad", "girl", 
"child", "sister", "daughter", "my lord", "mistress", "stranger". 
A word the text uses to address or describe a category of person 
is not an alias. When in doubt, ask: does this word identify THIS 
person specifically, or could it refer to many people? If many, exclude it.

CHARACTER TYPE (species, not nationality):
- "character_type" is the species of an INDIVIDUAL named or
  individually-acting being: human, ogier, trolloc, myrddraal, horse,
  wolf, or other.
- Use "other" for a named non-human creature that is not a horse,
  wolf, trolloc, myrddraal, or ogier.
- Nationality (Andoran, Cairhienin, Aiel, Two Rivers...) is a SEPARATE
  field and only applies to humans. Do NOT put species in nationality.

WHAT IS NOT A CHARACTER:
- A group, mass, army, or collective is NEVER a character, whether it
  is human or not. Do not create a character object for "Trollocs",
  "the Myrddraal", "the Children of the Light", an Ajah, a noble House,
  a village, or a clan.
- An unnamed creature mass (e.g. "Trollocs attacked the farm") is an
  EVENT, not an entity. Do not give it a row anywhere. Describe what it
  did inside the appearances of the named characters who were present
  (in their notable_actions and demeanor).
- A persistent, organized group that matters across the story (the
  Shadow, the Children of the Light, an Ajah, a noble House) is a
  FACTION. Record it only in the "factions" field of the individual
  named characters who belong to it. Never as a character.
- If you find yourself writing a note explaining why a group is in the
  characters list, that is the signal to remove it from the list.

NAMED INDIVIDUAL CREATURES:
- A creature that is named or acts as a distinct individual IS a
  character. Narg, an individual Trolloc who acts in a scene, gets a
  row with character_type "trolloc". A single Myrddraal that appears
  and acts gets "myrddraal". Bela the horse gets "horse".
- These individual creatures MAY appear in the "relationships" block
  (Bela has an owner; Narg fought Rand). Only groups are excluded from
  relationships, and groups are not characters in the first place.

FACTIONS (Ajahs, orders, houses, clans, societies):
- Membership in groups like the Red Ajah, Children of the Light,
  House Trakand, the Aiel clan Taardad, the Aiel society Far Dareis
  Mai goes in "factions", NOT in "associations" free text.
- For each faction, give a name, a faction_type from {ajah, order,
  house, clan, society, other}, a role ("member" or "leader" — leader
  for Amyrlin, Captain-General, clan chief, etc.), and short notes.
- A character can belong to several factions.

WARDER BOND:
- The Aes Sedai / Warder bond is a character-to-character link, not a
  faction. Record it in "relationships" with relationship_type
  "warder_bond", directed=true, character_a = the Aes Sedai (the one
  who holds the bond), character_b = the Warder (the one bonded).
- A single Aes Sedai can have several Warders; emit one relationship
  row per bonded pair.

PERSONALITY vs DEMEANOR:
- "personality" (under stable_traits) is the character's lasting
  disposition — e.g. Mat as reluctant and gambling-prone, Nynaeve as
  stubborn and tugging her braid. Things that travel with them.
- "demeanor" (under appearances) is how they present or behave in THIS
  chapter — drunk, terrified, cold, jubilant. WoT characters evolve, so
  the per-chapter snapshot matters as much as the standing portrait.

Only include characters who actually appear or act in the chapter, not
every name merely mentioned in passing. Minor unnamed figures (e.g.
"a guard") should be skipped.

Respond with ONLY a JSON object, no preamble and no markdown fences.
Use exactly this shape:

{
  "characters": [
    {
      "name_used_in_text": "string - the main name the text used",
      "likely_matches_existing": "roster primary name, or null if new",
      "is_new_character": true | false,
      "confidence": "high" | "medium" | "low",
      "character_type": "human|ogier|trolloc|myrddraal|horse|wolf|other",
      "stable_traits": {
        "nationality": "string or null (humans only; do not put species here)",
        "physical_traits": "string or null",
        "age": "string or null",
        "filiations": "string or null",
        "personality": "string or null - stable disposition"
      },
      "factions": [
        {
          "name": "e.g. Red Ajah, Children of the Light, House Trakand",
          "faction_type": "ajah|order|house|clan|society|other",
          "role": "member|leader",
          "notes": "string or null"
        }
      ],
      "aliases_observed": [
        {"alias_text": "string", "alias_type": "given_name|title|nickname|disguise|epithet", "notes": "string or null"}
      ],
      "notes": "string or null - anything ambiguous worth a human check"
    }
  ],
  "appearances": [
    {
      "character": "must match a name_used_in_text above",
      "whereabouts": "where this character is during the chapter",
      "notable_actions": "what this character does in the chapter",
      "demeanor": "how they present/behave in this chapter, or null",
      "alliances_shown": ["string", ...]
    }
  ],
  "relationships": [
    {
      "character_a": "name matching a character above",
      "character_b": "name matching a character above",
      "relationship_type": "ally|enemy|family|mentor|romantic|rival|servant|warder_bond|other",
      "directed": true | false,
      "description": "short description of the relationship as shown"
    }
  ]
}
"""


def get_roster(conn):
    """Return the known characters with all their aliases, for the prompt."""
    rows = conn.execute("""
        SELECT c.character_id, c.primary_name, c.character_type,
               c.nationality
        FROM characters c ORDER BY c.primary_name
    """).fetchall()
    roster = []
    for cid, pname, ctype, nat in rows:
        aliases = conn.execute(
            "SELECT alias_text, alias_type FROM aliases "
            "WHERE character_id = ? AND is_primary = 0",
            (cid,),
        ).fetchall()
        alias_str = ", ".join(f"{a} ({t})" for a, t in aliases) or "none"
        factions = conn.execute(
            "SELECT f.name FROM character_factions cf "
            "JOIN factions f ON f.faction_id = cf.faction_id "
            "WHERE cf.character_id = ?",
            (cid,),
        ).fetchall()
        fac_str = ", ".join(f for (f,) in factions) or "none"
        roster.append(
            f"- {pname}"
            f" | type: {ctype or 'human'}"
            f" | nationality: {nat or 'unknown'}"
            f" | factions: {fac_str}"
            f" | also known as: {alias_str}"
        )
    return "\n".join(roster) if roster else "(roster is empty - this is the first chapter)"


def get_chapter(conn, book_order, chapter_number):
    row = conn.execute("""
        SELECT ch.chapter_id, ch.title, ch.full_text, ch.extracted, b.title
        FROM chapters ch
        JOIN books b ON b.book_id = ch.book_id
        WHERE b.series_order = ? AND ch.chapter_number = ?
    """, (book_order, chapter_number)).fetchone()
    return row


def extract(book_order, chapter_number, do_print=False):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set the ANTHROPIC_API_KEY environment variable first.")

    conn = sqlite3.connect(DB_PATH)
    row = get_chapter(conn, book_order, chapter_number)
    if not row:
        sys.exit(f"Chapter not found: book {book_order}, chapter {chapter_number}")
    chapter_id, ch_title, full_text, extracted, book_title = row
    if extracted:
        print(f"Note: chapter already marked extracted. Re-running anyway.")

    roster = get_roster(conn)

    user_msg = (
        f"BOOK: {book_title}\n"
        f"CHAPTER {chapter_number}: {ch_title}\n\n"
        f"=== KNOWN CHARACTER ROSTER ===\n{roster}\n\n"
        f"=== CHAPTER TEXT ===\n{full_text}"
    )

    client = anthropic.Anthropic(max_retries=_SDK_MAX_RETRIES)
    print(f"Calling {MODEL} for book {book_order} ch {chapter_number} "
          f"'{ch_title}' ({len(full_text.split())} words)...")

    last_exc = None
    for attempt in range(1, _API_MAX_ATTEMPTS + 1):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=18000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            break  # success — exit retry loop
        except anthropic.APIStatusError as exc:
            if exc.status_code not in _RETRYABLE_STATUSES:
                raise  # 401 auth, 400 bad request, etc. — not transient
            last_exc = exc
            label = (f"HTTP {exc.status_code} "
                     f"({'overloaded' if exc.status_code == 529 else 'rate limited'})")
        except anthropic.APIConnectionError as exc:
            last_exc = exc
            label = "connection error"

        if attempt == _API_MAX_ATTEMPTS:
            raise last_exc  # all attempts exhausted; propagate to caller

        delay = min(_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _RETRY_MAX_DELAY)
        print(f"  API {label}, attempt {attempt} of {_API_MAX_ATTEMPTS}, "
              f"waiting {delay}s before retry...")
        time.sleep(delay)

    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Strip accidental code fences if the model adds them.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        bad = os.path.join(OUT_DIR, f"b{book_order}_c{chapter_number}_RAW.txt")
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(bad, "w", encoding="utf-8") as f:
            f.write(raw)
        sys.exit(f"Model did not return valid JSON. Raw saved to {bad}\n{e}")

    # Attach identifiers for the reconcile step.
    data["_meta"] = {
        "book_order": book_order,
        "chapter_number": chapter_number,
        "chapter_id": chapter_id,
        "chapter_title": ch_title,
        "model": MODEL,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"b{book_order}_c{chapter_number}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    chars = data.get("characters", [])
    new = sum(1 for c in chars if c.get("is_new_character"))
    print(f"  characters: {len(chars)}  (new: {new})")
    print(f"  appearances: {len(data.get('appearances', []))}")
    print(f"  relationships: {len(data.get('relationships', []))}")
    print(f"  tokens in/out: {resp.usage.input_tokens}/{resp.usage.output_tokens}")
    print(f"  saved to: {out_path}")
    if do_print:
        print("\n" + json.dumps(data, indent=2, ensure_ascii=False))
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=int, required=True, help="series order")
    ap.add_argument("--chapter", type=int, required=True,
                    help="chapter number (0 = prologue)")
    ap.add_argument("--print", action="store_true", dest="do_print",
                    help="also print the JSON to stdout")
    args = ap.parse_args()
    extract(args.book, args.chapter, args.do_print)


if __name__ == "__main__":
    main()
