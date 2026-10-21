#!/usr/bin/env python3
"""CompassCairn - log waypoint bearing corrections and flag bearings that drift.

A single-file, standard-library-only command line tool. It keeps one plain JSON
store on disk, gated by a local passphrase, holding:

  * a registry of magnetic declination per location (case-insensitive names),
  * a log of waypoints with their magnetic and recorded true bearing, the
    declination applied to them, the corrected true bearing and the drift delta,
  * the drift threshold used to flag corrections that need re-taking.

Run ``python3 compasscairn.py --help`` for the command list.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

# Modules only the write path needs are imported where they are used
# (tempfile, secrets, datetime, getpass): a read-only run must stay fast.
if TYPE_CHECKING:  # pragma: no cover - annotations are strings at runtime
    from typing import Any, Sequence

PROGRAM = "compasscairn"
VERSION = "1.0.0"
SCHEMA_VERSION = 1

DEFAULT_STORE_NAME = "compasscairn.json"
DEFAULT_THRESHOLD = 10.0
MAX_THRESHOLD = 180.0
MIN_PASSPHRASE_LENGTH = 8
BEARING_MIN = -180.0
BEARING_MAX = 180.0

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

PASSPHRASE_ENV = "COMPASSCAIRN_PASSPHRASE"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PASSPHRASE = 3
EXIT_STORE = 4

# --- Committed design direction (see DESIGN.md) ------------------------------
# Archetype: cli | style family: calm-precise | density: compact | type: mono
# One accent for the primary status only, and never a colour-only signal:
# the word FLAG is always present in the text itself.
ACCENT_HEX = "#318f5a"
ACCENT_ANSI = "\x1b[38;2;49;143;90m"  # #318f5a as a true-colour foreground
DIM_ANSI = "\x1b[2m"
RESET_ANSI = "\x1b[0m"

_COLOR_ENABLED = False


# --- Errors ------------------------------------------------------------------


class CompassCairnError(Exception):
    """Base class for every failure that maps onto a stable exit code."""

    exit_code = EXIT_STORE


class UsageError(CompassCairnError):
    """Bad arguments or values supplied by the user."""

    exit_code = EXIT_USAGE


class PassphraseError(CompassCairnError):
    """The supplied passphrase did not verify against the store."""

    exit_code = EXIT_PASSPHRASE


class StoreError(CompassCairnError):
    """The store is missing, unreadable, corrupt or unwritable."""

    exit_code = EXIT_STORE


# --- Small formatting helpers ------------------------------------------------


def wrap180(degrees: float) -> float:
    """Normalise an angle into the inclusive -180..180 range."""
    return ((degrees + 180.0) % 360.0) - 180.0


def fmt_degrees(value: float) -> str:
    """Format a degree value with at least one decimal and no float noise."""
    if value == 0:
        return "0.0"
    text = f"{value:.4f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def now_iso() -> str:
    """UTC timestamp in ISO-8601 with a trailing Z, second resolution."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def style(text: str, role: str) -> str:
    """Apply one of the committed theme roles, or nothing when colour is off."""
    if not _COLOR_ENABLED:
        return text
    if role == "accent":
        return f"{ACCENT_ANSI}{text}{RESET_ANSI}"
    if role == "dim":
        return f"{DIM_ANSI}{text}{RESET_ANSI}"
    return text


def display_path(raw: str) -> str:
    """Render a store path the way the user typed it, with a ./ hint."""
    if os.path.isabs(raw) or "/" in raw or "\\" in raw:
        return raw
    return f"./{raw}"


def _pad(cell: str, width: int, align: str) -> str:
    return cell.rjust(width) if align == "right" else cell.ljust(width)


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Sequence[str]) -> str:
    """Render fixed-width aligned columns with a rule under the header row."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    lines = [
        "  ".join(_pad(header, widths[i], aligns[i]) for i, header in enumerate(headers)).rstrip(),
        "  ".join("-" * widths[i] for i in range(len(headers))).rstrip(),
    ]
    for row in rows:
        lines.append("  ".join(_pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# --- Value parsing -----------------------------------------------------------


def parse_bearing(raw: str, field_name: str) -> float:
    """Parse a bearing/declination in -180..180 inclusive, or raise UsageError."""
    text = str(raw).strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise UsageError(
            f"{field_name} must be a number between -180 and 180 (got {text!r})"
        ) from None
    if not math.isfinite(value) or value < BEARING_MIN or value > BEARING_MAX:
        raise UsageError(f"{field_name} must be a number between -180 and 180 (got {text})")
    return value


def parse_threshold(raw: str) -> float:
    """Parse a drift threshold: numeric, greater than 0 and at most 180."""
    text = str(raw).strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise UsageError(
            f"threshold must be a number greater than 0 and at most 180 (got {text!r})"
        ) from None
    if not math.isfinite(value) or value <= 0 or value > MAX_THRESHOLD:
        raise UsageError(
            f"threshold must be a number greater than 0 and at most 180 (got {text})"
        )
    return value


def parse_name(raw: str, field_name: str) -> str:
    text = str(raw).strip()
    if not text:
        raise UsageError(f"{field_name} must not be empty")
    return text


def location_key(name: str) -> str:
    """Normalised key so 'Reykjavik', 'reykjavik ' and 'REYKJAVIK' match."""
    return name.strip().lower()


# --- Passphrase gate ---------------------------------------------------------


def passphrase_from_env() -> str | None:
    value = os.environ.get(PASSPHRASE_ENV)
    if value is None or value.strip() == "":
        return None
    return value


def read_passphrase(prompt: str) -> str:
    """Read the passphrase from COMPASSCAIRN_PASSPHRASE or an interactive prompt."""
    from_env = passphrase_from_env()
    if from_env is not None:
        return from_env
    if not sys.stdin.isatty():
        raise UsageError(
            f"passphrase required: set {PASSPHRASE_ENV} or run from an interactive terminal"
        )
    import getpass

    return getpass.getpass(prompt)


def read_new_passphrase() -> str:
    """Choose the passphrase for a new store (prompted twice when interactive)."""
    passphrase = read_passphrase("Choose a passphrase: ")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise UsageError(f"passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters")
    if passphrase_from_env() is not None:
        return passphrase
    import getpass

    confirmation = getpass.getpass("Repeat the passphrase: ")
    if confirmation != passphrase:
        raise UsageError("passphrases do not match")
    return passphrase


def derive_hash(passphrase: str, salt: bytes, n: int, r: int, p: int) -> str:
    digest = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN
    )
    return digest.hex()


# --- Records and store -------------------------------------------------------


@dataclass
class DeclinationRecord:
    location_key: str
    display_name: str
    degrees: float
    updated_at: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DeclinationRecord":
        return cls(
            location_key=str(raw["location_key"]),
            display_name=str(raw.get("display_name") or raw["location_key"]),
            degrees=float(raw["degrees"]),
            updated_at=str(raw.get("updated_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_key": self.location_key,
            "display_name": self.display_name,
            "degrees": self.degrees,
            "updated_at": self.updated_at,
        }


@dataclass
class Waypoint:
    id: str
    name: str
    location_key: str
    location_display: str
    magnetic_bearing: float
    true_bearing: float
    declination_applied: float
    corrected_bearing: float
    delta: float
    flagged: bool
    created_at: str
    note: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Waypoint":
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            location_key=str(raw.get("location_key", "")),
            location_display=str(raw.get("location_display") or raw.get("location_key", "")),
            magnetic_bearing=float(raw["magnetic_bearing"]),
            true_bearing=float(raw["true_bearing"]),
            declination_applied=float(raw.get("declination_applied", 0.0)),
            corrected_bearing=float(raw["corrected_bearing"]),
            delta=float(raw["delta"]),
            flagged=bool(raw.get("flagged", False)),
            created_at=str(raw.get("created_at", "")),
            note=str(raw.get("note", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "location_key": self.location_key,
            "location_display": self.location_display,
            "magnetic_bearing": self.magnetic_bearing,
            "true_bearing": self.true_bearing,
            "declination_applied": self.declination_applied,
            "corrected_bearing": self.corrected_bearing,
            "delta": self.delta,
            "flagged": self.flagged,
            "created_at": self.created_at,
            "note": self.note,
        }


@dataclass
class Store:
    path: Path
    shown_path: str
    passphrase_hash: str
    kdf: dict[str, Any]
    threshold_degrees: float
    created_at: str
    waypoints: list[Waypoint] = field(default_factory=list)
    declinations: list[DeclinationRecord] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    # -- construction --------------------------------------------------------

    @classmethod
    def new(cls, path: Path, shown_path: str, passphrase: str) -> "Store":
        import secrets

        salt = secrets.token_bytes(SALT_BYTES)
        kdf = {
            "name": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": salt.hex(),
        }
        return cls(
            path=path,
            shown_path=shown_path,
            passphrase_hash=derive_hash(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P),
            kdf=kdf,
            threshold_degrees=DEFAULT_THRESHOLD,
            created_at=now_iso(),
        )

    @classmethod
    def parse_file(cls, path: Path, shown_path: str) -> "Store":
        """Read the store file and parse it as JSON.

        The store is plain JSON by contract: it is parsed with ``json.loads``
        and every field is then type-checked by ``_from_dict``. That is the only
        parser this tool ever runs over the file, so a tampered or corrupt store
        can at worst produce a JSON error or a schema-validation error.
        """
        if not path.exists():
            raise StoreError(
                f"No CompassCairn store found at {shown_path} — run: compasscairn init"
            )
        if path.is_dir():
            raise StoreError(f"{shown_path} is a directory, not a CompassCairn store")
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StoreError(f"could not read {shown_path}: {exc.strerror or exc}") from exc
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise StoreError(
                f"{shown_path} is not valid JSON (line {exc.lineno} column {exc.colno}) — "
                "restore it from a backup or move it aside and run: compasscairn init"
            ) from exc
        if not isinstance(data, dict):
            raise StoreError(f"{shown_path} is not a CompassCairn store (expected a JSON object)")
        try:
            return cls._from_dict(path, shown_path, data)
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(
                f"{shown_path} is not a valid CompassCairn store ({type(exc).__name__}: {exc}) — "
                "restore it from a backup or move it aside and run: compasscairn init"
            ) from exc

    @classmethod
    def _from_dict(cls, path: Path, shown_path: str, data: dict[str, Any]) -> "Store":
        kdf = data["kdf"]
        if not isinstance(kdf, dict) or "salt" not in kdf:
            raise ValueError("kdf block missing salt")
        return cls(
            path=path,
            shown_path=shown_path,
            passphrase_hash=str(data["passphrase_hash"]),
            kdf=kdf,
            threshold_degrees=float(data.get("threshold_degrees", DEFAULT_THRESHOLD)),
            created_at=str(data.get("created_at", "")),
            waypoints=[Waypoint.from_dict(item) for item in data.get("waypoints", [])],
            declinations=[DeclinationRecord.from_dict(item) for item in data.get("declinations", [])],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )

    # -- persistence ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "threshold_degrees": self.threshold_degrees,
            "kdf": self.kdf,
            "passphrase_hash": self.passphrase_hash,
            "created_at": self.created_at,
            "waypoints": [waypoint.to_dict() for waypoint in self.waypoints],
            "declinations": [record.to_dict() for record in self.declinations],
        }

    def save(self) -> None:
        """Write the store atomically: temp file in the same directory, then replace."""
        import tempfile

        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
            )
        except OSError as exc:
            raise StoreError(f"could not write {self.shown_path}: {exc.strerror or exc}") from exc
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            raise StoreError(f"could not write {self.shown_path}: {exc.strerror or exc}") from exc
        finally:
            if os.path.exists(temp_name):
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    # -- lookups -------------------------------------------------------------

    def declination_for(self, location: str) -> DeclinationRecord | None:
        key = location_key(location)
        for record in self.declinations:
            if record.location_key == key:
                return record
        return None

    def upsert_declination(self, location: str, degrees: float) -> tuple[DeclinationRecord, bool]:
        """Insert or update a location; returns (record, created)."""
        key = location_key(location)
        existing = self.declination_for(location)
        if existing is not None:
            existing.degrees = degrees
            existing.updated_at = now_iso()
            return existing, False
        record = DeclinationRecord(
            location_key=key,
            display_name=location.strip(),
            degrees=degrees,
            updated_at=now_iso(),
        )
        self.declinations.append(record)
        return record, True

    def next_waypoint_id(self) -> str:
        return format(len(self.waypoints) + 1, "04x")

    def flags_at(self, threshold: float) -> list[Waypoint]:
        return [waypoint for waypoint in self.waypoints if abs(waypoint.delta) > threshold]


# --- Command helpers ---------------------------------------------------------


def resolve_store(args: argparse.Namespace) -> tuple[Path, str]:
    raw = getattr(args, "store", None) or DEFAULT_STORE_NAME
    return Path(raw).expanduser(), display_path(raw)


def want_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def open_store(args: argparse.Namespace) -> Store:
    path, shown = resolve_store(args)
    return Store.parse_file(path, shown)


def unlock_store(args: argparse.Namespace) -> Store:
    """Read the store and verify the passphrase before any read or write."""
    store = open_store(args)
    passphrase = read_passphrase("Passphrase: ")
    verify_passphrase(store, passphrase)
    return store


def verify_passphrase(store: Store, passphrase: str) -> None:
    try:
        salt = bytes.fromhex(str(store.kdf["salt"]))
        n = int(store.kdf.get("n", SCRYPT_N))
        r = int(store.kdf.get("r", SCRYPT_R))
        p = int(store.kdf.get("p", SCRYPT_P))
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError(
            f"{store.shown_path} has a damaged kdf block — restore it from a backup"
        ) from exc
    candidate = derive_hash(passphrase, salt, n, r, p)
    if not hmac.compare_digest(candidate, store.passphrase_hash):
        raise PassphraseError("incorrect passphrase — nothing was read or written")


# --- Commands ----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path, shown = resolve_store(args)
    if path.exists():
        raise StoreError(f"CompassCairn store already initialized at {shown}")
    passphrase = read_new_passphrase()
    store = Store.new(path, shown, passphrase)
    store.save()
    if want_json(args):
        emit_json(
            {
                "status": "initialized",
                "store": shown,
                "schema_version": SCHEMA_VERSION,
                "threshold_degrees": DEFAULT_THRESHOLD,
            }
        )
        return EXIT_OK
    print(
        f"Initialized CompassCairn store at {shown} "
        f"(drift threshold {fmt_degrees(DEFAULT_THRESHOLD)}°)"
    )
    return EXIT_OK


def cmd_declination_set(args: argparse.Namespace) -> int:
    location = parse_name(args.location, "location")
    degrees = parse_bearing(args.degrees, "declination")
    store = unlock_store(args)
    record, created = store.upsert_declination(location, degrees)
    store.save()
    if want_json(args):
        emit_json({"status": "created" if created else "updated", **record.to_dict()})
        return EXIT_OK
    verb = "set" if created else "updated"
    print(f"Declination for {record.display_name} {verb} to {fmt_degrees(record.degrees)}°")
    return EXIT_OK


def cmd_declination_get(args: argparse.Namespace) -> int:
    location = parse_name(args.location, "location")
    store = unlock_store(args)
    record = store.declination_for(location)
    if record is None:
        raise StoreError(
            f"No declination stored for {location.strip()} — set one with: "
            f"compasscairn declination set {location.strip()} <degrees>"
        )
    if want_json(args):
        emit_json(record.to_dict())
        return EXIT_OK
    print(f"{record.display_name}  {fmt_degrees(record.degrees)}")
    return EXIT_OK


def cmd_declination_list(args: argparse.Namespace) -> int:
    store = unlock_store(args)
    records = sorted(store.declinations, key=lambda record: record.location_key)
    if want_json(args):
        emit_json({"declinations": [record.to_dict() for record in records]})
        return EXIT_OK
    if not records:
        print(
            "No declinations stored yet — add one with: "
            "compasscairn declination set <location> <degrees>"
        )
        return EXIT_OK
    rows = [[record.display_name, fmt_degrees(record.degrees)] for record in records]
    print(render_table(["LOCATION", "DECL"], rows, ["left", "right"]))
    print()
    print(f"{len(records)} location(s) on file")
    return EXIT_OK


def cmd_waypoint_add(args: argparse.Namespace) -> int:
    name = parse_name(args.name, "name")
    location = parse_name(args.location, "location")
    magnetic = parse_bearing(args.magnetic, "magnetic_bearing")
    true_bearing = parse_bearing(args.true_bearing, "true_bearing")

    store = unlock_store(args)
    record = store.declination_for(location)
    declination = record.degrees if record is not None else 0.0
    corrected = wrap180(magnetic + declination)
    delta = wrap180(corrected - true_bearing)
    waypoint = Waypoint(
        id=store.next_waypoint_id(),
        name=name,
        location_key=location_key(location),
        location_display=record.display_name if record is not None else location.strip(),
        magnetic_bearing=magnetic,
        true_bearing=true_bearing,
        declination_applied=declination,
        corrected_bearing=corrected,
        delta=delta,
        flagged=abs(delta) > store.threshold_degrees,
        created_at=now_iso(),
    )
    store.waypoints.append(waypoint)
    store.save()

    notice = None
    if record is None:
        notice = f"no declination on file for {location.strip()} — applied 0.0"

    if want_json(args):
        payload = {"status": "saved", "waypoint": waypoint.to_dict()}
        if notice is not None:
            payload["notice"] = notice
        emit_json(payload)
        return EXIT_OK

    if notice is not None:
        print(notice)
    print(f"Saved waypoint {waypoint.id} \"{waypoint.name}\"")
    rows = [
        ["location", waypoint.location_display],
        ["magnetic_bearing", fmt_degrees(waypoint.magnetic_bearing)],
        ["true_bearing", fmt_degrees(waypoint.true_bearing)],
        ["declination_applied", fmt_degrees(waypoint.declination_applied)],
        ["corrected_bearing", fmt_degrees(waypoint.corrected_bearing)],
        ["delta", fmt_degrees(waypoint.delta)],
        ["status", _status_text(waypoint, store.threshold_degrees)],
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label.ljust(width)}  {value}")
    return EXIT_OK


def _flag(waypoint: Waypoint, threshold: float) -> bool:
    return abs(waypoint.delta) > threshold


def _status_text(waypoint: Waypoint, threshold: float) -> str:
    """Long-form status for the single-waypoint report; the word is never colour-only."""
    if _flag(waypoint, threshold):
        return style(f"FLAG (past the {fmt_degrees(threshold)}° drift threshold)", "accent")
    return f"ok (within the {fmt_degrees(threshold)}° drift threshold)"


def _status_badge(waypoint: Waypoint, threshold: float) -> str:
    """Short-form status for table rows."""
    if _flag(waypoint, threshold):
        return style("FLAG", "accent")
    return "ok"


def cmd_waypoint_list(args: argparse.Namespace) -> int:
    store = unlock_store(args)
    if want_json(args):
        emit_json(
            {
                "threshold_degrees": store.threshold_degrees,
                "waypoints": [waypoint.to_dict() for waypoint in store.waypoints],
            }
        )
        return EXIT_OK
    if not store.waypoints:
        print(
            "No waypoints logged yet — add one with: compasscairn waypoint add <name> "
            "--location <loc> --mag <deg> --true <deg>"
        )
        return EXIT_OK

    headers = ["NAME", "LOCATION", "MAG", "TRUE", "DECL", "CORR", "DELTA", "STATUS"]
    aligns = ["left", "left", "right", "right", "right", "right", "right", "left"]
    rows = [
        [
            waypoint.name,
            waypoint.location_display,
            fmt_degrees(waypoint.magnetic_bearing),
            fmt_degrees(waypoint.true_bearing),
            fmt_degrees(waypoint.declination_applied),
            fmt_degrees(waypoint.corrected_bearing),
            fmt_degrees(waypoint.delta),
            _status_badge(waypoint, store.threshold_degrees),
        ]
        for waypoint in store.waypoints
    ]
    print(render_table(headers, rows, aligns))
    print()
    print(
        f"{len(store.waypoints)} waypoint(s) logged — drift threshold "
        f"{fmt_degrees(store.threshold_degrees)}°"
    )
    return EXIT_OK


def cmd_drift_list(args: argparse.Namespace) -> int:
    override = args.threshold is not None
    threshold = parse_threshold(args.threshold) if override else None
    store = unlock_store(args)
    effective = threshold if threshold is not None else store.threshold_degrees
    flagged = store.flags_at(effective)
    scope = "--threshold override" if override else "stored threshold"

    if want_json(args):
        emit_json(
            {
                "threshold_degrees": effective,
                "threshold_source": scope,
                "stored_threshold_degrees": store.threshold_degrees,
                "flagged": [waypoint.to_dict() for waypoint in flagged],
            }
        )
        return EXIT_OK

    if not flagged:
        print(f"No bearings past the {fmt_degrees(effective)}° drift threshold")
        print("Raise the bar with: compasscairn config set threshold <degrees>")
        return EXIT_OK

    heading = (
        f"Flagged — {len(flagged)} of {len(store.waypoints)} waypoint(s) past the "
        f"{fmt_degrees(effective)}° drift threshold ({scope})"
    )
    print(style(heading, "accent"))
    rows = [
        [
            waypoint.name,
            waypoint.location_display,
            fmt_degrees(waypoint.corrected_bearing),
            fmt_degrees(waypoint.true_bearing),
            fmt_degrees(waypoint.delta),
            "FLAG",
        ]
        for waypoint in flagged
    ]
    print(render_table(
        ["NAME", "LOCATION", "CORR", "TRUE", "DELTA", "STATUS"],
        rows,
        ["left", "left", "right", "right", "right", "left"],
    ))
    return EXIT_OK


def cmd_config_set_threshold(args: argparse.Namespace) -> int:
    threshold = parse_threshold(args.degrees)
    store = unlock_store(args)
    previous = store.threshold_degrees
    store.threshold_degrees = threshold
    store.save()
    if want_json(args):
        emit_json(
            {
                "status": "updated",
                "threshold_degrees": threshold,
                "previous_threshold_degrees": previous,
            }
        )
        return EXIT_OK
    print(f"Drift threshold set to {fmt_degrees(threshold)}° (was {fmt_degrees(previous)}°)")
    return EXIT_OK


# --- Argument parsing --------------------------------------------------------

EXAMPLES = """\
examples:
  compasscairn init
  compasscairn declination set Reykjavik -13.5
  compasscairn declination get reykjavik
  compasscairn waypoint add "Whale Bay" --location Reykjavik --mag 170 --true -170
  compasscairn waypoint list
  compasscairn drift list --threshold 20
  compasscairn config set threshold 5

passphrase:
  COMPASSCAIRN_PASSPHRASE=<passphrase> in the environment, otherwise an
  interactive prompt. Exit codes: 0 ok, 2 usage, 3 passphrase, 4 store.
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--store",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"path to the CompassCairn JSON store (default: ./{DEFAULT_STORE_NAME})",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print machine-readable JSON instead of a table",
    )
    common.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="disable ANSI colour even on a terminal",
    )

    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "CompassCairn — log waypoint bearing corrections, keep magnetic "
            "declination per location and flag bearings past a drift threshold."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {VERSION}")
    subcommands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    subcommands.add_parser(
        "init",
        parents=[common],
        help="create the local store and set the passphrase",
        description="Create the JSON store and store a salted scrypt hash of the passphrase.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    ).set_defaults(handler=cmd_init)

    declination = subcommands.add_parser(
        "declination",
        parents=[common],
        help="manage magnetic declination per location",
        description="Set, read back and list stored magnetic declination per location.",
    )
    declination_commands = declination.add_subparsers(
        dest="declination_command", metavar="<subcommand>", required=True
    )
    set_parser = declination_commands.add_parser(
        "set",
        parents=[common],
        help="set or update a location's declination in degrees (-180..180)",
        description="Set the magnetic declination for a location, case-insensitively.",
    )
    set_parser.add_argument("location", help="location name, matched case-insensitively")
    set_parser.add_argument("degrees", metavar="DEGREES", help="declination in degrees, -180..180")
    set_parser.set_defaults(handler=cmd_declination_set)

    get_parser = declination_commands.add_parser(
        "get",
        parents=[common],
        help="print one location's stored declination",
        description="Print the stored declination for a location, matched case-insensitively.",
    )
    get_parser.add_argument("location", help="location name, matched case-insensitively")
    get_parser.set_defaults(handler=cmd_declination_get)

    declination_commands.add_parser(
        "list",
        parents=[common],
        help="list every location with its stored declination",
        description="Print the whole declination registry as aligned columns.",
    ).set_defaults(handler=cmd_declination_list)

    waypoint = subcommands.add_parser(
        "waypoint",
        parents=[common],
        help="log waypoints and list the correction log",
        description="Add waypoints and print the correction log.",
    )
    waypoint_commands = waypoint.add_subparsers(
        dest="waypoint_command", metavar="<subcommand>", required=True
    )

    add_parser = waypoint_commands.add_parser(
        "add",
        parents=[common],
        help="log a waypoint: name --location LOC --mag DEGREES --true DEGREES",
        description=(
            "Log one waypoint. The stored declination for its location is applied, "
            "the corrected true bearing and the drift delta are computed with "
            "-180..180 wraparound and the record is saved."
        ),
    )
    add_parser.add_argument("name", help="waypoint name, for example \"Whale Bay\"")
    add_parser.add_argument(
        "--location", required=True, metavar="LOC", help="location name used for the declination lookup"
    )
    add_parser.add_argument(
        "--mag", required=True, dest="magnetic", metavar="DEGREES", help="magnetic bearing, -180..180"
    )
    add_parser.add_argument(
        "--true",
        required=True,
        dest="true_bearing",
        metavar="DEGREES",
        help="recorded true bearing, -180..180",
    )
    add_parser.set_defaults(handler=cmd_waypoint_add)

    waypoint_commands.add_parser(
        "list",
        parents=[common],
        help="list logged waypoints with declination, corrected bearing and delta",
        description="Print the correction log as aligned columns.",
    ).set_defaults(handler=cmd_waypoint_list)

    drift = subcommands.add_parser(
        "drift",
        parents=[common],
        help="report bearings whose drift delta exceeds the threshold",
        description="Report waypoints whose correction drifted past the threshold in force.",
    )
    drift_commands = drift.add_subparsers(
        dest="drift_command", metavar="<subcommand>", required=True
    )
    drift_list = drift_commands.add_parser(
        "list",
        parents=[common],
        help="flag waypoints past the drift threshold (--threshold to override once)",
        description=(
            "Flag every waypoint whose absolute delta exceeds the stored drift threshold. "
            "--threshold overrides it for this run only."
        ),
    )
    drift_list.add_argument(
        "--threshold",
        metavar="DEGREES",
        default=None,
        help="flag only deltas beyond this value for this run (does not persist)",
    )
    drift_list.set_defaults(handler=cmd_drift_list)

    config = subcommands.add_parser(
        "config",
        parents=[common],
        help="change persisted settings",
        description="Change the persisted drift threshold.",
    )
    config_commands = config.add_subparsers(
        dest="config_command", metavar="<subcommand>", required=True
    )
    config_set = config_commands.add_parser(
        "set",
        parents=[common],
        help="set a persisted setting (threshold)",
        description="Set a persisted setting.",
    )
    config_set_commands = config_set.add_subparsers(
        dest="config_set_command", metavar="<subcommand>", required=True
    )
    threshold_parser = config_set_commands.add_parser(
        "threshold",
        parents=[common],
        help="persist the drift threshold in degrees (> 0, at most 180)",
        description="Persist the drift threshold used by later 'drift list' runs.",
    )
    threshold_parser.add_argument("degrees", metavar="DEGREES", help="drift threshold in degrees")
    threshold_parser.set_defaults(handler=cmd_config_set_threshold)

    return parser


def configure_output(args: argparse.Namespace) -> None:
    global _COLOR_ENABLED
    if getattr(args, "no_color", False):
        _COLOR_ENABLED = False
        return
    if os.environ.get("NO_COLOR"):
        _COLOR_ENABLED = False
        return
    _COLOR_ENABLED = bool(getattr(sys.stdout, "isatty", lambda: False)())


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help()
        return EXIT_OK

    args = parser.parse_args(arguments)
    configure_output(args)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        return int(handler(args))
    except CompassCairnError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        # `compasscairn waypoint list | head` is a normal, successful pipeline.
        return EXIT_OK
    except KeyboardInterrupt:
        print(f"{PROGRAM}: interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"{PROGRAM}: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_STORE


if __name__ == "__main__":
    sys.exit(main())
