"""Recover the empty project-list JSON of specific Windows Codex app builds."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
LOCAL = "local-projects"
MAPPING = "app-server-project-id-by-legacy-project-id-by-host"
TARGET_NAME = ".codex-global-state.json"
DATABASE_NAME = "state_5.sqlite"
ALLOWED_METADATA = {"appearance.color", "appearance.marker"}
PLAN_KEYS = {"formatVersion", "codexHome", "appVersion", "sourceFingerprint", "localProjects", "projectIdMap"}


class RecoveryError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_object(data: bytes | str) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError("Duplicate JSON keys; refusing an ambiguous file")
            result[key] = value
        return result

    def reject_constant(_value):
        raise RecoveryError("Non-finite numbers are not supported in JSON")

    value = json.loads(data, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise RecoveryError("Expected a JSON object")
    return value


def json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def save(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def journal(folder: Path, phase: str, **details) -> None:
    path = folder / "status.json"
    temporary = folder / (".status-" + uuid.uuid4().hex + ".tmp")
    save(temporary, json_bytes({"phase": phase, **details}))
    os.replace(temporary, path)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()


def require_windows() -> None:
    if os.name != "nt":
        raise RecoveryError("The recovery commands require Windows")


def powershell_json(command: str):
    require_windows()
    executable = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    prefix = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; "
    result = subprocess.run(
        [str(executable), "-NoProfile", "-NonInteractive", "-Command", prefix + command],
        check=True, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return json.loads(result.stdout.decode("utf-8-sig"))


def verify_asar(path: Path, modules: dict) -> None:
    """Hash only the reviewed JavaScript members; never execute or extract them."""
    with path.open("rb") as stream:
        prefix = stream.read(16)
        if len(prefix) != 16:
            raise RecoveryError("Incomplete ASAR header")
        marker, header_size, _pickle_size, json_size = struct.unpack("<IIII", prefix)
        if marker != 4 or not 8 <= header_size <= 16 * 1024 * 1024 or not 0 < json_size <= header_size - 8:
            raise RecoveryError("Unsupported ASAR header")
        header = json_object(stream.read(json_size))
        file_size = os.fstat(stream.fileno()).st_size
        for name, expected in modules.items():
            if not name.startswith(".vite/build/") or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise RecoveryError("Invalid supported-build manifest")
            entry = header
            for part in name.split("/"):
                entry = entry["files"][part]
            size, offset = entry.get("size"), int(entry.get("offset", "-1"))
            start = 8 + header_size + offset
            if entry.get("unpacked") or type(size) is not int or not 0 < size <= 16 * 1024 * 1024 or offset < 0 or start + size > file_size:
                raise RecoveryError("Unsupported ASAR member layout")
            stream.seek(start)
            actual = digest(stream.read(size))
            if actual != expected or entry.get("integrity", {}).get("hash") != actual:
                raise RecoveryError("App implementation differs from the reviewed build; stopping")


def checked_app() -> str:
    info = powershell_json(
        "$packages = @(Get-AppxPackage -Name OpenAI.Codex); "
        "if ($packages.Count -ne 1) { throw 'Expected one OpenAI.Codex package' }; "
        "$package = $packages[0]; "
        "@{version=$package.Version.ToString();path=$package.InstallLocation;"
        "architecture=$package.Architecture.ToString()} | ConvertTo-Json -Compress"
    )
    builds = json_object((HERE / "supported-builds.json").read_bytes())
    version = info["version"]
    if version not in builds or info["architecture"].lower() != "x64":
        raise RecoveryError("Unreviewed app build or architecture; inspect works, but recovery is disabled")
    verify_asar(Path(info["path"]) / "app/resources/app.asar", builds[version]["modules"])
    return version


def blocking_processes(rows: list) -> list:
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("role"), str) for row in rows):
        raise RecoveryError("Cannot classify app processes; stopping")
    return [row for row in rows if row["role"] != "crashpad-handler"]


def app_running() -> bool:
    command = r"""$rows = @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe'" | ForEach-Object {
        $role = if ($_.CommandLine -match '--type=([^\s"]+)') { $Matches[1] }
                elseif ($_.CommandLine) { 'main' } else { 'unknown' };
        @{pid=$_.ProcessId;role=$role}
    }); ConvertTo-Json -InputObject $rows -Compress"""
    return bool(blocking_processes(powershell_json(command)))


def require_closed() -> None:
    if app_running():
        raise RecoveryError("ChatGPT still has active processes; no state was changed")


def require_external_console() -> None:
    command = (
        f"$cursor = {os.getpid()}; $chain=@(); $seen=@(); "
        "for ($step=0; $step -lt 32; $step++) { "
        "if ($cursor -le 0 -or $seen -contains $cursor) { break }; $seen += $cursor; "
        "$ancestor=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $cursor); "
        "if (!$ancestor) { break }; $chain += $ancestor.Name; $cursor=$ancestor.ParentProcessId }; "
        "ConvertTo-Json -InputObject $chain -Compress"
    )
    names = powershell_json(command)
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise RecoveryError("Cannot verify the launch origin")
    names = {name.lower() for name in names}
    if names & {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}:
        raise RecoveryError("Run the launcher from File Explorer, outside ChatGPT/Codex")
    if not names & {"explorer.exe", "windowsterminal.exe", "openconsole.exe"} or not sys.stdout or not sys.stdout.isatty():
        raise RecoveryError("Use an external console and keep that window open")


@contextlib.contextmanager
def read_only(path: Path):
    with contextlib.closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=3)) as con:
        con.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 30
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        yield con


def catalog(path: Path) -> tuple[str, dict, dict]:
    with read_only(path) as con:
        con.execute("BEGIN")
        if [row[0] for row in con.execute("PRAGMA integrity_check(10)")] != ["ok"]:
            raise RecoveryError("The project database failed its integrity check; this tool cannot repair SQLite")
        projects = con.execute("SELECT id,name,metadata,position,created_at_ms,updated_at_ms FROM projects ORDER BY position,id").fetchall()
        roots = con.execute("SELECT project_id,position,path FROM project_roots ORDER BY project_id,position").fetchall()
        keys = con.execute("SELECT k.key,k.project_id FROM project_idempotency_keys k JOIN projects p ON p.id=k.project_id ORDER BY k.project_id,k.key").fetchall()
    by_id = {}
    for key, project_id in keys:
        by_id.setdefault(project_id, []).append(key)
    records, mapping = {}, {}
    for project_id, name, metadata, _position, created, updated in projects:
        identifiers = by_id.get(project_id, [])
        paths = [row[2] for row in roots if row[0] == project_id]
        if len(identifiers) != 1 or not isinstance(identifiers[0], str) or not identifiers[0] or identifiers[0] in records:
            raise RecoveryError("Project IDs are missing or ambiguous; refusing to invent an association")
        if not isinstance(name, str) or not paths or any(not isinstance(path, str) or not ntpath.isabs(path) for path in paths):
            raise RecoveryError("Unsupported project name or root paths")
        if type(created) is not int or type(updated) is not int or set(json_object(metadata)) - ALLOWED_METADATA:
            raise RecoveryError("Unsupported project timestamps or additional metadata, including ChatGPT backings")
        legacy = identifiers[0]
        records[legacy] = {"id": legacy, "name": name, "rootPaths": paths, "createdAt": created, "updatedAt": updated}
        mapping[legacy] = project_id
    if not records:
        raise RecoveryError("No saved projects were found")
    fingerprint = digest(json.dumps([projects, roots, keys], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return fingerprint, records, mapping


def patched(before: bytes, plan: dict) -> bytes:
    state = json_object(before)
    mapping = {"local:" + plan["codexHome"]: plan["projectIdMap"]}
    if state.get(LOCAL) == plan["localProjects"] and state.get(MAPPING) == mapping:
        return before
    if state.get(LOCAL) != {} or MAPPING in state:
        raise RecoveryError("This is not the reviewed empty-project state; refusing to overwrite it")
    text = before.decode("utf-8")
    matches = list(re.finditer(r'"local-projects"\s*:\s*\{\s*\}', text))
    if len(matches) != 1:
        raise RecoveryError("The empty project field is not unique")
    found = matches[0]
    replacement = json.dumps(LOCAL) + ":" + json_bytes(plan["localProjects"]).decode("utf-8")
    text = text[:found.start()] + replacement + text[found.end():]
    end = text.rfind("}")
    text = text[:end] + "," + json.dumps(MAPPING) + ":" + json_bytes(mapping).decode("utf-8") + text[end:]
    after = text.encode("utf-8")
    if json_object(after) != {**state, LOCAL: plan["localProjects"], MAPPING: mapping}:
        raise RecoveryError("Unexpected changes outside the two project keys")
    return after


def prepare(home: Path, data_root: Path | None = None) -> Path:
    version = checked_app()
    before = (home / TARGET_NAME).read_bytes()
    state = json_object(before)
    if state.get(LOCAL) != {} or MAPPING in state:
        raise RecoveryError("Recovery only supports an empty local-projects object and an absent ID map")
    data_root = data_root or HERE / "recovery-data"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = data_root / ("run-" + stamp + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    save(run / "preparation-before.json", before)
    snapshot = run / DATABASE_NAME
    with read_only(home / DATABASE_NAME) as con, contextlib.closing(sqlite3.connect(snapshot)) as target:
        con.backup(target)
    fingerprint, records, mapping = catalog(snapshot)
    plan = {
        "formatVersion": 1, "codexHome": str(home), "appVersion": version,
        "sourceFingerprint": fingerprint, "localProjects": records, "projectIdMap": mapping,
    }
    patched(before, plan)
    path = run / "repair-plan.json"
    save(path, json_bytes(plan))
    journal(run, "prepared", projects=len(records))
    print(f"Prepared {len(records)} project registrations. Private plan: {path}", flush=True)
    return path


@contextlib.contextmanager
def exclusive(path: Path):
    require_windows()
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0xC0000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "r+b") as stream:
        yield stream


def replace_locked(stream, before: bytes, after: bytes) -> None:
    try:
        stream.seek(0)
        stream.write(after)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if stream.read() != after:
            raise RecoveryError("Read-back differs from the prepared bytes")
    except BaseException:
        stream.seek(0)
        stream.write(before)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if stream.read() != before:
            raise RecoveryError("Rollback failed; retain the before.json backup")
        raise


def apply_plan(home: Path, plan_path: Path) -> None:
    require_closed()
    plan = json_object(plan_path.read_bytes())
    if set(plan) != PLAN_KEYS or type(plan.get("formatVersion")) is not int or plan["formatVersion"] != 1 or plan["codexHome"] != str(home):
        raise RecoveryError("Plan format or target home differs")
    if checked_app() != plan["appVersion"]:
        raise RecoveryError("The app version changed; rebuild the plan")
    fingerprint, records, mapping = catalog(home / DATABASE_NAME)
    if (fingerprint, records, mapping) != (plan["sourceFingerprint"], plan["localProjects"], plan["projectIdMap"]):
        raise RecoveryError("Saved projects or plan contents changed; rebuild the plan")
    folder = plan_path.parent / ("apply-" + uuid.uuid4().hex)
    with exclusive(home / TARGET_NAME) as stream:
        require_closed()
        before = stream.read()
        after = patched(before, plan)
        if before == after:
            print("SUCCESS: project registrations are already restored.", flush=True)
            return
        folder.mkdir()
        save(folder / "before.json", before)
        save(folder / "planned.json", after)
        save(folder / "repair-plan.json", json_bytes(plan))
        details = {"projects": len(records), "beforeSha256": digest(before), "afterSha256": digest(after)}
        journal(folder, "applying", **details)
        print(f"Backup saved: {folder / 'before.json'}", flush=True)
        require_closed()
        if catalog(home / DATABASE_NAME)[0] != fingerprint:
            raise RecoveryError("Project data changed during backup; no JSON was changed")
        try:
            replace_locked(stream, before, after)
        except BaseException:
            journal(folder, "write-failed", **details)
            raise
        journal(folder, "applied", **details)
    print(f"SUCCESS: {len(records)} project registrations restored. You may reopen ChatGPT.", flush=True)


def restore(home: Path, plan_path: Path) -> None:
    require_external_console()
    print("Waiting: completely quit ChatGPT; leave this window open.", flush=True)
    deadline, closed = time.monotonic() + 1200, 0
    while time.monotonic() < deadline:
        closed = 0 if app_running() else closed + 1
        if closed >= 3:
            apply_plan(home, plan_path)
            return
        time.sleep(1)
    raise RecoveryError("Timed out waiting for app exit; no live JSON was changed")


def inspect_state(home: Path) -> dict:
    result = {"state": [], "databases": []}
    for name in [TARGET_NAME, TARGET_NAME + ".bak"]:
        entry = {"file": name}
        try:
            state = json_object((home / name).read_bytes())
            projects = state.get(LOCAL)
            entry.update(jsonValid=True, localProjectCount=len(projects) if isinstance(projects, dict) else None)
        except (OSError, ValueError, RecoveryError) as error:
            entry["error"] = str(error)
        result["state"].append(entry)
    for path in sorted(home.glob("*.sqlite")) + sorted((home / "sqlite").glob("*.db")):
        entry = {"file": path.name}
        try:
            with read_only(path) as con:
                entry["integrityCheck"] = [row[0] for row in con.execute("PRAGMA integrity_check(10)")]
                if path.name == DATABASE_NAME:
                    entry["projectCount"] = con.execute("SELECT count(*) FROM projects").fetchone()[0]
        except (OSError, sqlite3.Error) as error:
            entry["error"] = str(error)
        result["databases"].append(entry)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="Read-only JSON and SQLite checks")
    commands.add_parser("prepare", help="Create private backups and a plan without changing live state")
    restore_parser = commands.add_parser("restore", help="Wait for app exit, then apply a prepared plan")
    restore_parser.add_argument("plan", type=Path)
    commands.add_parser("recover", help="Prepare and restore from an external console")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    home = codex_home()
    try:
        if args.command == "inspect":
            print(json.dumps(inspect_state(home), ensure_ascii=False, indent=2))
        elif args.command == "prepare":
            prepare(home)
        elif args.command == "restore":
            restore(home, args.plan.absolute())
        else:
            require_external_console()
            restore(home, prepare(home))
    except KeyboardInterrupt:
        print("STOPPED: interrupted. Keep any backup folders.", file=sys.stderr)
        return 130
    except (RecoveryError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"STOPPED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
