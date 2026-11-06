"""Runner-owned review evidence. Markdown verdicts are not readiness authority."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

POLICY = "omnigent.policies.builtins.orchestration.egm_review_protocol"
STAGES = ("comprehension", "critic", "readiness")


def enabled(spec) -> bool:
    return bool(
        spec
        and spec.guardrails
        and any(p.function and p.function.path == POLICY for p in spec.guardrails.policies)
    )


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate field: {key}")
        result[key] = value
    return result


class _UniqueYamlLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        return _unique_pairs(
            (self.construct_object(key, deep=deep), self.construct_object(value, deep=deep))
            for key, value in node.value
        )


def _yaml(text: str):
    return yaml.load(text, Loader=_UniqueYamlLoader)


_YAML_FENCE = re.compile(r"^```yaml\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


def _report_text(output: str) -> str:
    """Return the reviewer's single fenced YAML report, or the bare message.

    Prose around one fenced block is ignored; several blocks are ambiguous.
    """
    blocks = _YAML_FENCE.findall(output)
    if len(blocks) > 1:
        raise ValueError("Report must contain exactly one yaml block")
    return blocks[0] if blocks else output.strip()


def parse_packet(text: str):
    return Packet.model_validate(json.loads(text, object_pairs_hook=_unique_pairs))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


BASELINE_EXCLUDE = ":(exclude)docs/designs/egm"
# Reviewers reproduce the baseline from this text; it is not the hash of any file or git object.
BASELINE_METHOD = (
    "sha256 over three byte strings concatenated: (1) `git status --porcelain=v1 -z "
    "--untracked-files=all -- . ':(exclude)docs/designs/egm'`, (2) `git diff --cached "
    "--binary -- . ':(exclude)docs/designs/egm'`, (3) canonical JSON (sorted keys, "
    "separators ',' and ':') of baseline_dirty_files, which maps each modified or "
    "untracked path to the sha256 of its bytes or 'deleted'. A clean checkout yields "
    "sha256 of the two bytes '{}' = 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a."
)


class Packet(StrictModel):
    stage: Literal["comprehension", "critic", "readiness"]
    spec_path: str
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_path: str
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository: str
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    evidence: dict[str, str]
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Defaults keep revisions frozen before these fields existed loadable.
    baseline_method: str = BASELINE_METHOD
    baseline_dirty_files: dict[str, str] = Field(default_factory=dict)


class Decision(StrictModel):
    id: str = Field(min_length=1)
    status: Literal["resolved", "open"]
    material: bool
    evidence: str = Field(min_length=1)


class SpecMetadata(StrictModel):
    decisions: list[Decision]
    acceptance_corrections: list[Decision]


class Requirement(StrictModel):
    id: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)


class ContractMetadata(StrictModel):
    original_task: str = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)


class Check(Requirement):
    status: Literal["satisfied", "violated", "unknown"]
    evidence: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class Finding(StrictModel):
    id: str = Field(min_length=1)
    severity: Literal["blocking", "nonblocking"]
    kind: Literal["behavior", "acceptance", "editorial"]
    evidence: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    requested_change: str = Field(min_length=1)


class Report(StrictModel):
    stage: Literal["comprehension", "critic", "readiness"]
    spec_sha256: str
    contract_sha256: str
    source_revision: str
    verdict: Literal["pass", "revise", "blocked"]
    summary: str = Field(min_length=1)
    findings: list[Finding]
    requirement_checks: list[Check]
    execution_status: Literal["not_run"]
    limitations: list[str]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _metadata(data: bytes, model):
    text = data.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("Artifact requires YAML front matter; see EGM protocol documentation")
    return model.model_validate(_yaml(text[4:].split("\n---\n", 1)[0]))


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class ReviewLedger:
    """SQLite transactions serialize dispatch reservations across turns and processes."""

    def __init__(self, session: str, workspace: Path, *, root: Path | None = None):
        from omnigent.process_logging import data_dir

        if not session:
            raise ValueError("Review requires a session identity")
        self.workspace = workspace.resolve(strict=True)
        directory = root or data_dir() / "egm-reviews"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (digest(session.encode()) + ".sqlite3")
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS dispatches (title TEXT PRIMARY KEY, "
                "revision INTEGER NOT NULL, stage TEXT NOT NULL, child TEXT UNIQUE, result TEXT)"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _read(self, path: str, *, artifact=False) -> bytes:
        target = Path(path)
        if not target.is_absolute() or target.is_symlink():
            raise ValueError("Evidence paths must be absolute regular files")
        resolved = target.resolve(strict=True)
        parent = self.workspace / "docs/designs/egm" if artifact else self.workspace
        if not resolved.is_relative_to(parent) or not resolved.is_file():
            raise ValueError("Evidence path is outside its allowed root")
        if resolved.stat().st_size > 2_000_000:
            raise ValueError("Evidence file exceeds 2 MB")
        return resolved.read_bytes()

    def _git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", *args], cwd=self.workspace, check=True, capture_output=True, timeout=30
        ).stdout

    def _baseline(self) -> str:
        return self._baseline_parts()[0]

    def _baseline_parts(self) -> tuple[str, dict[str, str]]:
        """Worktree-state digest and the dirty-file inventory it covers.

        Includes index changes and all dirty/untracked bytes, excluding design
        outputs; see ``BASELINE_METHOD`` for the reviewer-facing formula.
        """
        status = self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".", BASELINE_EXCLUDE
        )
        names = self._git(
            "ls-files",
            "--modified",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            BASELINE_EXCLUDE,
        ).split(b"\0")
        files = {}
        for name in filter(None, names):
            path = self.workspace / name.decode("utf-8")
            files[str(path)] = digest(self._read(str(path))) if path.exists() else "deleted"
        cached = self._git("diff", "--cached", "--binary", "--", ".", BASELINE_EXCLUDE)
        return digest(status + cached + _json(files).encode()), files

    def freeze(self, spec_path: str, contract_path: str, evidence_paths: list[str]) -> dict:
        spec = self._read(spec_path, artifact=True)
        contract = self._read(contract_path, artifact=True)
        spec_path = str(Path(spec_path).resolve(strict=True))
        contract_path = str(Path(contract_path).resolve(strict=True))
        metadata = _metadata(spec, SpecMetadata)
        requirements = _metadata(contract, ContractMetadata)
        ids = [r.id for r in requirements.requirements]
        if len(ids) != len(set(ids)) or any(
            r.source_quote not in requirements.original_task for r in requirements.requirements
        ):
            raise ValueError("Requirements need unique IDs and verbatim original-task quotes")
        if (
            not isinstance(evidence_paths, list)
            or not evidence_paths
            or not all(
                isinstance(path, str)
                and Path(path).is_absolute()
                and not Path(path).resolve().is_relative_to(self.workspace / "docs/designs/egm")
                for path in evidence_paths
            )
        ):
            raise ValueError(
                "Evidence must be a nonempty list of absolute existing repository files "
                "outside docs/designs/egm. For new-file designs, use existing source, "
                "configuration or README files; do not create placeholder code/tests."
            )
        baseline, dirty_files = self._baseline_parts()
        packet = Packet(
            stage="comprehension",
            spec_path=spec_path,
            spec_sha256=digest(spec),
            contract_path=contract_path,
            contract_sha256=digest(contract),
            repository=str(self.workspace),
            source_revision=self._git("rev-parse", "HEAD").decode().strip(),
            evidence={p: digest(self._read(p)) for p in evidence_paths},
            baseline_sha256=baseline,
            baseline_dirty_files=dirty_files,
        )
        value = {
            "packet": packet.model_dump(),
            "spec": spec.decode(),
            "contract": contract.decode(),
            "metadata": metadata.model_dump(),
            "requirements": requirements.model_dump(),
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,data FROM revisions ORDER BY id").fetchall()
            for revision, raw in rows:
                old = json.loads(raw)
                if old == value:
                    return {"revision": revision, "packet": packet.model_dump()}
                for key, body in (("spec_path", "spec"), ("contract_path", "contract")):
                    if old["packet"][key] == value["packet"][key] and old[body] != value[body]:
                        raise ValueError("Frozen artifact changed: use a new versioned path")
            if len(rows) >= 3:
                raise ValueError("Three-revision limit reached")
            revision = len(rows) + 1
            db.execute("INSERT INTO revisions VALUES (?,?)", (revision, _json(value)))
        return {"revision": revision, "packet": packet.model_dump()}

    def _verify(self, frozen: dict):
        packet = Packet.model_validate(frozen["packet"])
        if packet.repository != str(self.workspace):
            raise ValueError("Workspace changed")
        for path, expected in [
            (packet.spec_path, packet.spec_sha256),
            (packet.contract_path, packet.contract_sha256),
            *packet.evidence.items(),
        ]:
            if digest(self._read(path)) != expected:
                raise ValueError("Frozen artifact or source evidence changed: " + path)
        if (
            self._git("rev-parse", "HEAD").decode().strip() != packet.source_revision
            or self._baseline() != packet.baseline_sha256
        ):
            raise ValueError("Repository baseline changed; freeze a new revision")

    def _blockers(self, frozen: dict) -> list[str]:
        metadata = SpecMetadata.model_validate(frozen["metadata"])
        return [d.id for d in metadata.decisions if d.material and d.status == "open"] + [
            d.id for d in metadata.acceptance_corrections if d.status == "open"
        ]

    def reserve(self, args: dict) -> str:
        if set(args) != {"agent", "title", "args"} or args["agent"] != "goldfish":
            raise ValueError(
                "Review dispatch requires a fresh named Goldfish; "
                "continuations and overrides are forbidden"
            )
        child_args = args["args"]
        if (
            not isinstance(child_args, dict)
            or set(child_args) != {"purpose", "input"}
            or child_args["purpose"] != "review"
        ):
            raise ValueError("Review dispatch accepts only purpose and the generated JSON packet")
        packet = parse_packet(child_args["input"])
        title = args["title"]
        if not isinstance(title, str) or not re.fullmatch(r"egm-[a-zA-Z0-9_-]{1,180}", title):
            raise ValueError("Invalid review title")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,data FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                raise ValueError("Freeze the artifacts with sys_egm_review first")
            revision, raw = row
            frozen = json.loads(raw)
            expected = {**frozen["packet"], "stage": packet.stage}
            if packet.model_dump() != expected:
                raise ValueError("Packet differs from the current frozen revision")
            self._verify(frozen)
            if db.execute("SELECT 1 FROM dispatches WHERE title=?", (title,)).fetchone():
                raise ValueError("Review title already used; use a fresh reviewer")
            if db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] >= 9:
                raise ValueError("Nine-dispatch limit reached")
            if db.execute("SELECT 1 FROM dispatches WHERE result IS NULL").fetchone():
                raise ValueError("A review dispatch is still pending")
            passed = self._passed(db, revision)
            index = STAGES.index(packet.stage)
            missing = [stage for stage in STAGES[:index] if stage not in passed]
            if missing:
                raise ValueError(
                    f"Prior stages have not passed this revision: {missing[0]}. "
                    "Redispatch it with the next attempt title only if its latest review "
                    "errored; a revise verdict needs a new revision"
                )
            if packet.stage == "readiness" and self._blockers(frozen):
                raise ValueError(
                    "Unresolved material decisions or acceptance corrections: "
                    + ", ".join(self._blockers(frozen))
                )
            db.execute(
                "INSERT INTO dispatches(title,revision,stage) VALUES (?,?,?)",
                (title, revision, packet.stage),
            )
        return title

    def bind(self, title: str, output: str):
        try:
            child = json.loads(output).get("conversation_id")
        except (ValueError, AttributeError):
            child = None
        if not isinstance(child, str) or not child:
            child = None
        with self._connect() as db:
            db.execute(
                "UPDATE dispatches SET child=?,result=? WHERE title=?",
                (
                    child,
                    None if child else _json({"passed": False, "error": "dispatch failed"}),
                    title,
                ),
            )

    def record(self, payload: dict):
        if payload.get("type") != "sub_agent":
            return
        child = (
            payload.get("conversation_id") or payload.get("task_id") or payload.get("handle_id")
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT title,revision,stage,result FROM dispatches WHERE child=?", (child,)
            ).fetchone()
            if row is None:
                if db.execute(
                    "SELECT 1 FROM dispatches WHERE title=? AND child IS NULL AND result IS NULL",
                    (payload.get("title"),),
                ).fetchone():
                    raise ValueError("Review launch is not bound yet; retain the inbox result")
                return
            if row[3] is not None:
                return
            title, revision, stage, _ = row
            frozen = json.loads(
                db.execute("SELECT data FROM revisions WHERE id=?", (revision,)).fetchone()[0]
            )
            result = {
                "passed": False,
                "output": payload.get("output"),
                "status": payload.get("status"),
            }
            try:
                if payload.get("status") != "completed":
                    raise ValueError("Reviewer did not complete successfully")
                self._verify(frozen)
                report = Report.model_validate(_yaml(_report_text(payload.get("output", ""))))
                packet = frozen["packet"]
                if report.stage != stage or any(
                    getattr(report, key) != packet[key]
                    for key in ("spec_sha256", "contract_sha256", "source_revision")
                ):
                    raise ValueError("Review identity mismatch")
                expected = {
                    r["id"]: r["source_quote"] for r in frozen["requirements"]["requirements"]
                }
                actual = {r.id: r.source_quote for r in report.requirement_checks}
                if expected != actual or len(actual) != len(report.requirement_checks):
                    raise ValueError("Missing, duplicate or mismatched requirement checks")
                result["passed"] = (
                    report.verdict == "pass"
                    and all(c.status == "satisfied" for c in report.requirement_checks)
                    and not any(
                        f.severity == "blocking" or f.kind != "editorial" for f in report.findings
                    )
                    and not report.limitations
                )
                result["report"] = report.model_dump()
            except (
                ValueError,
                TypeError,
                AttributeError,
                OSError,
                yaml.YAMLError,
                subprocess.SubprocessError,
            ) as exc:
                result["error"] = str(exc)
            db.execute("UPDATE dispatches SET result=? WHERE title=?", (_json(result), title))

    @staticmethod
    def _passed(db, revision: int) -> set[str]:
        # A later failed attempt supersedes an earlier pass for the same stage.
        results = {}
        for stage, raw in db.execute(
            "SELECT stage,result FROM dispatches WHERE revision=? ORDER BY rowid", (revision,)
        ):
            results[stage] = bool(raw and json.loads(raw)["passed"])
        return {stage for stage, passed in results.items() if passed}

    def status(self) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,data FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return {"state": "drafting", "ready": False}
            revision, raw = row
            frozen = json.loads(raw)
            blockers = self._blockers(frozen)
            try:
                self._verify(frozen)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                blockers.append(str(exc))
            passed = self._passed(db, revision)
            missing = [stage for stage in STAGES if stage not in passed]
            ready = not blockers and not missing
            return {
                "state": "ready" if ready else "blocked",
                "ready": ready,
                "revision": revision,
                "packet": frozen["packet"],
                "blockers": blockers,
                "missing_stages": missing,
                "dispatches": db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0],
                "reviews": [
                    {
                        "title": title,
                        "stage": stage,
                        "child": child,
                        "passed": bool(result and json.loads(result)["passed"]),
                        "error": json.loads(result).get("error") if result else "pending",
                    }
                    for title, stage, child, result in db.execute(
                        "SELECT title,stage,child,result FROM dispatches "
                        "WHERE revision=? ORDER BY rowid",
                        (revision,),
                    )
                ],
                "execution_status": "not_run",
                "approval": "pending",
            }


class ReviewOutputGate:
    """Keep coordinator chat status runtime-owned, including its live stream.

    Tool events and reviewer reports remain intact. Coordinator prose is kept
    in design artifacts; freeform text is never used as the completion verdict.
    """

    def __init__(self, session: str, workspace: Path):
        self.session = session
        self.workspace = workspace

    @staticmethod
    def suppresses(event: dict) -> bool:
        """Drop every assistant-text surface before live publication/persistence."""
        kind = event.get("type", "")
        if kind.startswith(("response.output_text.", "response.content_part.")):
            return True
        return (
            kind.startswith("response.output_item.")
            and (event.get("item") or {}).get("type") == "message"
        )

    def completion(self) -> str:
        """Recheck the ledger and artifact hashes at completion; fail closed."""
        try:
            status = ReviewLedger(self.session, self.workspace).status()
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            return "Design review status unavailable. Readiness has not been established."
        if status["ready"]:
            return (
                f"Design review ready for implementation: revision {status['revision']}. "
                "All three review stages passed against the unchanged artifacts and repository. "
                "Implementation acceptance tests have not run. Approval remains pending."
            )
        if status["state"] == "drafting":
            return (
                "Design review incomplete: no revision has been frozen. Runtime ready=false. "
                "Use existing repository evidence and complete the three review stages. "
                "Exported drafts have not passed review."
            )
        pending = [r["stage"] for r in status["reviews"] if r["error"] == "pending"]
        if pending and not status["blockers"]:
            return (
                "Design review in progress. Runtime ready=false. Waiting for "
                + ", ".join(pending)
                + " review."
            )
        return (
            "Design review incomplete. Runtime ready=false. "
            f"Unresolved blockers: {len(status['blockers'])}. "
            "Review stages still required: "
            + (
                ", ".join(status["missing_stages"])
                or "none; resolve the artifact/decision blockers"
            )
            + ". Exported drafts are not approved for implementation."
        )

    @staticmethod
    def terminal(event: dict, text: str) -> dict:
        """Remove provider prose also carried in a terminal response payload."""
        response = dict(event.get("response") or {})
        if "output" in response:
            response["output"] = [
                item for item in response["output"] if item.get("type") != "message"
            ]
        if "output_text" in response:
            response["output_text"] = text
        return {**event, "response": response}
