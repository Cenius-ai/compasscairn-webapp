"""End-to-end tests for the CompassCairn CLI.

Every test drives the real program through a subprocess, exactly the way a
scripting user would, against a throwaway store in a temporary directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "compasscairn.py"
PASSPHRASE = "cairn-test-passphrase"
OTHER_PASSPHRASE = "wrong-passphrase-entirely"


class CliTestCase(unittest.TestCase):
    """Shared harness: a temp store plus a run_cli helper."""

    store: Path

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="compasscairn-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.store = self.tmpdir / "compasscairn.json"

    # -- helpers ---------------------------------------------------------

    def run_cli(self, *args: str, passphrase: str | None = PASSPHRASE,
                store: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("COMPASSCAIRN_PASSPHRASE", None)
        env["NO_COLOR"] = "1"
        if passphrase is not None:
            env["COMPASSCAIRN_PASSPHRASE"] = passphrase
        command = [sys.executable, str(CLI), "--store", str(store or self.store), *args]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )

    def init_store(self) -> None:
        if not self.store.exists():
            result = self.run_cli("init")
            self.assert_returned(result, 0)

    def assert_returned(self, result: subprocess.CompletedProcess[str], code: int) -> None:
        self.assertEqual(
            result.returncode,
            code,
            msg=f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )

    def read_store(self) -> dict:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def add_declination(self, location: str, degrees: str) -> None:
        self.init_store()
        result = self.run_cli("declination", "set", location, degrees)
        self.assert_returned(result, 0)


class InitTests(CliTestCase):
    def test_init_creates_store_without_plaintext_passphrase(self) -> None:
        result = self.run_cli("init")
        self.assert_returned(result, 0)
        self.assertIn("Initialized CompassCairn store at", result.stdout)
        self.assertIn("drift threshold 10.0", result.stdout)

        raw = self.store.read_text(encoding="utf-8")
        self.assertNotIn(PASSPHRASE, raw)
        data = json.loads(raw)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["threshold_degrees"], 10.0)
        self.assertEqual(data["kdf"]["name"], "scrypt")
        self.assertEqual(data["kdf"]["n"], 16384)
        self.assertEqual(len(data["kdf"]["salt"]), 32)
        self.assertEqual(len(data["passphrase_hash"]), 64)
        self.assertEqual(data["waypoints"], [])
        self.assertEqual(data["declinations"], [])

    def test_init_rejects_short_passphrase_and_writes_nothing(self) -> None:
        result = self.run_cli("init", passphrase="short")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("passphrase must be at least 8 characters", result.stderr)
        self.assertFalse(self.store.exists())

    def test_init_refuses_to_overwrite_existing_store(self) -> None:
        self.init_store()
        self.add_declination("Reykjavik", "-13.5")
        self.run_cli("waypoint", "add", "Whale Bay", "--location", "Reykjavik",
                     "--mag", "170", "--true", "-170")
        before = self.store.read_bytes()

        result = self.run_cli("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CompassCairn store already initialized", result.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(len(self.read_store()["waypoints"]), 1)

    def test_unknown_subcommand_prints_usage_and_exits_2(self) -> None:
        result = self.run_cli("frobnicate")
        self.assert_returned(result, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unknown_flag_exits_2(self) -> None:
        self.init_store()
        result = self.run_cli("waypoint", "list", "--nonsense")
        self.assert_returned(result, 2)
        self.assertIn("usage:", result.stderr)

    def test_missing_required_argument_is_named(self) -> None:
        self.init_store()
        result = self.run_cli("waypoint", "add", "No Bearing")
        self.assert_returned(result, 2)
        self.assertIn("--location", result.stderr)
        self.assertIn("--mag", result.stderr)

    def test_missing_store_gives_actionable_message(self) -> None:
        result = self.run_cli("waypoint", "list")
        self.assert_returned(result, 4)
        self.assertIn("No CompassCairn store found", result.stderr)
        self.assertIn("compasscairn init", result.stderr)

    def test_corrupt_store_gives_actionable_message(self) -> None:
        self.store.write_text("{not json at all", encoding="utf-8")
        result = self.run_cli("declination", "list")
        self.assert_returned(result, 4)
        self.assertIn("is not valid JSON", result.stderr)


class PassphraseGateTests(CliTestCase):
    def test_wrong_passphrase_exits_3_and_leaves_store_untouched(self) -> None:
        self.add_declination("Reykjavik", "-13.5")
        before = self.store.read_bytes()

        for command in (
            ("declination", "set", "Reykjavik", "-14.0"),
            ("declination", "get", "Reykjavik"),
            ("declination", "list"),
            ("waypoint", "list"),
            ("drift", "list"),
            ("config", "set", "threshold", "5"),
        ):
            with self.subTest(command=command):
                result = self.run_cli(*command, passphrase=OTHER_PASSPHRASE)
                self.assert_returned(result, 3)
                self.assertEqual(result.stdout, "")
                self.assertIn("incorrect passphrase", result.stderr)
                self.assertEqual(self.store.read_bytes(), before)

    def test_no_passphrase_and_no_tty_is_a_usage_error(self) -> None:
        self.init_store()
        result = self.run_cli("declination", "list", passphrase=None)
        self.assert_returned(result, 2)
        self.assertIn("COMPASSCAIRN_PASSPHRASE", result.stderr)
        self.assertNotIn(PASSPHRASE, result.stderr)


class DeclinationTests(CliTestCase):
    def test_set_stores_normalised_key_and_original_spelling(self) -> None:
        result = self.run_cli("init")
        self.assert_returned(result, 0)
        result = self.run_cli("declination", "set", "Reykjavik", "-13.5")
        self.assert_returned(result, 0)
        self.assertIn("-13.5", result.stdout)

        records = self.read_store()["declinations"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["location_key"], "reykjavik")
        self.assertEqual(records[0]["display_name"], "Reykjavik")
        self.assertEqual(records[0]["degrees"], -13.5)

    def test_get_matches_case_insensitively(self) -> None:
        self.add_declination("Reykjavik", "-13.5")
        result = self.run_cli("declination", "get", "reykjavik")
        self.assert_returned(result, 0)
        self.assertEqual(result.stdout.strip(), "Reykjavik  -13.5")

    def test_get_unknown_location_is_a_plain_non_zero_message(self) -> None:
        self.init_store()
        result = self.run_cli("declination", "get", "atlantis")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No declination stored for atlantis", result.stderr)
        self.assertIn("compasscairn declination set", result.stderr)

    def test_set_updates_in_place_instead_of_duplicating(self) -> None:
        self.add_declination("Reykjavik", "-13.5")
        self.add_declination("Sable Point", "20")
        result = self.run_cli("declination", "set", "reykjavik", "-14.0")
        self.assert_returned(result, 0)

        records = self.read_store()["declinations"]
        self.assertEqual(len(records), 2)
        reykjavik_rows = [row for row in records if row["location_key"] == "reykjavik"]
        self.assertEqual(len(reykjavik_rows), 1)
        self.assertEqual(reykjavik_rows[0]["degrees"], -14.0)
        self.assertEqual(reykjavik_rows[0]["display_name"], "Reykjavik")

        listed = self.run_cli("declination", "list")
        self.assert_returned(listed, 0)
        reykjavik_lines = [line for line in listed.stdout.splitlines() if "Reykjavik" in line]
        self.assertEqual(len(reykjavik_lines), 1)
        self.assertIn("-14.0", reykjavik_lines[0])
        self.assertNotIn("-13.5", listed.stdout)

    def test_out_of_range_declination_writes_nothing(self) -> None:
        self.init_store()
        before = self.store.read_bytes()
        result = self.run_cli("declination", "set", "Reykjavik", "400")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("declination must be a number between -180 and 180", result.stderr)
        self.assertIn("400", result.stderr)
        self.assertEqual(self.store.read_bytes(), before)

    def test_non_numeric_declination_is_rejected(self) -> None:
        self.init_store()
        result = self.run_cli("declination", "set", "Reykjavik", "N30E")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("declination must be a number between -180 and 180", result.stderr)
        self.assertEqual(self.read_store()["declinations"], [])

    def test_empty_registry_prints_empty_state(self) -> None:
        self.init_store()
        result = self.run_cli("declination", "list")
        self.assert_returned(result, 0)
        self.assertIn("No declinations stored yet", result.stdout)
        self.assertIn("compasscairn declination set <location> <degrees>", result.stdout)


class WaypointTests(CliTestCase):
    def test_wraparound_correction_and_delta(self) -> None:
        self.add_declination("Sable Point", "20")
        result = self.run_cli(
            "waypoint", "add", "Kelp Reef", "--location", "Sable Point",
            "--mag", "170", "--true", "-170",
        )
        self.assert_returned(result, 0)

        waypoint = self.read_store()["waypoints"][0]
        self.assertEqual(waypoint["declination_applied"], 20.0)
        self.assertEqual(waypoint["corrected_bearing"], -170.0)  # 190 wrapped to -170
        self.assertEqual(waypoint["delta"], 0.0)
        self.assertFalse(waypoint["flagged"])
        self.assertIn("ok", result.stdout)
        self.assertNotIn("FLAG", result.stdout)

    def test_out_of_range_bearing_records_nothing(self) -> None:
        self.init_store()
        before = self.store.read_bytes()
        result = self.run_cli(
            "waypoint", "add", "Bad Bearing", "--location", "Gull Rock",
            "--mag", "200", "--true", "10",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("magnetic_bearing must be a number between -180 and 180", result.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.read_store()["waypoints"], [])

    def test_list_row_shows_every_column(self) -> None:
        self.add_declination("Reykjavik", "-13.5")
        self.assert_returned(
            self.run_cli("waypoint", "add", "Whale Bay", "--location", "Reykjavik",
                         "--mag", "170", "--true", "-170"),
            0,
        )
        result = self.run_cli("waypoint", "list")
        self.assert_returned(result, 0)

        lines = result.stdout.splitlines()
        header = next(line for line in lines if line.startswith("NAME"))
        for heading in ("LOCATION", "MAG", "TRUE", "DECL", "CORR", "DELTA", "STATUS"):
            self.assertIn(heading, header)
        row = next(line for line in lines if line.startswith("Whale Bay"))
        for value in ("Reykjavik", "170.0", "-170.0", "-13.5", "156.5", "-33.5", "FLAG"):
            self.assertIn(value, row)
        # aligned: the DECL value ends on the same column as its heading
        self.assertEqual(header.index("DECL") + len("DECL"), row.index("-13.5") + len("-13.5"))

    def test_missing_declination_still_saves_with_notice(self) -> None:
        self.init_store()
        result = self.run_cli(
            "waypoint", "add", "North Spur", "--location", "Gull Rock",
            "--mag", "10", "--true", "10",
        )
        self.assert_returned(result, 0)
        self.assertIn("no declination on file for Gull Rock", result.stdout)
        self.assertIn("applied 0.0", result.stdout)

        waypoint = self.read_store()["waypoints"][0]
        self.assertEqual(waypoint["declination_applied"], 0.0)
        self.assertEqual(waypoint["corrected_bearing"], 10.0)
        self.assertEqual(waypoint["delta"], 0.0)

    def test_empty_log_prints_empty_state(self) -> None:
        self.init_store()
        result = self.run_cli("waypoint", "list")
        self.assert_returned(result, 0)
        self.assertIn("No waypoints logged yet", result.stdout)
        self.assertIn("compasscairn waypoint add <name> --location <loc>", result.stdout)

    def test_store_is_written_atomically_and_keeps_schema_version(self) -> None:
        self.add_declination("Reykjavik", "-13.5")
        self.run_cli("waypoint", "add", "Whale Bay", "--location", "Reykjavik",
                     "--mag", "170", "--true", "-170")
        data = self.read_store()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(data["waypoints"]), 1)
        leftovers = [p.name for p in self.tmpdir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class DriftTests(CliTestCase):
    def seed_flagged_waypoint(self) -> None:
        self.add_declination("Reykjavik", "-13.5")
        self.assert_returned(
            self.run_cli("waypoint", "add", "Whale Bay", "--location", "Reykjavik",
                         "--mag", "170", "--true", "-170"),
            0,
        )

    def test_default_threshold_flags_the_waypoint(self) -> None:
        self.seed_flagged_waypoint()
        result = self.run_cli("drift", "list")
        self.assert_returned(result, 0)
        self.assertIn("Flagged", result.stdout)
        self.assertIn("Whale Bay", result.stdout)
        self.assertIn("-33.5", result.stdout)
        self.assertIn("10.0", result.stdout)

    def test_nothing_flagged_prints_all_clear(self) -> None:
        self.add_declination("Sable Point", "20")
        self.assert_returned(
            self.run_cli("waypoint", "add", "Kelp Reef", "--location", "Sable Point",
                         "--mag", "170", "--true", "-170"),
            0,
        )
        result = self.run_cli("drift", "list")
        self.assert_returned(result, 0)
        self.assertIn("No bearings past the 10.0° drift threshold", result.stdout)
        self.assertNotIn("Kelp Reef", result.stdout)

    def test_threshold_override_does_not_persist(self) -> None:
        self.seed_flagged_waypoint()
        result = self.run_cli("drift", "list", "--threshold", "50")
        self.assert_returned(result, 0)
        self.assertIn("50.0", result.stdout)
        self.assertNotIn("Whale Bay", result.stdout)
        self.assertNotIn("FLAG", result.stdout)
        self.assertIn("No bearings past the 50.0° drift threshold", result.stdout)
        self.assertEqual(self.read_store()["threshold_degrees"], 10.0)

        again = self.run_cli("drift", "list")
        self.assert_returned(again, 0)
        self.assertIn("Whale Bay", again.stdout)

    def test_config_set_threshold_persists(self) -> None:
        self.seed_flagged_waypoint()
        result = self.run_cli("config", "set", "threshold", "5")
        self.assert_returned(result, 0)
        self.assertIn("5.0", result.stdout)
        self.assertEqual(self.read_store()["threshold_degrees"], 5.0)

        report = self.run_cli("drift", "list")
        self.assert_returned(report, 0)
        self.assertIn("Whale Bay", report.stdout)
        self.assertIn("5.0°", report.stdout)

    def test_non_numeric_threshold_is_a_usage_error(self) -> None:
        self.init_store()
        result = self.run_cli("drift", "list", "--threshold", "lots")
        self.assert_returned(result, 2)
        self.assertIn("threshold must be a number", result.stderr)

    def test_zero_threshold_is_rejected(self) -> None:
        self.init_store()
        result = self.run_cli("config", "set", "threshold", "0")
        self.assert_returned(result, 2)
        self.assertIn("threshold must be a number greater than 0", result.stderr)

    def test_360_wraparound_never_invents_a_flag(self) -> None:
        self.add_declination("Cape Fury", "0")
        self.assert_returned(
            self.run_cli("waypoint", "add", "Cape Fury Run", "--location", "Cape Fury",
                         "--mag", "-179", "--true", "179"),
            0,
        )
        waypoint = self.read_store()["waypoints"][0]
        self.assertEqual(waypoint["delta"], 2.0)  # not 358
        self.assertFalse(waypoint["flagged"])


class OutputContractTests(CliTestCase):
    def test_help_lists_every_subcommand(self) -> None:
        result = self.run_cli("--help")
        self.assert_returned(result, 0)
        for command in ("init", "declination", "waypoint", "drift", "config"):
            self.assertIn(command, result.stdout)

    def test_subcommand_help_is_specific(self) -> None:
        result = self.run_cli("waypoint", "add", "--help")
        self.assert_returned(result, 0)
        for flag in ("--location", "--mag", "--true"):
            self.assertIn(flag, result.stdout)

    def test_version_flag(self) -> None:
        result = self.run_cli("--version")
        self.assert_returned(result, 0)
        self.assertIn("compasscairn 1.0.0", result.stdout)

    def test_bare_invocation_prints_help_and_exits_zero(self) -> None:
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        result = subprocess.run(
            [sys.executable, str(CLI)],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        self.assert_returned(result, 0)
        self.assertIn("usage:", result.stdout)

    def test_json_mode_is_machine_readable(self) -> None:
        self.add_declination("Reykjavik", "-13.5")
        self.run_cli("waypoint", "add", "Whale Bay", "--location", "Reykjavik",
                     "--mag", "170", "--true", "-170")
        result = self.run_cli("--json", "waypoint", "list")
        self.assert_returned(result, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["threshold_degrees"], 10.0)
        self.assertEqual(len(payload["waypoints"]), 1)
        self.assertEqual(payload["waypoints"][0]["delta"], -33.5)

        drift = self.run_cli("--json", "drift", "list")
        self.assert_returned(drift, 0)
        drift_payload = json.loads(drift.stdout)
        self.assertEqual(len(drift_payload["flagged"]), 1)
        self.assertEqual(drift_payload["flagged"][0]["name"], "Whale Bay")

    def test_stdout_carries_no_ansi_when_piped(self) -> None:
        self.init_store()
        result = self.run_cli("declination", "list")
        self.assert_returned(result, 0)
        self.assertNotIn("\x1b[", result.stdout)


if __name__ == "__main__":
    unittest.main()
