import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import codex_project_recovery as recovery

VERSION = "26.1021.3705.0"


def make_home(folder):
    home = folder / "home"
    home.mkdir()
    (home / recovery.TARGET_NAME).write_bytes(
        '{"local-projects": {}, "draft": "日本語の下書き", "settings": [true,null,3]}\n'.encode("utf-8")
    )
    with contextlib.closing(sqlite3.connect(home / recovery.DATABASE_NAME)) as con:
        con.executescript("""
            CREATE TABLE projects(id TEXT, name TEXT, metadata TEXT, position INTEGER,
                                  created_at_ms INTEGER, updated_at_ms INTEGER);
            CREATE TABLE project_roots(project_id TEXT, position INTEGER, path TEXT);
            CREATE TABLE project_idempotency_keys(key TEXT, project_id TEXT, created_at_ms INTEGER);
            INSERT INTO projects VALUES('server-1','Example','{}',0,1000,2000);
            INSERT INTO project_roots VALUES('server-1',0,'C:/Workspace/Example');
            INSERT INTO project_idempotency_keys VALUES('legacy-1','server-1',1000);
            INSERT INTO project_idempotency_keys VALUES('deleted-key','deleted-project',500);
        """)
        con.commit()
    return home


def make_plan(home):
    fingerprint, projects, mapping = recovery.catalog(home / recovery.DATABASE_NAME)
    return {
        "formatVersion": 1, "codexHome": str(home), "appVersion": VERSION,
        "sourceFingerprint": fingerprint, "localProjects": projects, "projectIdMap": mapping,
    }


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.home = make_home(self.folder)
        self.plan = make_plan(self.home)
        self.before = (self.home / recovery.TARGET_NAME).read_bytes()

    def change_database(self, sql):
        with contextlib.closing(sqlite3.connect(self.home / recovery.DATABASE_NAME)) as con:
            con.execute(sql)
            con.commit()

    def test_catalog_keeps_active_ids_and_never_resurrects_deleted_keys(self):
        fingerprint, projects, mapping = recovery.catalog(self.home / recovery.DATABASE_NAME)
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(list(projects), ["legacy-1"])
        self.assertEqual(mapping, {"legacy-1": "server-1"})

    def test_catalog_is_read_only(self):
        path = self.home / recovery.DATABASE_NAME
        before = path.read_bytes()
        recovery.catalog(path)
        self.assertEqual(before, path.read_bytes())

    def test_catalog_reads_committed_wal_data(self):
        with contextlib.closing(sqlite3.connect(self.home / recovery.DATABASE_NAME)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE projects SET name='Committed WAL name'")
            writer.commit()
            self.assertEqual(recovery.catalog(self.home / recovery.DATABASE_NAME)[1]["legacy-1"]["name"], "Committed WAL name")

    def test_missing_database_is_not_created(self):
        missing = self.folder / "missing.sqlite"
        with self.assertRaises(sqlite3.Error):
            with recovery.read_only(missing):
                pass
        self.assertFalse(missing.exists())

    def test_ambiguous_id_map_is_rejected(self):
        self.change_database("INSERT INTO project_idempotency_keys VALUES('second-key','server-1',1001)")
        with self.assertRaises(recovery.RecoveryError):
            recovery.catalog(self.home / recovery.DATABASE_NAME)

    def test_unknown_project_metadata_is_rejected(self):
        self.change_database('UPDATE projects SET metadata=\'{"codex.chatGptBacking":"unreviewed"}\'')
        with self.assertRaises(recovery.RecoveryError):
            recovery.catalog(self.home / recovery.DATABASE_NAME)

    def test_relative_root_is_rejected(self):
        self.change_database("UPDATE project_roots SET path='../outside'")
        with self.assertRaises(recovery.RecoveryError):
            recovery.catalog(self.home / recovery.DATABASE_NAME)

    def test_only_two_json_keys_change(self):
        after = recovery.patched(self.before, self.plan)
        before_state, after_state = json.loads(self.before), json.loads(after)
        changed = {key for key in before_state.keys() | after_state.keys() if before_state.get(key) != after_state.get(key)}
        self.assertEqual(changed, {recovery.LOCAL, recovery.MAPPING})
        self.assertEqual(before_state["draft"], after_state["draft"])
        self.assertEqual(before_state["settings"], after_state["settings"])
        self.assertTrue(after.endswith(b"}\n"))

    def test_project_count_is_not_fixed_at_26(self):
        after = json.loads(recovery.patched(self.before, self.plan))
        self.assertEqual(len(after[recovery.LOCAL]), 1)

    def test_repeated_patch_is_a_no_op(self):
        after = recovery.patched(self.before, self.plan)
        self.assertEqual(recovery.patched(after, self.plan), after)

    def test_nonempty_project_list_is_never_overwritten(self):
        before = b'{"local-projects":{"new-project":{}}}'
        with self.assertRaises(recovery.RecoveryError):
            recovery.patched(before, self.plan)

    def test_existing_id_map_is_never_overwritten(self):
        before = json.dumps({recovery.LOCAL: {}, recovery.MAPPING: {"other-host": {}}}).encode()
        with self.assertRaises(recovery.RecoveryError):
            recovery.patched(before, self.plan)

    def test_ambiguous_project_field_in_another_object_is_rejected(self):
        before = b'{"local-projects":{},"nested":{"local-projects":{}}}'
        with self.assertRaises(recovery.RecoveryError):
            recovery.patched(before, self.plan)

    def test_invalid_json_does_not_get_replaced(self):
        for before in [b"\x00\x00", b"[]", b'{"local-projects":{},"x":NaN}', b'{"local-projects":{},"local-projects":{}}']:
            with self.subTest(before=before), self.assertRaises((recovery.RecoveryError, ValueError)):
                recovery.patched(before, self.plan)

    def test_prepare_uses_a_consistent_backup_without_changing_live_json(self):
        with mock.patch.object(recovery, "checked_app", return_value=VERSION):
            path = recovery.prepare(self.home, self.folder / "private-data")
        self.assertEqual((self.home / recovery.TARGET_NAME).read_bytes(), self.before)
        self.assertEqual(json.loads(path.read_bytes())["localProjects"], self.plan["localProjects"])
        self.assertEqual(recovery.catalog(path.parent / recovery.DATABASE_NAME)[0], self.plan["sourceFingerprint"])

    def write_plan(self):
        folder = self.folder / "plan"
        folder.mkdir()
        path = folder / "repair-plan.json"
        path.write_bytes(recovery.json_bytes(self.plan))
        return path

    def test_apply_preserves_database_and_records_a_verified_backup(self):
        path = self.write_plan()
        database_before = (self.home / recovery.DATABASE_NAME).read_bytes()
        with mock.patch.object(recovery, "require_closed"), mock.patch.object(recovery, "checked_app", return_value=VERSION), mock.patch.object(recovery, "exclusive", side_effect=lambda target: target.open("r+b")):
            recovery.apply_plan(self.home, path)
            recovery.apply_plan(self.home, path)
        self.assertEqual((self.home / recovery.DATABASE_NAME).read_bytes(), database_before)
        folders = list(path.parent.glob("apply-*"))
        self.assertEqual(len(folders), 1)
        self.assertEqual((folders[0] / "before.json").read_bytes(), self.before)
        self.assertEqual(json.loads((folders[0] / "status.json").read_bytes())["phase"], "applied")

    def test_tampered_plan_is_rejected_before_any_live_write(self):
        self.plan["localProjects"]["legacy-1"]["rootPaths"] = ["C:/Different/Folder"]
        path = self.write_plan()
        with mock.patch.object(recovery, "require_closed"), mock.patch.object(recovery, "checked_app", return_value=VERSION), self.assertRaises(recovery.RecoveryError):
            recovery.apply_plan(self.home, path)
        self.assertEqual((self.home / recovery.TARGET_NAME).read_bytes(), self.before)
        self.assertFalse(list(path.parent.glob("apply-*")))

    def test_database_change_after_preparation_stops_apply(self):
        path = self.write_plan()
        self.change_database("UPDATE projects SET name='Newer user change'")
        with mock.patch.object(recovery, "require_closed"), mock.patch.object(recovery, "checked_app", return_value=VERSION), self.assertRaises(recovery.RecoveryError):
            recovery.apply_plan(self.home, path)
        self.assertEqual((self.home / recovery.TARGET_NAME).read_bytes(), self.before)

    def test_changed_app_version_stops_apply(self):
        path = self.write_plan()
        with mock.patch.object(recovery, "require_closed"), mock.patch.object(recovery, "checked_app", return_value="different-build"), self.assertRaises(recovery.RecoveryError):
            recovery.apply_plan(self.home, path)
        self.assertEqual((self.home / recovery.TARGET_NAME).read_bytes(), self.before)

    def test_other_home_in_plan_is_rejected(self):
        self.plan["codexHome"] = str(self.folder / "another-home")
        path = self.write_plan()
        with mock.patch.object(recovery, "require_closed"), self.assertRaises(recovery.RecoveryError):
            recovery.apply_plan(self.home, path)

    def test_live_app_stops_apply(self):
        path = self.write_plan()
        with mock.patch.object(recovery, "app_running", return_value=True), self.assertRaises(recovery.RecoveryError):
            recovery.apply_plan(self.home, path)
        self.assertEqual((self.home / recovery.TARGET_NAME).read_bytes(), self.before)

    def test_partial_write_rolls_back_exact_original_bytes(self):
        class FailOnce:
            def __init__(self, stream):
                self.stream, self.failed = stream, False

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, data):
                if not self.failed:
                    self.failed = True
                    self.stream.write(data[:3])
                    self.stream.flush()
                    raise OSError("Injected partial write")
                return self.stream.write(data)

        path = self.home / recovery.TARGET_NAME
        after = recovery.patched(self.before, self.plan)
        with path.open("r+b") as stream, self.assertRaises(OSError):
            recovery.replace_locked(FailOnce(stream), self.before, after)
        self.assertEqual(path.read_bytes(), self.before)

    def test_process_classification_keeps_unknown_processes_blocking(self):
        self.assertEqual(recovery.blocking_processes([{"role": "crashpad-handler"}]), [])
        for role in ["main", "renderer", "utility", "gpu-process", "unknown"]:
            self.assertTrue(recovery.blocking_processes([{"role": role}]))
        with self.assertRaises(recovery.RecoveryError):
            recovery.blocking_processes([{}])

    def test_app_owned_launch_is_rejected_even_if_explorer_is_an_ancestor(self):
        with mock.patch.object(recovery, "powershell_json", return_value=["python.exe", "codex.exe", "explorer.exe"]), self.assertRaises(recovery.RecoveryError):
            recovery.require_external_console()

    def test_external_interactive_console_is_accepted(self):
        output = mock.Mock()
        output.isatty.return_value = True
        with mock.patch.object(recovery, "powershell_json", return_value=["python.exe", "cmd.exe", "explorer.exe"]), mock.patch.object(recovery.sys, "stdout", output):
            recovery.require_external_console()

    def test_inspect_reports_counts_without_mutating_state(self):
        before = (self.home / recovery.DATABASE_NAME).read_bytes()
        result = recovery.inspect_state(self.home)
        self.assertEqual(result["state"][0]["localProjectCount"], 0)
        self.assertEqual(result["databases"][0]["projectCount"], 1)
        self.assertEqual((self.home / recovery.DATABASE_NAME).read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Requires actual Windows file sharing")
    def test_windows_exclusive_lock_blocks_reads_and_app_style_rename(self):
        path = self.home / recovery.TARGET_NAME
        staged = self.folder / "staged.json"
        staged.write_bytes(b"{}")
        with recovery.exclusive(path):
            with self.assertRaises(PermissionError):
                path.read_bytes()
            with self.assertRaises(PermissionError):
                os.replace(staged, path)
        self.assertEqual(path.read_bytes(), self.before)

    @unittest.skipUnless(os.name == "nt", "Requires cmd.exe")
    def test_launcher_handles_unicode_and_spaces_without_running_recovery(self):
        folder = self.folder / "日本語 folder"
        folder.mkdir()
        launcher = folder / "Restore-Codex-Projects.cmd"
        launcher.write_bytes((recovery.HERE / launcher.name).read_bytes())
        (folder / "codex_project_recovery.py").write_text("print('LAUNCHER_TEST_OK')\n", encoding="utf-8")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(launcher)], input=b"\r\n", capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"LAUNCHER_TEST_OK", result.stdout)


class AsarTests(unittest.TestCase):
    def make_asar(self, folder, *, unpacked=False, offset="0"):
        name = ".vite/build/test.js"
        source = b"throw new Error('This must never be executed');"
        checksum = recovery.digest(source)
        entry = {"size": len(source), "offset": offset, "integrity": {"hash": checksum}}
        if unpacked:
            entry["unpacked"] = True
        header = recovery.json_bytes({"files": {".vite": {"files": {"build": {"files": {"test.js": entry}}}}}})
        path = folder / "app.asar"
        path.write_bytes(struct.pack("<IIII", 4, len(header) + 8, len(header) + 4, len(header)) + header + source)
        return path, {name: checksum}

    def test_matching_member_is_hashed_without_executing_code(self):
        with tempfile.TemporaryDirectory() as folder:
            path, modules = self.make_asar(Path(folder))
            recovery.verify_asar(path, modules)

    def test_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path, modules = self.make_asar(Path(folder))
            with self.assertRaises(recovery.RecoveryError):
                recovery.verify_asar(path, {next(iter(modules)): "0" * 64})

    def test_unpacked_and_negative_offsets_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for parameters in [{"unpacked": True}, {"offset": "-1"}]:
                with self.subTest(parameters=parameters):
                    path, modules = self.make_asar(Path(folder), **parameters)
                    with self.assertRaises(recovery.RecoveryError):
                        recovery.verify_asar(path, modules)

    def test_unreviewed_build_is_rejected(self):
        with mock.patch.object(recovery, "powershell_json", return_value={"version": "future-version", "architecture": "X64", "path": "unused"}), self.assertRaises(recovery.RecoveryError):
            recovery.checked_app()


if __name__ == "__main__":
    unittest.main()
