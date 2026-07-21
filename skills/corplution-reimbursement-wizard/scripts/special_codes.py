#!/usr/bin/env python3
"""Agent/user-writable fiscal-year charge codes (BD / ADMIN).

The administrative and shared business-development charge codes are year-coded:
``CORP-2026-BD`` / ``CORP-2026-ADMIN`` today, ``CORP-2027-*`` next fiscal year.
This file lets the agent (or user) update them on request — "改成 2027" — and read
them back, instead of hand-editing ``policy.toml`` every year.

Precedence, resolved in ``policy_config.Policy``:

    special-code-definitions.json  >  policy.toml [charge_codes]  >  built-in defaults

so a missing/malformed definition file simply falls back to ``policy.toml`` and never
blocks the workflow (fail-open, like ``policy_config`` and ``place_config``).

IMPORTANT — this file defines only the code *strings*. It never changes the
allocation *logic*: mobile/telecom is still auto-assigned to the ADMIN code, and
other ADMIN matters (年会/半年会/客户会…) are still matched to ADMIN only when the
applicant says so. Rolling the year updates the string everywhere at once because
every consumer reads the code through ``policy_config``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from exit_codes import ExitCode

SCHEMA_VERSION = "special_code_definitions.v1"

# Keys mirror policy.toml [charge_codes].
CODE_KEYS = ("admin", "shared_bd")


def special_codes_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "special-code-definitions.json"


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def load_codes(path: Path | None = None) -> dict[str, str]:
    """Return {key: code} for keys present and non-empty in the definition file.

    Fails open: missing file -> {} silently; malformed file -> {} with an advisory.
    Never raises. Only keys actually defined are returned, so ``policy_config`` keeps
    ``policy.toml`` as the fallback for anything absent here.
    """
    path = path or special_codes_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("root is not an object")
        version = _clean(payload.get("schema_version"))
        if version and version != SCHEMA_VERSION:
            raise ValueError(f"unexpected schema_version {version!r}")
        codes = payload.get("codes")
        if not isinstance(codes, dict):
            raise ValueError("missing 'codes' object")
        out: dict[str, str] = {}
        for key in CODE_KEYS:
            value = _clean(codes.get(key))
            if value:
                out[key] = value
        return out
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"ADVISORY: special-code definitions at {path} could not be read ({exc}); "
            "falling back to policy.toml charge codes. Workflow continues.",
            file=sys.stderr,
        )
        return {}


def set_codes(admin: str | None = None, shared_bd: str | None = None,
              path: Path | None = None) -> bool:
    """Write/update the definition file. Fails open: returns False on error, no raise."""
    target = path or special_codes_path()
    updates = {"admin": _clean(admin), "shared_bd": _clean(shared_bd)}
    updates = {k: v for k, v in updates.items() if v}
    if not updates:
        return False
    try:
        payload: dict = {"schema_version": SCHEMA_VERSION, "codes": {}}
        if target.exists():
            loaded = json.loads(target.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                payload = loaded
        payload.setdefault("schema_version", SCHEMA_VERSION)
        codes = payload.get("codes")
        if not isinstance(codes, dict):
            codes = {}
        codes.update(updates)
        payload["codes"] = codes
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ADVISORY: could not write special-code definitions at {target} ({exc}).",
              file=sys.stderr)
        return False


def _bump_year(code: str, year: str) -> str:
    """Replace a 4-digit fiscal year (20xx) inside a code, e.g. CORP-2026-BD -> CORP-2027-BD."""
    return re.sub(r"20\d{2}", year, code, count=1)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Show or update the BD/ADMIN charge codes.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="Show the currently effective codes.")

    p_set = sub.add_parser("set", help="Set explicit codes.")
    p_set.add_argument("--admin", default="", help="Admin charge code, e.g. CORP-2027-ADMIN.")
    p_set.add_argument("--bd", dest="shared_bd", default="", help="Shared BD code, e.g. CORP-2027-BD.")

    p_year = sub.add_parser("set-year", help="Roll the fiscal year in the current codes.")
    p_year.add_argument("year", help="Four-digit fiscal year, e.g. 2027.")

    args = parser.parse_args(argv)

    # Import here so this CLI can read the fully-resolved codes without a load-time cycle.
    import policy_config

    if args.cmd == "show":
        policy = policy_config.load_policy()
        overrides = load_codes()
        src = special_codes_path() if overrides else policy_config.policy_path()
        print(f"admin     = {policy.admin_code}")
        print(f"shared_bd = {policy.shared_bd_code}")
        print(f"source    = {src}")
        return ExitCode.SUCCESS

    if args.cmd == "set":
        if not (args.admin or args.shared_bd):
            print("ERROR: provide --admin and/or --bd.", file=sys.stderr)
            return ExitCode.COMMAND_ERROR
        ok = set_codes(admin=args.admin or None, shared_bd=args.shared_bd or None)
        if ok:
            print(f"Updated {special_codes_path()}.")
            return ExitCode.SUCCESS
        return ExitCode.OPERATIONAL_ERROR

    if args.cmd == "set-year":
        if not re.fullmatch(r"20\d{2}", args.year):
            print("ERROR: year must be four digits like 2027.", file=sys.stderr)
            return ExitCode.COMMAND_ERROR
        policy = policy_config.load_policy()
        new_admin = _bump_year(policy.admin_code, args.year)
        new_bd = _bump_year(policy.shared_bd_code, args.year)
        if "20" not in policy.admin_code and "20" not in policy.shared_bd_code:
            print("ERROR: current codes contain no 20xx year to roll; use `set --admin --bd`.",
                  file=sys.stderr)
            return ExitCode.COMMAND_ERROR
        ok = set_codes(admin=new_admin, shared_bd=new_bd)
        if ok:
            print(f"Rolled codes to {args.year}: admin={new_admin}, shared_bd={new_bd}")
            print(f"Updated {special_codes_path()}.")
            return ExitCode.SUCCESS
        return ExitCode.OPERATIONAL_ERROR

    return ExitCode.SUCCESS


if __name__ == "__main__":
    sys.exit(_main())
