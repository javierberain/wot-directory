#!/usr/bin/env python3
"""
extract_chapter.py - Run one chapter through the Claude API and get back
structured character / appearance / mention / relationship data.

This script does NOT write to the directory tables. It reads the chapter text
and the current roster, calls the API, and writes the result to
data/extractions/. The separate reconcile step reviews and commits that JSON,
so you can inspect the LLM output before it touches your data.

What changed in the "fix at origin" redesign:
  * Output is produced via a FORCED TOOL CALL (tool_choice) against an
    input_schema, instead of "respond with ONLY JSON" + manual code-fence
    stripping. The API enforces the shape; there is no fragile text parsing.
  * The large static system prompt + tool definition are PROMPT-CACHED, so
    every chapter after the first only pays full price for the volatile
    roster + chapter text in the user message.
  * A `mentions` list distinguishes characters merely REFERENCED in a chapter
    (discussed while offstage) from those present-and-acting (`appearances`).
    This is the structural fix for the appearances-vs-mentions disparity:
    referenced-but-absent characters no longer get dropped OR mis-recorded as
    present.
  * The prompt bakes in the nationality conventions and the "never use a
    role-noun as the primary name" rule, and the enum values come from
    directory_rules so the prompt and the reconciler's validators agree.

Usage:
    python extract_chapter.py --book 1 --chapter 4
    python extract_chapter.py --book 1 --chapter 4 --print   # show JSON
"""
import argparse
import json
import os
import sqlite3
import sys
import time

import anthropic
import httpx
from dotenv import load_dotenv

from directory_rules import VALID_CHAR_TYPES, VALID_FACTION_TYPES

load_dotenv()

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "extractions")
MODEL = "claude-sonnet-4-6"   # current Sonnet: capable + cost-sensible per chapter

# Request timeout. messages.create is non-streaming; a big chapter (the ~19.5k
# word prologue) plus the full roster is ~65k input tokens and can run well over
# a minute server-side. Give it a generous 10-minute read window so a slow-but-
# successful call is not cut off (and then wrongly retried, re-billing it).
_REQUEST_TIMEOUT = httpx.Timeout(600.0, connect=10.0)   # 10 min read

# Retry configuration — ONE layer only, and billing-safe.
# The SDK's OWN auto-retry is DISABLED (max_retries=0): it blindly re-fires the
# request on a read timeout / transport error even when the call already reached
# the model and was billed. All retry decisions are made here instead, and only
# for errors that definitely did NOT reach the model (see _with_retries).
_SDK_MAX_RETRIES = 0
_API_MAX_ATTEMPTS = 3          # tight cap; applies only to safe-retryable cases
_RETRY_BASE_DELAY = 5
_RETRY_MAX_DELAY = 60
_RETRYABLE_STATUSES = {429, 529}   # request rejected (rate limit/overloaded), not billed

# Shown when a call's outcome is unknown (sent but no clean response). We do NOT
# retry these, because the model may have run and been billed already.
_UNKNOWN_OUTCOME_MSG = (
    "The request was SENT but its outcome is unknown — the model may have "
    "completed and been BILLED. NOT retrying, to avoid duplicate charges. "
    "Check the Anthropic console (Usage / Logs) for this call before re-running "
    "this chapter; if it produced output, the chapter just needs reconciling.")

# Batch path configuration (used only when --batch is set).
_BATCH_POLL_INTERVAL = 30      # seconds between status polls
_BATCH_MAX_WAIT = 2 * 60 * 60  # soft cap; batch SLA is 24h but a blocking
                               # script should not wait that long silently

# Enum value sets. character_type / faction_type come from directory_rules so
# the prompt, the tool schema, and the reconciler's validators can never
# disagree. alias / relationship types are listed here.
ALIAS_TYPES = ["given_name", "title", "nickname", "disguise", "epithet"]
RELATIONSHIP_TYPES = ["ally", "enemy", "family", "mentor", "romantic",
                      "rival", "servant", "warder_bond", "other"]

# ---------------------------------------------------------------
# System prompt (static -> cached). The roster is injected per call
# in the user message, after the cache breakpoint.
# ---------------------------------------------------------------
SYSTEM_PROMPT = """\
You are a literary analysis tool building a character directory for the
Wheel of Time series. You are given the full text of ONE chapter and a roster
of characters already known from earlier chapters. Extract structured data
about the characters in THIS chapter by calling the record_chapter_extraction
tool exactly once.

CRITICAL RULES ON IDENTITY:
- The roster lists characters we already know, with all their aliases. When a
  character in this chapter matches a roster entry, set "is_new_character" to
  false and put the roster's primary name in "likely_matches_existing".
- Match generously across name variants. "Mat", "Matrim", "Matrim Cauthon",
  "Mat Cauthon" are the SAME person. A fuller form of a name is not new.
- Only set "is_new_character" to true when the character genuinely does not
  appear on the roster under any name.
- Some characters use a FALSE NAME to disguise themselves. If the text shows a
  character deliberately using an alias to hide their identity, record that
  alias with alias_type "disguise" and explain in notes.
- If unsure whether a name is known or new, set "confidence" to "low" and
  explain in "notes".

NAME_USED_IN_TEXT MUST BE A PROPER NAME (NEVER A ROLE-NOUN):
- "name_used_in_text" must be the character's actual proper name (given name,
  surname, or full name) whenever the chapter supplies one ANYWHERE, even if
  the text mostly refers to them by a role or description. Scan the ENTIRE
  chapter before filling this field; a name that appears once, late, still
  counts.
- Prefer the FULLEST proper name the chapter supplies: if both a given name and
  a surname appear (e.g. "Agelmar" and "Agelmar Jagad", "Egwene" and "Egwene
  al'Vere"), use the full "given + surname" form so the row is created with its
  canonical name. Put any shorter form in aliases_observed as a "given_name".
- NEVER use a pure role-noun or descriptor ("the gleeman", "the peddler", "the
  Wisdom", "the innkeeper", "the weaselly man", "a serving woman", "the
  stranger") as name_used_in_text. If a proper name exists, use it and put the
  role-noun in aliases_observed as an "epithet". If the character is a known
  roster member referred to only by role here, set likely_matches_existing to
  the roster's primary name and use that primary name as name_used_in_text.
- A walk-on with genuinely NO proper name anywhere in the chapter is usually
  not a character at all (see WHAT IS NOT A CHARACTER) — do not invent a
  descriptor row for it.

ALIASES MUST IDENTIFY THE INDIVIDUAL:
- Record a name only if it picks out THIS specific person: a given name,
  surname, formal name, unique epithet ("the Dragon Reborn"), or a title held
  by one person at a time ("the Amyrlin Seat"), or a disguise false-name.
- Do NOT record generic forms of address that could apply to anyone: "boy",
  "lad", "girl", "child", "sister", "my lord", "mistress", "stranger". If a
  word could refer to many people, exclude it.
- Never record a name the text presents as a MISTAKE that is then corrected
  (someone calls her by the wrong title/name and is corrected): omit it
  entirely.
- Do NOT record descriptive phrases built from adjectives plus the character's
  own name ("stout little Verin", "old Cenn"): these are description, not names.
- Do NOT emit rank-decorated variants of a name already given ("Verin Sedai",
  "Lord Agelmar", "Mistress Mathwin", "Agelmar Dai Shan"): record the bare
  proper name, and SEPARATELY record only GENUINE titles that are positional or
  held by one person at a time ("Lord of Fal Dara", "the Amyrlin Seat").

CHARACTER TYPE (species, not nationality):
- "character_type" is the species of an individual named/acting being.
- "nationality" is a SEPARATE field for humans only. Do NOT put a species in
  nationality.

ORIGIN (the "nationality" field — it is broader than geography):
Choose the single most appropriate category:
- GEOGRAPHIC (preferred when known). Use PLACE NAMES, not demonyms ("Andor" not
  "Andoran", "Tear" not "Tairen"). When the text pins a character to a
  village/district, use the FULL compound hierarchy "Village, Region, Nation"
  ("Emond's Field, Two Rivers, Andor") or "District, Nation" ("Maule, Tear").
  ALWAYS include the nation — write "Two Rivers, Andor", never bare
  "Two Rivers".
- STEDDING for Ogier: the named stedding ("Stedding Shangtai"). Never the
  species ("Ogier") as origin.
- PEOPLES whose identity IS their origin: the bare group name ("Aiel",
  "Tuatha'an", "Atha'an Miere").
- "Age of Legends" for figures of the Age of Legends — the Forsaken/Chosen
  (Lanfear, Asmodean, Sammael, Rahvin, Moghedien, Be'lal, Demandred, ...),
  Lews Therin Telamon, and other Age-of-Legends figures the text identifies.
- "Shadow" for Shadow-created beings (Trollocs, Myrddraal, Draghkar, and other
  Shadowspawn).
- "Time" for cosmic / metaphysical, non-geographic entities (the Creator, the
  Dark One, Machin Shin, Mashadar).
- null when no reasonable origin can be inferred. NO hedges or parentheticals;
  NEVER write "unknown", "not stated", or "n/a" — use null.
Faction/rank/title is NOT origin. "The Amyrlin Seat said..." is not evidence of
the speaker's origin.

WHAT IS NOT A CHARACTER:
- A group, mass, army, or collective is NEVER a character — not "Trollocs",
  "the Children of the Light", an Ajah, a House, a village, or a clan. A
  persistent organized group is a FACTION on its individual members. An
  unnamed creature mass ("Trollocs attacked the farm") is an EVENT: describe it
  in the notable_actions/demeanor of the named characters present.
- A creature that is named or acts as a distinct individual IS a character
  (Narg the Trolloc; Bela the horse).
- A named OBJECT, VEHICLE, or PLACE is NEVER a character, even with a proper
  name: a ship (the Spray), a city/region/fortress (Mafal Dadaranell, Tar
  Valon), a sword/ter'angreal/artifact (Callandor, the Horn of Valere,
  Avendesora). Only named, individually-ACTING beings get character rows;
  mention objects/places in the actions/whereabouts of the beings instead.

PRESENT vs MENTIONED (this is important):
- "characters" + "appearances" are for characters who are PRESENT and acting in
  this chapter (they do something, speak, or are physically in a scene).
- "mentions" is for characters merely REFERENCED while NOT present — discussed,
  remembered, named by others, the subject of a letter, etc. A protagonist
  others talk about while he is offstage goes in "mentions", NOT "appearances".
- A character is in EITHER appearances OR mentions for a given chapter, never
  both. Use the roster's name (or the proper name) for the mention.

FACTIONS, WARDER BOND, PERSONALITY vs DEMEANOR:
- Faction membership (Ajah, order, House, clan, society) goes in "factions"
  with a faction_type and a role ("member"/"leader").
- A sister belongs to exactly ONE public Ajah. The BLACK AJAH is a covert,
  OVERLAPPING faction: if the text reveals a sister is Black Ajah, list BOTH her
  public Ajah AND a separate "Black Ajah" faction (faction_type "ajah") — e.g.
  factions: [{"name":"Green Ajah",...}, {"name":"Black Ajah",...}]. Never write
  "formerly Green Ajah" and never replace her public Ajah. Do NOT also add a
  generic "the Shadow" faction for her — Black Ajah already conveys that.
- The Aes Sedai/Warder bond is a relationship with relationship_type
  "warder_bond", directed=true, character_a = the Aes Sedai, character_b = the
  Warder. One row per bonded pair.
- "personality" (stable_traits) is a lasting disposition; "demeanor"
  (appearances) is how they behave in THIS chapter.

Call record_chapter_extraction with the structured result. Do not write any
prose outside the tool call."""


def _nullable_string(desc):
    return {"type": ["string", "null"], "description": desc}


def build_tool():
    """The extraction tool. Its input_schema is the structured shape the model
    must return; tool_choice forces a single call to it."""
    character = {
        "type": "object",
        "properties": {
            "name_used_in_text": {
                "type": "string",
                "description": "The character's PROPER name (never a role-noun)."},
            "likely_matches_existing": _nullable_string(
                "Roster primary name this matches, or null if new."),
            "is_new_character": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "character_type": {"type": "string",
                               "enum": sorted(VALID_CHAR_TYPES)},
            "stable_traits": {
                "type": "object",
                "properties": {
                    "nationality": _nullable_string(
                        "Place name per the conventions; null if unstated. "
                        "Never 'unknown'."),
                    "physical_traits": _nullable_string(""),
                    "age": _nullable_string(""),
                    "filiations": _nullable_string("family / parentage"),
                    "personality": _nullable_string("stable disposition"),
                },
            },
            "factions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "faction_type": {"type": "string",
                                         "enum": sorted(VALID_FACTION_TYPES)},
                        "role": {"type": "string",
                                 "enum": ["member", "leader"]},
                        "notes": _nullable_string(""),
                    },
                    "required": ["name", "faction_type"],
                },
            },
            "aliases_observed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "alias_text": {"type": "string"},
                        "alias_type": {"type": "string", "enum": ALIAS_TYPES},
                        "notes": _nullable_string(""),
                    },
                    "required": ["alias_text", "alias_type"],
                },
            },
            "notes": _nullable_string("anything ambiguous worth a human check"),
        },
        "required": ["name_used_in_text", "is_new_character", "confidence",
                     "character_type"],
    }
    appearance = {
        "type": "object",
        "properties": {
            "character": {"type": "string",
                          "description": "must match a name_used_in_text above"},
            "whereabouts": _nullable_string("where they are this chapter"),
            "notable_actions": _nullable_string("what they do this chapter"),
            "demeanor": _nullable_string("how they present/behave this chapter"),
            "alliances_shown": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["character"],
    }
    mention = {
        "type": "object",
        "properties": {
            "character": {"type": "string",
                          "description": "roster primary name or a proper name "
                                         "of someone referenced but NOT present"},
            "context": _nullable_string("why/how they were referenced"),
        },
        "required": ["character"],
    }
    relationship = {
        "type": "object",
        "properties": {
            "character_a": {"type": "string"},
            "character_b": {"type": "string"},
            "relationship_type": {"type": "string", "enum": RELATIONSHIP_TYPES},
            "directed": {"type": "boolean"},
            "description": _nullable_string(""),
        },
        "required": ["character_a", "character_b", "relationship_type"],
    }
    return {
        "name": "record_chapter_extraction",
        "description": "Record the structured character data extracted from "
                       "this chapter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "characters": {"type": "array", "items": character},
                "appearances": {"type": "array", "items": appearance},
                "mentions": {"type": "array", "items": mention},
                "relationships": {"type": "array", "items": relationship},
            },
            "required": ["characters", "appearances", "mentions",
                         "relationships"],
        },
    }


def get_roster(conn):
    """Known characters with aliases + factions, for the prompt.

    Returns (roster_str, roster_size) where roster_size is the number of
    character rows in the roster (len of the SELECT result)."""
    rows = conn.execute(
        "SELECT character_id, primary_name, character_type, nationality "
        "FROM characters ORDER BY primary_name").fetchall()
    roster = []
    for cid, pname, ctype, nat in rows:
        aliases = conn.execute(
            "SELECT alias_text, alias_type FROM aliases "
            "WHERE character_id = ? AND is_primary = 0", (cid,)).fetchall()
        alias_str = ", ".join(f"{a} ({t})" for a, t in aliases) or "none"
        factions = conn.execute(
            "SELECT f.name FROM character_factions cf "
            "JOIN factions f ON f.faction_id = cf.faction_id "
            "WHERE cf.character_id = ?", (cid,)).fetchall()
        fac_str = ", ".join(f for (f,) in factions) or "none"
        roster.append(
            f"- {pname} | type: {ctype or 'human'} | "
            f"nationality: {nat or 'unknown'} | factions: {fac_str} | "
            f"also known as: {alias_str}")
    roster_str = "\n".join(roster) if roster else \
        "(roster is empty - this is the first chapter)"
    return roster_str, len(rows)


def working_db(book_order):
    """The per-book snapshot is the working DB (same convention as run_book.py's
    working_db()). Book N is read from db/wot_book{N}.db — never the retired
    db/wot.db scratch DB."""
    return os.path.join(DB_DIR, f"wot_book{book_order}.db")


def get_chapter(conn, book_order, chapter_number):
    return conn.execute("""
        SELECT ch.chapter_id, ch.title, ch.full_text, ch.extracted, b.title
        FROM chapters ch JOIN books b ON b.book_id = ch.book_id
        WHERE b.series_order = ? AND ch.chapter_number = ?
    """, (book_order, chapter_number)).fetchone()


def build_request_params(tool, user_msg):
    """The single source of truth for the request. Both the synchronous path
    and the batch path build their request from this, so they are guaranteed
    to send the same thing. Returns exactly the kwargs for
    client.messages.create; the batch path wraps this dict as a request's
    "params" field. cache_control is kept for both paths (best-effort in
    batch)."""
    return {
        "model": MODEL,
        "max_tokens": 36000,
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}}],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": user_msg}],
    }


def _is_connect_establishment_error(exc):
    """True only for a definitive connection-ESTABLISHMENT failure — the request
    never left the client, so re-firing it cannot double-bill. The SDK wraps the
    underlying httpx error as the exception's __cause__. Anything else (read
    error, broken pipe mid-stream) has unknown outcome and is treated as such."""
    return isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout))


def _with_retries(fn, label="API", idempotent=False):
    """Run fn() with a narrow, BILLING-SAFE retry policy. Returns fn()'s result.

    Always retried (the request did NOT reach / was not processed by the model):
      - 429 / 529          : rejected (rate limit / overloaded), never billed.
      - connection-establishment failures : the request never left the client.

    For a NON-idempotent call (idempotent=False, the default — e.g.
    messages.create) a read/response TIMEOUT (APITimeoutError) or any other
    post-send transport error is treated as UNKNOWN OUTCOME and raised
    immediately — never re-fired — because the model may have completed and been
    billed. For an idempotent call (idempotent=True — e.g. a GET batch poll)
    those are safe to retry. The SDK's own auto-retry is disabled
    (max_retries=0) so this is the single retry layer.
    """
    last_exc = None
    for attempt in range(1, _API_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except anthropic.APITimeoutError as exc:
            # Subclass of APIConnectionError, so it MUST be caught first.
            # Timeout = request was sent; outcome unknown. Do not re-fire.
            if not idempotent:
                raise RuntimeError(f"{label}: read/response timeout. "
                                   f"{_UNKNOWN_OUTCOME_MSG}") from exc
            last_exc = exc
            why = "timeout (idempotent GET, safe to retry)"
        except anthropic.APIConnectionError as exc:
            # Only a connection-establishment failure is safe to re-fire; any
            # other transport error after send has unknown outcome.
            if not idempotent and not _is_connect_establishment_error(exc):
                raise RuntimeError(f"{label}: connection error after the request "
                                   f"was sent. {_UNKNOWN_OUTCOME_MSG}") from exc
            last_exc = exc
            why = "connection-establishment error"
        except anthropic.APIStatusError as exc:
            if exc.status_code not in _RETRYABLE_STATUSES:
                raise
            last_exc = exc
            why = f"HTTP {exc.status_code} (rejected, not billed)"
        if attempt == _API_MAX_ATTEMPTS:
            raise last_exc
        delay = min(_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _RETRY_MAX_DELAY)
        print(f"  {label} {why}, attempt {attempt}/{_API_MAX_ATTEMPTS}, "
              f"waiting {delay}s...")
        time.sleep(delay)


def _call_api(client, params):
    """Synchronous forced tool call with prompt-cached system+tool. Retries
    transient errors with backoff (outer loop on top of the SDK's own
    retries)."""
    return _with_retries(lambda: client.messages.create(**params))


def _call_api_batch(client, params, custom_id,
                    poll_interval=_BATCH_POLL_INTERVAL,
                    max_wait=_BATCH_MAX_WAIT):
    """Submit one request as a Message Batch, poll to completion, and return
    the resulting Message object (same shape as client.messages.create)."""
    # 1. Submit (transient-retry-wrapped; a submission can hit 429/529 too).
    batch = _with_retries(
        lambda: client.messages.batches.create(
            requests=[{"custom_id": custom_id, "params": params}]),
        label="batch submit")

    # 2. Record the id immediately, before any polling, so an interrupted run
    #    leaves the id in the terminal.
    print(f"  batch submitted: id={batch.id}  custom_id={custom_id}")

    # 3. Poll until processing ends (or we exceed the soft cap).
    started = time.monotonic()
    while batch.processing_status != "ended":
        elapsed = time.monotonic() - started
        if elapsed > max_wait:
            raise TimeoutError(
                f"batch {batch.id} (custom_id {custom_id}) still "
                f"'{batch.processing_status}' after {int(elapsed)}s "
                f"(max_wait {max_wait}s). It may still finish; retrieve it "
                f"later by id {batch.id}.")
        print(f"  batch {batch.id}: {batch.processing_status}  "
              f"counts={batch.request_counts}")
        time.sleep(poll_interval)
        batch = _with_retries(
            lambda: client.messages.batches.retrieve(batch.id),
            label="batch poll", idempotent=True)
    print(f"  batch {batch.id}: ended  counts={batch.request_counts}")

    # 4. Retrieve and match on custom_id (results are unordered).
    for entry in client.messages.batches.results(batch.id):
        if entry.custom_id != custom_id:
            continue
        # 5. Branch on the per-request result type.
        result_type = entry.result.type
        if result_type == "succeeded":
            message = entry.result.message
            # Carry the batch id alongside the Message so extract() can record
            # it in _meta. The Message's own .id is the message id, not the
            # batch id.
            message._batch_id = batch.id
            return message
        if result_type == "errored":
            # In anthropic 0.104.1 entry.result.error is an ErrorResponse
            # whose own .type is always "error"; the discriminating error type
            # and message are nested one level deeper on .error.
            err = entry.result.error.error
            if getattr(err, "type", None) == "invalid_request_error":
                raise ValueError(
                    f"batch {batch.id} request {custom_id} was rejected as "
                    f"invalid_request_error (malformed body, not retryable): "
                    f"{err.message}")
            raise RuntimeError(
                f"batch {batch.id} request {custom_id} errored "
                f"({getattr(err, 'type', 'unknown')}, retryable - re-run the "
                f"chapter): {getattr(err, 'message', err)}")
        if result_type in ("canceled", "expired"):
            raise RuntimeError(
                f"batch {batch.id} request {custom_id} {result_type} before "
                f"producing a result; re-run the chapter.")
        raise RuntimeError(
            f"batch {batch.id} request {custom_id} returned unexpected result "
            f"type '{result_type}'.")

    # 6. No entry matched our custom_id.
    raise RuntimeError(
        f"batch {batch.id} returned no result for custom_id {custom_id}.")


def extract(book_order, chapter_number, do_print=False, db_path=None,
            use_batch=False, poll_interval=_BATCH_POLL_INTERVAL):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set the ANTHROPIC_API_KEY environment variable first.")

    db = db_path or working_db(book_order)
    if not os.path.exists(db):
        sys.exit(f"Working DB not found: {db}\n"
                 f"Seed it first:  python scripts/start_book.py --book "
                 f"{book_order} --epub \"<file>\" --title \"<title>\"")
    conn = sqlite3.connect(db)
    row = get_chapter(conn, book_order, chapter_number)
    if not row:
        sys.exit(f"Chapter not found: book {book_order}, chapter {chapter_number}")
    chapter_id, ch_title, full_text, extracted, book_title = row
    if extracted:
        print("Note: chapter already marked extracted. Re-running anyway.")

    roster, roster_size = get_roster(conn)
    user_msg = (
        f"BOOK: {book_title}\n"
        f"CHAPTER {chapter_number}: {ch_title}\n\n"
        f"=== KNOWN CHARACTER ROSTER ===\n{roster}\n\n"
        f"=== CHAPTER TEXT ===\n{full_text}")

    client = anthropic.Anthropic(max_retries=_SDK_MAX_RETRIES,
                                 timeout=_REQUEST_TIMEOUT)
    tool = build_tool()
    params = build_request_params(tool, user_msg)
    custom_id = f"b{book_order}_c{chapter_number}"
    print(f"Calling {MODEL} for book {book_order} ch {chapter_number} "
          f"'{ch_title}' ({len(full_text.split())} words)"
          f"{' via batch API' if use_batch else ''}...")

    if use_batch:
        resp = _call_api_batch(client, params, custom_id,
                               poll_interval=poll_interval)
        batch_id = getattr(resp, "_batch_id", None)
    else:
        resp = _call_api(client, params)
        batch_id = None

    # Pull the single forced tool call. No code-fence stripping needed.
    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_block is None:
        sys.exit("Model did not return the expected tool call. "
                 f"stop_reason={resp.stop_reason}")
    if resp.stop_reason == "max_tokens":
        sys.exit(
            f"TRUNCATED: model hit max_tokens ({params['max_tokens']}) for "
            f"book {book_order} ch {chapter_number} before finishing the tool "
            f"call. The extraction is incomplete (characters present, later "
            f"sections empty) and was NOT saved. Raise max_tokens in "
            f"build_request_params and re-run, or split this chapter.")
    data = dict(tool_block.input)
    data.setdefault("characters", [])
    data.setdefault("appearances", [])
    data.setdefault("mentions", [])
    data.setdefault("relationships", [])

    data["_meta"] = {
        "book_order": book_order,
        "chapter_number": chapter_number,
        "chapter_id": chapter_id,
        "chapter_title": ch_title,
        "model": MODEL,
        "via": "batch" if use_batch else "sync",
        "batch_id": batch_id,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens":
            getattr(resp.usage, "cache_read_input_tokens", None),
        "cache_creation_input_tokens":
            getattr(resp.usage, "cache_creation_input_tokens", None),
        "roster_size": roster_size,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"b{book_order}_c{chapter_number}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    chars = data.get("characters", [])
    new = sum(1 for c in chars if c.get("is_new_character"))
    print(f"  characters: {len(chars)}  (new: {new})")
    print(f"  appearances: {len(data.get('appearances', []))}  "
          f"mentions: {len(data.get('mentions', []))}")
    print(f"  relationships: {len(data.get('relationships', []))}")
    u = data["_meta"]
    print(f"  tokens in/out: {u['input_tokens']}/{u['output_tokens']} "
          f"(cache read: {u['cache_read_input_tokens']})")
    print(f"  saved to: {out_path}")
    if do_print:
        print("\n" + json.dumps(data, indent=2, ensure_ascii=False))
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=int, required=True, help="series order")
    ap.add_argument("--chapter", type=int, required=True,
                    help="chapter number (0 = prologue)")
    ap.add_argument("--db", help="target database (default: db/wot_book{N}.db)")
    ap.add_argument("--print", action="store_true", dest="do_print",
                    help="also print the JSON to stdout")
    ap.add_argument("--batch", action="store_true", dest="use_batch",
                    help="extract via the Message Batches API (async, 50%% "
                         "token price) instead of a synchronous call")
    ap.add_argument("--poll-interval", type=int, default=_BATCH_POLL_INTERVAL,
                    help="seconds between batch status polls (with --batch)")
    args = ap.parse_args()
    extract(args.book, args.chapter, args.do_print, db_path=args.db,
            use_batch=args.use_batch, poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()
