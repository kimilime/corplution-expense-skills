#!/usr/bin/env python3
"""Persistent place -> place-type memory for Corplution taxi/Didi/Gaode notes.

Stage 2 taxi notes are `打车（<origin place type>-<destination place type>）`.
Public places (火车站/机场/酒店) the model can infer, but private places —
友力国际大厦=公司, 某某公寓=家, 中关村产业园=某客户 — it cannot know, so it used to
ask every month. This file is a persistent, cross-run memory: matching consults it
first (a user-declared place is high-confidence, no question), and confirmed answers
are written back automatically so next month's run resolves them silently.

The memory lives in ``assets/place-definitions.json`` (a stable path under the skill
root, so it survives the per-run ``process/`` reset). If that file is missing or
malformed, loading fails open to the built-in defaults below and never blocks
allocation — mirroring ``policy_config.py`` (missing file -> defaults) and the
journal-write rule (a write failure must not change a script's exit code).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from exit_codes import ExitCode

SCHEMA_VERSION = "place_definitions.v1"

# Canonical place types (mirror the taxi place-type values in allocate_expenses.C).
PLACE_TYPES = {"公司", "机场", "火车站", "酒店", "客户", "家", "餐厅", "其他"}

# No private facts are hard-coded. Every user-specific place (office, home, a
# client's site) lives only in assets/place-definitions.json, which the user and
# agent can edit freely. If that file is missing/corrupt, loading fails open to an
# EMPTY book — allocation still runs, unknown places just get asked as usual.
BUILTIN_DEFAULTS: list[dict[str, Any]] = []

# Public place categories the model recognizes WITHOUT any private memory
# (airports, rail stations, hotels). These are general knowledge, not private
# facts, so they stay as keyword heuristics here — never stored in the JSON and
# never memorized on write-back (e.g. 虹桥T2 needs no memory to become 机场).
PUBLIC_PLACE_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("机场", "航站楼", "T1", "T2", "T3", "3F", "出发", "到达"), "机场"),
    (("火车站", "高铁站", "车站", "虹桥站"), "火车站"),
    (("酒店", "宾馆", "亚朵", "全季", "喜来登", "汉庭"), "酒店"),
]


def place_definitions_path() -> Path:
    """Stable, cross-run location of the place-definitions memory file."""
    return Path(__file__).resolve().parents[1] / "assets" / "place-definitions.json"


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _valid_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    name = _clean(entry.get("name"))
    place_type = _clean(entry.get("place_type"))
    if len(name) < 2 or place_type not in PLACE_TYPES:
        return None
    aliases = [a for a in (_clean(x) for x in (entry.get("aliases") or [])) if len(a) >= 2]
    normalized = {"name": name, "aliases": aliases, "place_type": place_type}
    for optional in ("client_name", "note", "added_on"):
        val = _clean(entry.get(optional))
        if val:
            normalized[optional] = val
    return normalized


def public_place_type(text: str) -> str | None:
    """Return a public place type (机场/火车站/酒店) from general-knowledge keywords,
    or None. Used both to classify obvious public places and to decide a place is
    too generic to be worth memorizing."""
    hay = _clean(text)
    if not hay:
        return None
    for keywords, place_type in PUBLIC_PLACE_KEYWORDS:
        if any(k in hay for k in keywords):
            return place_type
    return None


class PlaceBook:
    """A read/lookup view over the persistent place memory (built-ins + file)."""

    def __init__(self, entries: list[dict[str, Any]], path: Path) -> None:
        self.path = path
        # File entries take priority over built-ins on a name+type collision.
        self.entries: list[dict[str, Any]] = list(entries)

    @classmethod
    def load(cls, path: Path | None = None) -> "PlaceBook":
        """Load the memory, failing open to built-ins. Never raises."""
        path = path or place_definitions_path()
        builtins = [dict(e) for e in BUILTIN_DEFAULTS]
        file_entries: list[dict[str, Any]] = []
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict):
                    raise ValueError("root is not an object")
                version = _clean(payload.get("schema_version"))
                if version and version != SCHEMA_VERSION:
                    raise ValueError(f"unexpected schema_version {version!r}")
                for raw in payload.get("places") or []:
                    entry = _valid_entry(raw)
                    if entry:
                        file_entries.append(entry)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                print(
                    f"ADVISORY: place-definitions memory at {path} could not be read "
                    f"({exc}); falling back to built-in defaults. Allocation continues.",
                    file=sys.stderr,
                )
                file_entries = []
        # File entries first so they win in lookup; built-ins only fill gaps.
        seen = {(e["name"], e["place_type"]) for e in file_entries}
        merged = list(file_entries)
        for e in builtins:
            if (e["name"], e["place_type"]) not in seen:
                merged.append(e)
        return cls(merged, path)

    def lookup(self, text: str) -> tuple[str, str, bool] | None:
        """Return (place_type, confidence, needs_confirmation) for a known place.

        A user-declared place is authoritative: high confidence, no confirmation.
        Match by case-insensitive substring of name or any alias, longest key first
        so a specific name beats a short alias. Returns None when unknown.
        """
        haystack = _clean(text)
        if not haystack:
            return None
        low = haystack.lower()
        best: tuple[int, str] | None = None
        for entry in self.entries:
            keys = [entry["name"], *entry.get("aliases", [])]
            for key in keys:
                if key and key.lower() in low:
                    if best is None or len(key) > best[0]:
                        best = (len(key), entry["place_type"])
        if best is None:
            return None
        return best[1], "high", False

    def remember(self, entries: list[dict[str, Any]], path: Path | None = None) -> int:
        """Upsert (name -> place_type) mappings into the on-disk memory by NAME.

        A place has exactly one current type, so this is an upsert keyed on name,
        not an append keyed on (name, place_type): correcting a place's type
        REPLACES the type on its existing record instead of leaving a stale second
        row for the same name (which older builds did, and which `lookup` would then
        resolve to the wrong, first-written type). Pre-existing duplicate names left
        by that bug are healed here too — the last-written type wins, which is the
        correction the user actually made.

        Fails open: on any error it prints an advisory and returns 0 without raising,
        so a memory-write problem can never change a caller's exit code.
        """
        target = path or self.path or place_definitions_path()
        try:
            candidates = [e for e in (_valid_entry(x) for x in entries) if e]
            if not candidates:
                return 0
            payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "places": []}
            if target.exists():
                loaded = json.loads(target.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    payload = loaded
            payload.setdefault("schema_version", SCHEMA_VERSION)
            places = payload.get("places")
            if not isinstance(places, list):
                places = []

            # Collapse the file to one record per name, healing legacy duplicates.
            # A later duplicate is treated as a correction: its type wins.
            ordered: list[dict[str, Any]] = []
            by_name: dict[str, dict[str, Any]] = {}
            healed = False
            for raw in places:
                if not isinstance(raw, dict):
                    continue
                name = _clean(raw.get("name"))
                if not name:
                    continue
                if name in by_name:
                    healed = True
                    later_type = _clean(raw.get("place_type"))
                    if later_type:
                        by_name[name]["place_type"] = later_type
                    for extra in ("aliases", "client_name", "note", "added_on"):
                        if raw.get(extra):
                            by_name[name][extra] = raw[extra]
                    continue
                by_name[name] = raw
                ordered.append(raw)

            changed = 0
            for entry in candidates:
                current = by_name.get(entry["name"])
                if current is None:
                    by_name[entry["name"]] = entry
                    ordered.append(entry)
                    changed += 1
                    continue
                if _clean(current.get("place_type")) != entry["place_type"]:
                    current["place_type"] = entry["place_type"]
                    changed += 1
                # Preserve prior aliases/notes; add anything the new answer supplied.
                merged_aliases = list(dict.fromkeys(
                    [*current.get("aliases", []), *entry.get("aliases", [])]
                ))
                if merged_aliases:
                    current["aliases"] = merged_aliases
                for optional in ("client_name", "note", "added_on"):
                    if entry.get(optional):
                        current[optional] = entry[optional]

            if not changed and not healed:
                return 0
            payload["places"] = ordered
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, target)
            return changed
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(
                f"ADVISORY: could not update place-definitions memory at {target} "
                f"({exc}); the place type was applied but not memorized.",
                file=sys.stderr,
            )
            return 0


    def forget(self, name: str, place_type: str | None = None, path: Path | None = None) -> int:
        """Remove entries whose name matches (optionally also matching place_type).

        Fails open like remember(): prints an advisory and returns 0 on error,
        never raising. Returns the number of entries removed.
        """
        target = path or self.path or place_definitions_path()
        want = _clean(name)
        want_type = _clean(place_type) if place_type else ""
        try:
            if not target.exists():
                return 0
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                return 0
            places = payload.get("places")
            if not isinstance(places, list):
                return 0
            kept = [
                p for p in places
                if not (
                    isinstance(p, dict)
                    and _clean(p.get("name")) == want
                    and (not want_type or _clean(p.get("place_type")) == want_type)
                )
            ]
            removed = len(places) - len(kept)
            if not removed:
                return 0
            payload["places"] = kept
            payload.setdefault("schema_version", SCHEMA_VERSION)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, target)
            return removed
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            print(
                f"ADVISORY: could not update place-definitions memory at {target} ({exc}).",
                file=sys.stderr,
            )
            return 0


def load_place_book(path: Path | None = None) -> PlaceBook:
    return PlaceBook.load(path)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect or seed the place-definitions memory.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Add a place -> type mapping (deduped).")
    p_add.add_argument("--name", required=True, help="Canonical place name, e.g. 友力国际大厦.")
    p_add.add_argument("--type", required=True, dest="place_type",
                       help="One of: " + "/".join(sorted(PLACE_TYPES)))
    p_add.add_argument("--alias", action="append", default=[], help="Alias (repeatable).")
    p_add.add_argument("--client", dest="client_name", default="", help="Client name for 客户 places.")
    p_add.add_argument("--note", default="", help="Free-text note.")

    sub.add_parser("list", help="List remembered places.")

    p_rm = sub.add_parser("remove", help="Remove a remembered place by name.")
    p_rm.add_argument("--name", required=True, help="Place name to remove.")
    p_rm.add_argument("--type", dest="place_type", default="",
                      help="Optional: only remove the entry with this place type.")

    args = parser.parse_args(argv)

    if args.cmd == "add":
        if args.place_type not in PLACE_TYPES:
            print(f"ERROR: --type must be one of {'/'.join(sorted(PLACE_TYPES))}", file=sys.stderr)
            return ExitCode.COMMAND_ERROR
        entry: dict[str, Any] = {"name": args.name, "place_type": args.place_type, "aliases": args.alias}
        if args.client_name:
            entry["client_name"] = args.client_name
        if args.note:
            entry["note"] = args.note
        book = PlaceBook.load()
        added = book.remember([entry])
        print(f"Added {added} entry(ies) to {place_definitions_path()}.")
        return ExitCode.SUCCESS

    if args.cmd == "list":
        book = PlaceBook.load()
        if not book.entries:
            print("(no places remembered)")
            return ExitCode.SUCCESS
        for entry in book.entries:
            aliases = "/".join(entry.get("aliases", []))
            extra = f"  [{aliases}]" if aliases else ""
            client = f"  client={entry['client_name']}" if entry.get("client_name") else ""
            print(f"{entry['place_type']:<4} {entry['name']}{extra}{client}")
        return ExitCode.SUCCESS

    if args.cmd == "remove":
        book = PlaceBook.load()
        removed = book.forget(args.name, args.place_type or None)
        print(f"Removed {removed} entry(ies) from {place_definitions_path()}.")
        return ExitCode.SUCCESS

    return ExitCode.SUCCESS


if __name__ == "__main__":
    sys.exit(_main())
