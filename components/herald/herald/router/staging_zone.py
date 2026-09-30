"""Transactional staging and three-way application of agent patches.

Patches are kept in SQLite until they can be applied without colliding with
another pending agent pass.  Merges are first performed in a detached Git
worktree and tested there, so a conflict or failing test never changes the
main working tree.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence


DEFAULT_DB_PATH = Path.home() / ".herald" / "staging_patches.db"
PATCH_STATUSES = {"pending_merge", "merged", "conflict", "rejected"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    while current.parent != current:
        if (current / ".git").exists() or (current / "pyproject.toml").exists():
            return current
        current = current.parent
    raise ValueError("staging zone requires a Git repository")


def _normalise_files(files: Sequence[str]) -> list[str]:
    normalised: list[str] = []
    for raw in files:
        value = str(raw).strip().replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        if not value or value.startswith("/") or ".." in Path(value).parts:
            raise ValueError(f"invalid repository-relative path: {raw!r}")
        if value not in normalised:
            normalised.append(value)
    if not normalised:
        raise ValueError("files_touched must contain at least one path")
    return normalised


@dataclass(frozen=True)
class StagedPatch:
    id: str
    agent_name: str
    task_id: str | None
    files_touched: list[str]
    diff_content: str
    status: str
    created_at: str
    merged_at: str | None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "task_id": self.task_id,
            "files_touched": self.files_touched,
            "diff_content": self.diff_content,
            "status": self.status,
            "created_at": self.created_at,
            "merged_at": self.merged_at,
        }


class StagingStore:
    """Persistent patch buffer backed by a disposable Git merge worktree."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        *,
        repo_path: str | Path | None = None,
        test_command: Sequence[str] | None = None,
        test_timeout_seconds: float = 300.0,
        conflict_resolver: Callable[[Path, Sequence[StagedPatch], str], dict[str, Any]] | None = None,
        reviewer: Callable[[Path, Sequence[StagedPatch]], dict[str, Any]] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.repo_path = _repo_root(Path(repo_path) if repo_path else None)
        self.test_command = tuple(test_command or (sys.executable, "-m", "pytest", "-q"))
        # A synthesized patch can easily introduce a hang (an infinite loop,
        # a test that blocks on network/input) -- without a timeout here,
        # that silently freezes the whole merge forever with no recovery.
        # Treated the same as a failing test: feeds into the reviewer-repair
        # retry where one exists, a clean "rejected"/"deferred" result where
        # it doesn't.
        self.test_timeout_seconds = test_timeout_seconds
        self.conflict_resolver = conflict_resolver
        self.reviewer = reviewer
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS patches (
                    id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    task_id TEXT,
                    files_touched_json TEXT NOT NULL,
                    diff_content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending_merge', 'merged', 'conflict', 'rejected')
                    ),
                    created_at TEXT NOT NULL,
                    merged_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS patches_status_idx ON patches(status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviewed_batches (
                    id TEXT PRIMARY KEY,
                    patch_ids_json TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    reviewed_commit TEXT NOT NULL,
                    reviewed_ref TEXT NOT NULL,
                    changed_paths_json TEXT NOT NULL,
                    diff_content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('deferred', 'merged')),
                    test_output TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    merged_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS reviewed_batches_status_idx ON reviewed_batches(status)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def submit_patch(
        self,
        agent_name: str,
        diff_content: str,
        files_touched: Sequence[str],
        *,
        task_id: str | None = None,
    ) -> str:
        agent_name = agent_name.strip()
        if not agent_name:
            raise ValueError("agent_name is required")
        if not diff_content.strip():
            raise ValueError("diff_content is required")
        files = _normalise_files(files_touched)
        patch_id = uuid.uuid4().hex
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO patches (id,agent_name,task_id,files_touched_json,diff_content,status,created_at) "
                "VALUES (?,?,?,?,?,'pending_merge',?)",
                (patch_id, agent_name, task_id, json.dumps(files), diff_content, _now()),
            )
        return patch_id

    def get_patch(self, patch_id: str) -> StagedPatch | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM patches WHERE id=?", (patch_id,)).fetchone()
        return self._row(row) if row else None

    def list_patches(self, *, status: str | None = None, limit: int = 100) -> list[StagedPatch]:
        if status is not None and status not in PATCH_STATUSES:
            raise ValueError(f"unknown patch status: {status}")
        limit = max(1, min(int(limit), 1000))
        query = "SELECT * FROM patches"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def detect_collisions(self, patch_id: str) -> list[str]:
        patch = self._require(patch_id)
        if patch.status != "pending_merge":
            return []
        touched = set(patch.files_touched)
        return [
            candidate.id
            for candidate in self.list_patches(status="pending_merge", limit=1000)
            if candidate.id != patch_id and touched.intersection(candidate.files_touched)
        ]

    def apply_or_stage_patch(self, patch_id: str) -> dict[str, Any]:
        patch = self._require(patch_id)
        if patch.status != "pending_merge":
            raise ValueError(f"patch {patch_id} is already {patch.status}")
        collisions = self.detect_collisions(patch_id)
        if collisions:
            return {
                "ok": True,
                "applied": False,
                "status": "pending_merge",
                "patch_ids": [patch_id],
                "collisions": collisions,
            }
        return self.auto_merge_patches([patch_id])

    def auto_merge_patches(self, patch_ids: Sequence[str]) -> dict[str, Any]:
        """Three-way merge and test patches before applying them to the main tree."""
        ids = list(dict.fromkeys(str(value) for value in patch_ids))
        if not ids:
            raise ValueError("patch_ids must contain at least one patch id")
        patches = [self._require(patch_id) for patch_id in ids]
        inactive = [patch.id for patch in patches if patch.status != "pending_merge"]
        if inactive:
            raise ValueError(f"patches are not pending: {', '.join(inactive)}")

        temp_root = Path(tempfile.mkdtemp(prefix="herald-staging-"))
        worktree = temp_root / "worktree"
        added = False
        try:
            # Force LF in the disposable checkout.  A system-wide autocrlf=true
            # otherwise makes unified diffs authored from Git blobs fail to
            # apply on Windows even though their blob ids are correct.
            created = self._git(
                "-c", "core.autocrlf=false",
                "worktree", "add", "--detach", str(worktree), "HEAD",
            )
            if created.returncode:
                return self._failure(ids, "conflict", "could not create merge worktree", created)
            added = True

            base_result = self._git("rev-parse", "HEAD", cwd=worktree)
            if base_result.returncode:
                return self._failure(ids, "conflict", "could not resolve merge base", base_result)
            base_commit = base_result.stdout.strip()
            patch_commits: list[tuple[StagedPatch, str]] = []

            # Each submitted diff was authored against the same main-tree base.
            # Materialise each one as its own commit before merging those commits;
            # applying all diffs sequentially would make the index cease to match
            # that base after the first patch.
            for patch in patches:
                reset = self._git("reset", "--hard", base_commit, cwd=worktree)
                if reset.returncode:
                    return self._failure(ids, "conflict", "could not reset merge worktree", reset)
                applied = self._git(
                    "apply", "--3way", "--whitespace=nowarn", "-",
                    cwd=worktree, input_text=patch.diff_content,
                )
                if applied.returncode:
                    applied = self._git(
                        "apply", "--whitespace=nowarn", "-",
                        cwd=worktree, input_text=patch.diff_content,
                    )
                if applied.returncode:
                    return self._failure(ids, "conflict", f"patch application failed for {patch.id}", applied)

                staged = self._git("add", "-A", cwd=worktree)
                if staged.returncode:
                    return self._failure(ids, "conflict", "could not stage agent patch", staged)
                committed = self._git(
                    "-c", "user.name=Herald Reviewer",
                    "-c", "user.email=reviewer@herald.local",
                    "commit", "-m", f"staged patch {patch.id}", cwd=worktree,
                )
                if committed.returncode:
                    return self._failure(ids, "conflict", f"patch {patch.id} produced no commit", committed)
                commit = self._git("rev-parse", "HEAD", cwd=worktree)
                if commit.returncode:
                    return self._failure(ids, "conflict", "could not resolve patch commit", commit)
                patch_commits.append((patch, commit.stdout.strip()))

            reset = self._git("reset", "--hard", base_commit, cwd=worktree)
            if reset.returncode:
                return self._failure(ids, "conflict", "could not initialise reviewed merge", reset)
            for patch, commit in patch_commits:
                merged = self._git(
                    "-c", "user.name=Herald Reviewer",
                    "-c", "user.email=reviewer@herald.local",
                    "merge", "--no-ff", "--no-edit", commit, cwd=worktree,
                )
                if merged.returncode:
                    if self.conflict_resolver is None:
                        return self._failure(ids, "conflict", f"merge conflict for {patch.id}", merged)
                    resolution = self.conflict_resolver(
                        worktree, patches, (merged.stderr or merged.stdout).strip()[-4000:],
                    )
                    if resolution.get("ok"):
                        self._git("add", "-A", cwd=worktree)
                    unresolved = self._git("diff", "--name-only", "--diff-filter=U", cwd=worktree)
                    if not resolution.get("ok") or unresolved.returncode or unresolved.stdout.strip():
                        detail = subprocess.CompletedProcess(
                            merged.args, 1,
                            str(resolution.get("content") or unresolved.stdout or ""),
                            str(resolution.get("error") or "reviewer left unresolved merge conflicts"),
                        )
                        return self._failure(ids, "conflict", f"reviewer could not resolve merge conflict for {patch.id}", detail)
                    resolved_commit = self._git(
                        "-c", "user.name=Herald Reviewer",
                        "-c", "user.email=reviewer@herald.local",
                        "commit", "--no-edit", cwd=worktree,
                    )
                    if resolved_commit.returncode:
                        return self._failure(ids, "conflict", "could not commit reviewer conflict resolution", resolved_commit)

            if self.reviewer is not None:
                review = self.reviewer(worktree, patches)
                if not review.get("ok"):
                    detail = subprocess.CompletedProcess(
                        ("reviewer",), 1, str(review.get("content") or ""), str(review.get("error") or "review failed"),
                    )
                    return self._failure(ids, "rejected", "reviewer rejected synthesized patch", detail)
                self._git("add", "-A", cwd=worktree)
                review_changes = self._git("diff", "--cached", "--quiet", cwd=worktree)
                if review_changes.returncode == 1:
                    review_commit = self._git(
                        "-c", "user.name=Herald Reviewer",
                        "-c", "user.email=reviewer@herald.local",
                        "commit", "-m", "review synthesized swarm patch", cwd=worktree,
                    )
                    if review_commit.returncode:
                        return self._failure(ids, "conflict", "could not commit reviewer adjustments", review_commit)
                elif review_changes.returncode:
                    return self._failure(ids, "conflict", "could not inspect reviewer adjustments", review_changes)

            tested = self._run_test_command(worktree)
            if tested.returncode and self.reviewer is not None:
                failure_output = (tested.stdout + "\n" + tested.stderr)[-6000:]
                try:
                    repair = self.reviewer(worktree, patches, failure_output)
                except TypeError:
                    repair = self.reviewer(worktree, patches)
                if repair.get("ok"):
                    self._git("add", "-A", cwd=worktree)
                    repair_changes = self._git("diff", "--cached", "--quiet", cwd=worktree)
                    if repair_changes.returncode == 1:
                        repaired_commit = self._git(
                            "-c", "user.name=Herald Reviewer", "-c", "user.email=reviewer@herald.local",
                            "commit", "-m", "repair synthesized swarm verification", cwd=worktree,
                        )
                        if repaired_commit.returncode:
                            return self._failure(ids, "conflict", "could not commit verification repair", repaired_commit)
                    tested = self._run_test_command(worktree)
            if tested.returncode:
                return self._failure(ids, "rejected", "verification still fails after reviewer repair", tested)

            combined = self._git("diff", "--binary", base_commit, "HEAD", cwd=worktree)
            if combined.returncode or not combined.stdout.strip():
                return self._failure(ids, "conflict", "could not create merged diff", combined)
            changed = self._git("diff", "--name-only", base_commit, "HEAD", cwd=worktree)
            changed_paths = [line.strip() for line in changed.stdout.splitlines() if line.strip()]
            if changed.returncode or not changed_paths:
                return self._failure(ids, "conflict", "could not identify merged paths", changed)

            reviewed = self._git("rev-parse", "HEAD", cwd=worktree)
            if reviewed.returncode:
                return self._failure(ids, "conflict", "could not preserve reviewed commit", reviewed)
            batch_id = uuid.uuid4().hex
            reviewed_commit = reviewed.stdout.strip()
            reviewed_ref = f"refs/heads/herald/reviewed/{batch_id}"
            preserved = self._git("update-ref", reviewed_ref, reviewed_commit)
            if preserved.returncode:
                return self._failure(ids, "conflict", "could not preserve reviewed branch", preserved)

            # A worker patch may be based on HEAD while the user's primary
            # worktree contains unrelated edits.  Those edits are allowed,
            # but a dirty path that this merge also touches is ambiguous and
            # must remain untouched for a later retry.
            dirty_targets = self._git(
                "status", "--porcelain=v1", "--untracked-files=all", "--", *changed_paths,
            )
            if dirty_targets.returncode or dirty_targets.stdout.strip():
                self._record_reviewed_batch(
                    batch_id=batch_id,
                    patch_ids=ids,
                    base_commit=base_commit,
                    reviewed_commit=reviewed_commit,
                    reviewed_ref=reviewed_ref,
                    changed_paths=changed_paths,
                    diff_content=combined.stdout,
                    test_output=tested.stdout[-4000:],
                )
                return {
                    "ok": True,
                    "applied": False,
                    "status": "deferred",
                    "patch_ids": ids,
                    "batch_id": batch_id,
                    "reviewed_ref": reviewed_ref.removeprefix("refs/heads/"),
                    "blocked_paths": [
                        line[3:].strip().replace("\\", "/")
                        for line in dirty_targets.stdout.splitlines() if line.strip()
                    ],
                    "test_output": tested.stdout[-4000:],
                }

            final = self._git(
                "apply", "--index", "--whitespace=nowarn", "-",
                input_text=combined.stdout,
            )
            if final.returncode:
                final = self._git(
                    "apply", "--3way", "--index", "--whitespace=nowarn", "-",
                    input_text=combined.stdout,
                )
            if final.returncode:
                return self._failure(ids, "conflict", "could not apply reviewed merge", final)

            # Commit only the paths synthesized in the review worktree.  In
            # particular, never `git add -A`: the primary tree may contain
            # unrelated user work and it does not belong to this swarm.
            agent_names = ", ".join(dict.fromkeys(p.agent_name for p in patches))
            commit_msg = f"feat(merge): review and merge parallel passes from [{agent_names}]\n\nMerged patches: {', '.join(ids)}"
            committed = self._git(
                "-c", "user.name=Herald Reviewer",
                "-c", "user.email=reviewer@herald.local",
                "commit", "--only", "-m", commit_msg, "--", *changed_paths,
            )
            if committed.returncode:
                # The paths were clean before apply, so reversing this exact
                # patch restores them without disturbing unrelated work.
                self._git("apply", "--reverse", "--index", "-", input_text=combined.stdout)
                return self._failure(ids, "conflict", "could not commit reviewed merge", committed)

            merged_at = _now()
            self._set_status(ids, "merged", merged_at=merged_at)
            self._git("update-ref", "-d", reviewed_ref)
            return {
                "ok": True,
                "applied": True,
                "status": "merged",
                "patch_ids": ids,
                "collisions": self._collisions_within(patches),
                "test_output": tested.stdout[-4000:],
            }
        finally:
            if added:
                self._git("worktree", "remove", "--force", str(worktree))
            try:
                temp_root.rmdir()
            except OSError:
                pass

    def list_deferred_batches(self) -> list[dict[str, Any]]:
        """Return reviewed bundles waiting for a safe primary-tree promotion."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM reviewed_batches WHERE status='deferred' ORDER BY created_at ASC"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "patch_ids": json.loads(row["patch_ids_json"]),
                "base_commit": row["base_commit"],
                "reviewed_commit": row["reviewed_commit"],
                "reviewed_ref": row["reviewed_ref"].removeprefix("refs/heads/"),
                "changed_paths": json.loads(row["changed_paths_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def reconcile_deferred(self) -> dict[str, Any]:
        """Promote reviewed bundles when their target paths become safe.

        A dirty target remains untouched.  Promotion is first rebased and
        tested in another disposable worktree, so a later change to main can
        never turn a previously reviewed patch into an unsafe primary-tree
        mutation.
        """
        promoted: list[str] = []
        blocked: list[dict[str, Any]] = []
        for batch in self.list_deferred_batches():
            paths = batch["changed_paths"]
            dirty = self._git("status", "--porcelain=v1", "--untracked-files=all", "--", *paths)
            if dirty.returncode or dirty.stdout.strip():
                blocked.append({"batch_id": batch["id"], "reason": "dirty target paths"})
                continue
            result = self._promote_deferred_batch(batch)
            if result.get("status") == "merged":
                promoted.append(batch["id"])
            else:
                blocked.append({"batch_id": batch["id"], "reason": result.get("error", "promotion failed")})
        return {"status": "merged" if promoted else "deferred" if blocked else "idle", "promoted": promoted, "blocked": blocked}

    def _promote_deferred_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT diff_content FROM reviewed_batches WHERE id=?", (batch["id"],)).fetchone()
        if row is None:
            return {"status": "conflict", "error": "reviewed batch disappeared"}
        temp_root = Path(tempfile.mkdtemp(prefix="herald-reconcile-"))
        worktree = temp_root / "worktree"
        added = False
        try:
            base = self._git("rev-parse", "HEAD")
            if base.returncode:
                return {"status": "conflict", "error": "could not resolve current main"}
            base_commit = base.stdout.strip()
            created = self._git("-c", "core.autocrlf=false", "worktree", "add", "--detach", str(worktree), base_commit)
            if created.returncode:
                return {"status": "conflict", "error": "could not create reconciliation worktree"}
            added = True
            applied = self._git("apply", "--3way", "--whitespace=nowarn", "-", cwd=worktree, input_text=row["diff_content"])
            if applied.returncode:
                return {"status": "deferred", "error": "reviewed bundle needs conflict resolution against newer main"}
            self._git("add", "-A", cwd=worktree)
            tested = self._run_test_command(worktree)
            if tested.returncode:
                return {"status": "deferred", "error": "reviewed bundle no longer passes tests against newer main"}
            rebased = self._git("diff", "--binary", base_commit, cwd=worktree)
            if rebased.returncode or not rebased.stdout.strip():
                return {"status": "deferred", "error": "reconciliation produced no diff"}
            # Recheck both HEAD and paths immediately before touching primary.
            current = self._git("rev-parse", "HEAD")
            dirty = self._git("status", "--porcelain=v1", "--untracked-files=all", "--", *batch["changed_paths"])
            if current.stdout.strip() != base_commit or dirty.returncode or dirty.stdout.strip():
                return {"status": "deferred", "error": "primary tree changed during reconciliation"}
            final = self._git("apply", "--index", "--whitespace=nowarn", "-", input_text=rebased.stdout)
            if final.returncode:
                return {"status": "deferred", "error": "could not apply rebased reviewed bundle"}
            commit_msg = f"feat(merge): promote reviewed Herald batch {batch['id']}"
            committed = self._git(
                "-c", "user.name=Herald Reviewer", "-c", "user.email=reviewer@herald.local",
                "commit", "--only", "-m", commit_msg, "--", *batch["changed_paths"],
            )
            if committed.returncode:
                self._git("apply", "--reverse", "--index", "-", input_text=rebased.stdout)
                return {"status": "deferred", "error": "could not commit rebased reviewed bundle"}
            merged_at = _now()
            self._set_status(batch["patch_ids"], "merged", merged_at=merged_at)
            self._set_batch_status(batch["id"], "merged", merged_at=merged_at)
            self._git("update-ref", "-d", f"refs/heads/{batch['reviewed_ref']}")
            return {"status": "merged", "batch_id": batch["id"], "test_output": tested.stdout[-4000:]}
        finally:
            if added:
                self._git("worktree", "remove", "--force", str(worktree))
            try:
                temp_root.rmdir()
            except OSError:
                pass

    def _record_reviewed_batch(
        self, *, batch_id: str, patch_ids: Sequence[str], base_commit: str,
        reviewed_commit: str, reviewed_ref: str, changed_paths: Sequence[str],
        diff_content: str, test_output: str,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO reviewed_batches
                   (id,patch_ids_json,base_commit,reviewed_commit,reviewed_ref,changed_paths_json,
                    diff_content,status,test_output,created_at)
                   VALUES (?,?,?,?,?,?,?,'deferred',?,?)""",
                (batch_id, json.dumps(list(patch_ids)), base_commit, reviewed_commit, reviewed_ref,
                 json.dumps(list(changed_paths)), diff_content, test_output, _now()),
            )

    def _set_batch_status(self, batch_id: str, status: str, *, merged_at: str | None = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE reviewed_batches SET status=?, merged_at=? WHERE id=?",
                (status, merged_at, batch_id),
            )

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ("git", *args)
        if cwd is not None and cwd.resolve() != self.repo_path:
            command = ("git", "-c", "core.autocrlf=false", *args)
        # Always use binary pipes.  Windows text mode both translates patch
        # stdin to CRLF and normalises CRLF in diff stdout, either of which can
        # corrupt hunk context for repositories with explicit line endings.
        raw_bytes = input_text.encode("utf-8") if input_text is not None else None
        result = subprocess.run(
            command, cwd=cwd or self.repo_path, input=raw_bytes,
            capture_output=True, check=False,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )

    def _run_test_command(self, cwd: Path) -> subprocess.CompletedProcess[str]:
        """Run self.test_command with a timeout so a hung test (an infinite
        loop, a test blocking on network/input -- both realistic outcomes of
        a synthesized patch) can't freeze the whole merge forever. A timeout
        is reported as a normal failing CompletedProcess (returncode 1) so
        every caller's existing `if tested.returncode:` handling, including
        the reviewer-repair retry, applies to it unchanged."""
        try:
            return subprocess.run(
                self.test_command, cwd=cwd, text=True, capture_output=True,
                check=False, timeout=self.test_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.output or "") if isinstance(exc.output, str) else (exc.output or b"").decode("utf-8", errors="replace")
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
            return subprocess.CompletedProcess(
                self.test_command, 1, output,
                f"{stderr}\nverification timed out after {self.test_timeout_seconds:g}s".strip(),
            )

    def _failure(
        self,
        ids: list[str],
        status: str,
        message: str,
        process: subprocess.CompletedProcess[str],
    ) -> dict[str, Any]:
        # Patch-level rejection remains useful evidence for diagnostics; the
        # coordinator maps this to a recoverable needs_revision dispatch.
        self._set_status(ids, status)
        detail = (process.stderr or process.stdout or "").strip()[-4000:]
        return {
            "ok": False,
            "applied": False,
            "status": status,
            "patch_ids": ids,
            "error": message,
            "detail": detail,
        }

    def _set_status(self, ids: Sequence[str], status: str, *, merged_at: str | None = None) -> None:
        if status not in PATCH_STATUSES:
            raise ValueError(status)
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE patches SET status=?, merged_at=? WHERE id IN ({placeholders})",
                (status, merged_at, *ids),
            )

    @staticmethod
    def _collisions_within(patches: Sequence[StagedPatch]) -> list[list[str]]:
        collisions: list[list[str]] = []
        for index, left in enumerate(patches):
            for right in patches[index + 1:]:
                if set(left.files_touched).intersection(right.files_touched):
                    collisions.append([left.id, right.id])
        return collisions

    def _require(self, patch_id: str) -> StagedPatch:
        patch = self.get_patch(patch_id)
        if patch is None:
            raise KeyError(patch_id)
        return patch

    @staticmethod
    def _row(row: sqlite3.Row) -> StagedPatch:
        return StagedPatch(
            id=row["id"], agent_name=row["agent_name"], task_id=row["task_id"],
            files_touched=json.loads(row["files_touched_json"]),
            diff_content=row["diff_content"], status=row["status"],
            created_at=row["created_at"], merged_at=row["merged_at"],
        )
