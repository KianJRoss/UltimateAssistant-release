"""Safe, deterministic Cline/Roo Memory Bank context loading."""
from __future__ import annotations

import os
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path


MEMORY_BANK_FILES = (
    "projectBrief.md",
    "productContext.md",
    "systemPatterns.md",
    "techContext.md",
    "decisionLog.md",
    "activeContext.md",
    "progress.md",
)
MAX_FILE_BYTES = 65_536
MAX_TOTAL_BYTES = 262_144
BLOCK_HEADER = "<herald-memory-bank reference-only=\"true\">\n"
BLOCK_FOOTER = "</herald-memory-bank>"


@dataclass(frozen=True)
class MemoryBankContext:
    text: str | None
    files: tuple[Path, ...]
    warnings: tuple[str, ...]


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def load_memory_bank(
    workspace: str | Path,
    *,
    enabled: bool = False,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> MemoryBankContext:
    """Load canonical Memory Bank files beneath an explicitly trusted workspace."""
    if not enabled:
        return MemoryBankContext(None, (), ())
    if not 0 <= max_file_bytes <= MAX_FILE_BYTES:
        raise ValueError(f"max_file_bytes must be between 0 and {MAX_FILE_BYTES}")
    if not 0 <= max_total_bytes <= MAX_TOTAL_BYTES:
        raise ValueError(f"max_total_bytes must be between 0 and {MAX_TOTAL_BYTES}")

    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must resolve to a directory")
    bank = root / "memory-bank"
    messages: list[str] = []

    def reject(relative: str, reason: str) -> None:
        message = f"Memory Bank skipped {relative}: {reason}"
        messages.append(message)
        warnings.warn(message, RuntimeWarning, stacklevel=2)

    try:
        if _is_reparse_point(bank):
            reject("memory-bank", "symlink or junction is not allowed")
            return MemoryBankContext(None, (), tuple(messages))
        bank_info = bank.lstat()
    except FileNotFoundError:
        return MemoryBankContext(None, (), ())
    except OSError:
        reject("memory-bank", "could not inspect path")
        return MemoryBankContext(None, (), tuple(messages))
    if not stat.S_ISDIR(bank_info.st_mode):
        reject("memory-bank", "not a directory")
        return MemoryBankContext(None, (), tuple(messages))
    try:
        resolved_bank = bank.resolve(strict=True)
        resolved_bank.relative_to(root)
    except (OSError, ValueError):
        reject("memory-bank", "path escapes the trusted workspace")
        return MemoryBankContext(None, (), tuple(messages))
    try:
        # Path lookup is case-insensitive on some supported filesystems, so
        # select from the directory's actual entry names to enforce the
        # portable profile's exact, case-sensitive filenames everywhere.
        available_names = {entry.name for entry in bank.iterdir()}
    except OSError:
        reject("memory-bank", "could not read directory")
        return MemoryBankContext(None, (), tuple(messages))

    parts: list[bytes] = [BLOCK_HEADER.encode("utf-8")]
    used = len(parts[0]) + len(BLOCK_FOOTER.encode("utf-8"))
    loaded: list[Path] = []
    identities: set[tuple[int, int]] = set()
    for name in MEMORY_BANK_FILES:
        if name not in available_names:
            continue
        candidate = bank / name
        relative = f"memory-bank/{name}"
        try:
            if _is_reparse_point(candidate):
                reject(relative, "symlink or junction is not allowed")
                continue
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            reject(relative, "could not inspect path")
            continue
        if not stat.S_ISREG(info.st_mode):
            reject(relative, "not a regular file")
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            reject(relative, "path escapes the trusted workspace")
            continue
        if info.st_size > max_file_bytes:
            reject(relative, f"exceeds {max_file_bytes}-byte file limit")
            continue
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags)
            try:
                opened_info = os.fstat(descriptor)
                if not stat.S_ISREG(opened_info.st_mode):
                    reject(relative, "not a regular file")
                    continue
                if (opened_info.st_dev, opened_info.st_ino) != (info.st_dev, info.st_ino):
                    reject(relative, "path changed during inspection")
                    continue
                chunks: list[bytes] = []
                remaining = max_file_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                after_read = os.fstat(descriptor)
                before_signature = (
                    opened_info.st_dev, opened_info.st_ino, opened_info.st_mode,
                    opened_info.st_size, opened_info.st_mtime_ns,
                )
                after_signature = (
                    after_read.st_dev, after_read.st_ino, after_read.st_mode,
                    after_read.st_size, after_read.st_mtime_ns,
                )
                if after_signature != before_signature:
                    reject(relative, "file changed during read")
                    continue
            finally:
                os.close(descriptor)
        except OSError:
            reject(relative, "could not read file")
            continue
        identity = (opened_info.st_dev, opened_info.st_ino)
        if identity in identities:
            reject(relative, "duplicate resolved file")
            continue
        identities.add(identity)
        if len(raw) > max_file_bytes:
            reject(relative, f"exceeds {max_file_bytes}-byte file limit")
            continue
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            reject(relative, "invalid UTF-8")
            continue
        section = f'<memory-bank-file path="{relative}">\n'.encode() + raw + b"\n</memory-bank-file>\n"
        if used + len(section) > max_total_bytes:
            reject(relative, f"exceeds {max_total_bytes}-byte total limit")
            continue
        parts.append(section)
        used += len(section)
        loaded.append(resolved)
    if not loaded:
        return MemoryBankContext(None, (), tuple(messages))
    parts.append(BLOCK_FOOTER.encode("utf-8"))
    return MemoryBankContext(b"".join(parts).decode("utf-8"), tuple(loaded), tuple(messages))
