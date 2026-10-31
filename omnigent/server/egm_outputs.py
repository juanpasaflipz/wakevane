"""Durable Design review outputs that survive cloud sandbox deletion.

A cloud review writes only under ``docs/designs/egm`` (see :mod:`egm_cloud`).
:func:`publish_outputs` copies that directory out of the live sandbox into the
artifact store: content-addressed blobs first, then a manifest of relative
paths, sizes and SHA-256 hashes bound to the review's repository and commit.
A manifest is written only after every blob it names, so a reader never sees
a handoff whose bytes are missing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from omnigent.server.egm_cloud import (
    COMMIT_LABEL,
    OUTPUT_DIR,
    REPOSITORY_LABEL,
    pinned_commit,
)
from omnigent.stores.artifact_store import ArtifactStore

_logger = logging.getLogger(__name__)

MAX_FILES = 200
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 5_000_000
MANIFEST_VERSION = 1
_PREFIX = "egm-outputs"

# Runs inside the sandbox. Refuses symlinks and special files rather than
# following them, so nothing outside the output directory can be exported.
_COLLECTOR_SOURCE = r"""
import base64, hashlib, json, os, stat, sys
root = os.path.join(sys.argv[1], sys.argv[2])
max_files, max_file, max_total = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
files, errors, total = [], [], 0
if os.path.islink(root) or not os.path.isdir(root):
    errors.append("output directory is missing or is a symlink")
else:
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = os.path.join(current, name)
            rel = os.path.relpath(path, root)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                errors.append("symlink: " + rel)
            elif stat.S_ISDIR(info.st_mode):
                continue
            elif not stat.S_ISREG(info.st_mode):
                errors.append("not a regular file: " + rel)
            elif info.st_size > max_file:
                errors.append("file too large: " + rel)
            elif len(files) >= max_files:
                errors.append("too many files")
            elif total + info.st_size > max_total:
                errors.append("outputs too large")
            else:
                with open(path, "rb") as handle:
                    data = handle.read(max_file + 1)
                total += len(data)
                files.append({"path": rel, "size": len(data),
                              "sha256": hashlib.sha256(data).hexdigest(),
                              "content": base64.b64encode(data).decode()})
print(json.dumps({"files": files, "errors": errors}))
"""


class OutputPublicationError(Exception):
    """Outputs could not be exported safely; nothing new was published."""


@dataclass(frozen=True)
class OutputFile:
    """One exported file: its path under the output directory and its bytes."""

    path: str
    sha256: str
    data: bytes


def collector_command(workspace: str) -> str:
    """Shell command printing the output directory's files as one JSON line."""
    return shlex.join(
        [
            "python3",
            "-c",
            _COLLECTOR_SOURCE,
            workspace,
            OUTPUT_DIR,
            str(MAX_FILES),
            str(MAX_FILE_BYTES),
            str(MAX_TOTAL_BYTES),
        ]
    )


def _safe_relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise OutputPublicationError("The review produced an invalid file name.")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise OutputPublicationError(f"The review produced an unsafe path: {raw!r}")
    return str(path)


def parse_collected(stdout: str) -> list[OutputFile]:
    """Validate the collector's report; raise rather than export a partial set."""
    report: dict[str, Any] | None = None
    # Providers may merge stderr into stdout; the report is the last JSON object.
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "files" in parsed:
            report = parsed
            break
    if report is None:
        raise OutputPublicationError("The cloud workspace did not report its review outputs.")
    errors = report.get("errors") or []
    if errors:
        raise OutputPublicationError("Review outputs were not exported: " + "; ".join(errors[:5]))
    entries = report["files"]
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise OutputPublicationError("The review produced too many files.")
    files: list[OutputFile] = []
    seen: set[str] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise OutputPublicationError("The cloud workspace sent a malformed report.")
        path = _safe_relative_path(entry.get("path"))
        try:
            data = base64.b64decode(entry.get("content", ""), validate=True)
        except ValueError as exc:
            raise OutputPublicationError(f"Could not decode {path}.") from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry.get("sha256") or len(data) != entry.get("size"):
            raise OutputPublicationError(f"{path} changed while it was exported.")
        total += len(data)
        if path in seen or len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise OutputPublicationError("Review outputs exceed the export limits.")
        seen.add(path)
        files.append(OutputFile(path=path, sha256=digest, data=data))
    return sorted(files, key=lambda item: item.path)


def _manifest_key(root_session_id: str) -> str:
    return f"{_PREFIX}/{root_session_id}/manifest.json"


def _blob_key(root_session_id: str, sha256: str) -> str:
    return f"{_PREFIX}/{root_session_id}/blobs/{sha256}"


def read_manifest(store: ArtifactStore, root_session_id: str) -> dict[str, Any] | None:
    """The latest published manifest for a review, or ``None``."""
    key = _manifest_key(root_session_id)
    if not store.exists(key):
        return None
    return json.loads(store.get(key))


def read_output(store: ArtifactStore, root_session_id: str, path: str) -> tuple[bytes, str] | None:
    """A published file's bytes and hash, verified against the manifest."""
    manifest = read_manifest(store, root_session_id)
    if manifest is None:
        return None
    entry = next((item for item in manifest["files"] if item["path"] == path), None)
    if entry is None:
        return None
    data = store.get(_blob_key(root_session_id, entry["sha256"]))
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise OutputPublicationError(f"Stored bytes for {path} do not match their hash.")
    return data, entry["sha256"]


def store_outputs(
    store: ArtifactStore,
    *,
    root_session_id: str,
    labels: dict[str, str],
    files: list[OutputFile],
    reason: str,
) -> dict[str, Any]:
    """Write blobs then the manifest; unchanged outputs keep the existing manifest."""
    entries = [
        {"path": item.path, "size": len(item.data), "sha256": item.sha256} for item in files
    ]
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    current = read_manifest(store, root_session_id)
    if current is not None and current.get("digest") == digest:
        return current
    for item in files:
        key = _blob_key(root_session_id, item.sha256)
        if not store.exists(key):
            store.put(key, item.data)
    manifest = {
        "version": MANIFEST_VERSION,
        "session_id": root_session_id,
        "repository": labels.get(REPOSITORY_LABEL),
        "commit": labels.get(COMMIT_LABEL),
        "checkpoint": (current or {}).get("checkpoint", 0) + 1,
        "published_at": int(time.time()),
        "reason": reason,
        "digest": digest,
        "files": entries,
    }
    store.put(_manifest_key(root_session_id), json.dumps(manifest, sort_keys=True).encode())
    return manifest


def publish_outputs(
    launcher: Any,
    sandbox_id: str,
    *,
    store: ArtifactStore,
    root_session_id: str,
    workspace: str,
    labels: dict[str, str],
    reason: str,
) -> dict[str, Any]:
    """Export a live cloud review's outputs; blocking, call from a worker thread."""
    import click

    try:
        result = launcher.run(sandbox_id, collector_command(workspace), check=False)
    except click.ClickException as exc:
        raise OutputPublicationError("The cloud workspace could not be read.") from exc
    files = parse_collected(result.stdout)
    return store_outputs(
        store, root_session_id=root_session_id, labels=labels, files=files, reason=reason
    )


def cloud_review_roots(conversation_store: Any, host_id: str) -> list[Any]:
    """Root cloud review sessions bound to *host_id*, newest first."""
    roots: dict[str, Any] = {}
    after: str | None = None
    while True:
        ids = conversation_store.list_host_conversation_ids(host_id, after=after, limit=32)
        for session_id in ids:
            conversation = conversation_store.get_conversation(session_id)
            if conversation is None or pinned_commit(conversation.labels) is None:
                continue
            root_id = conversation.root_conversation_id or conversation.id
            if root_id == conversation.id and conversation.workspace:
                roots[root_id] = conversation
        if len(ids) < 32:
            return list(roots.values())
        after = ids[-1]


class OutputPublisher:
    """Checkpoints every cloud review on a managed host into durable storage."""

    def __init__(self, *, store: ArtifactStore, conversation_store: Any) -> None:
        self._store = store
        self._conversation_store = conversation_store

    def __call__(self, launcher: Any, host: Any, reason: str) -> bool:
        """Publish each review on *host*; ``False`` if any publication failed."""
        succeeded = True
        for root in cloud_review_roots(self._conversation_store, host.host_id):
            try:
                publish_outputs(
                    launcher,
                    host.sandbox_id,
                    store=self._store,
                    root_session_id=root.id,
                    workspace=root.workspace,
                    labels=root.labels,
                    reason=reason,
                )
            except Exception:
                succeeded = False
                _logger.exception(
                    "Could not publish Design review outputs for session %s", root.id
                )
        return succeeded
