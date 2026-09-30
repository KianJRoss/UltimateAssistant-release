"""Fail when release archives contain private or runtime-only material.

The source checkout is allowed to contain private operational configuration and
internal documentation. This checker deliberately inspects only publication
artifacts. Add one private marker per line to the ignored
``.publication-private-markers`` file, or pass the same list through the
``HERALD_PRIVATE_MARKERS`` environment variable in protected release CI.
"""
from __future__ import annotations

import argparse
import os
import re
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKER_FILE = ROOT / ".publication-private-markers"
TEXT_LIMIT = 8 * 1024 * 1024

FORBIDDEN_ENTRY_PARTS = (
    "__pycache__",
    ".pyc",
    ".pyo",
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite3",
    ".log",
)

GENERIC_PRIVATE_PATTERNS = {
    "absolute user-home path": re.compile(
        rb"(?:[A-Za-z]:[\\/]+Users[\\/]+|(?<![A-Za-z0-9:/])/(?:home|Users)/)"
        rb"[^\\/\s|()\[\]{}<>\"']+",
        re.IGNORECASE,
    ),
    "private key": re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
}


def _markers(path: Path | None) -> list[bytes]:
    values: list[str] = []
    if path and path.is_file():
        values.extend(path.read_text(encoding="utf-8").splitlines())
    values.extend(os.environ.get("HERALD_PRIVATE_MARKERS", "").splitlines())
    cleaned = {value.strip().casefold() for value in values if len(value.strip()) >= 3}
    return sorted((value.encode("utf-8") for value in cleaned), key=len, reverse=True)


def _archive_entries(path: Path):
    if path.suffix in {".whl", ".zip"}:
        with ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith("/"):
                    yield name, archive.read(name)
        return
    if path.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    handle = archive.extractfile(member)
                    yield member.name, handle.read() if handle else b""
        return
    raise ValueError(f"unsupported release archive: {path}")


def _entry_findings(name: str, data: bytes, markers: list[bytes]) -> list[str]:
    findings: list[str] = []
    normalized = PurePosixPath(name.replace("\\", "/"))
    lowered_name = str(normalized).casefold()
    if any(part in lowered_name for part in FORBIDDEN_ENTRY_PARTS):
        findings.append("runtime/generated file")
    sample = data[:TEXT_LIMIT]
    lowered = sample.lower()
    name_bytes = lowered_name.encode("utf-8")
    if any(_contains_marker(lowered, marker) or _contains_marker(name_bytes, marker) for marker in markers):
        findings.append("private marker")
    for label, pattern in GENERIC_PRIVATE_PATTERNS.items():
        if pattern.search(sample) or pattern.search(name.encode("utf-8", "ignore")):
            findings.append(label)
    return findings


def _contains_marker(content: bytes, marker: bytes) -> bool:
    """Use token boundaries for short names to avoid substring false positives."""
    if len(marker) >= 5 or not marker.isalnum():
        return marker in content
    pattern = rb"(?<![a-z0-9])" + re.escape(marker) + rb"(?![a-z0-9])"
    return re.search(pattern, content) is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--markers-file", type=Path, default=DEFAULT_MARKER_FILE)
    parser.add_argument("--require-private-markers", action="store_true")
    args = parser.parse_args()

    markers = _markers(args.markers_file)
    if args.require_private_markers and not markers:
        print("private marker input is required for a publication build")
        return 1

    failures: list[tuple[Path, str, list[str]]] = []
    for artifact in args.artifacts:
        for name, data in _archive_entries(artifact):
            findings = _entry_findings(name, data, markers)
            if findings:
                failures.append((artifact, name, findings))

    if failures:
        for artifact, name, findings in failures:
            print(f"{artifact.name}: {name}: {', '.join(findings)}")
        print(f"publication blocked: {len(failures)} archive entries need review")
        return 1

    print(f"publication artifact check passed for {len(args.artifacts)} archive(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
