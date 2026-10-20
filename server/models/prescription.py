"""
PrescriptionFile — pure domain model for a single doctor PSD/PSB on disk.

No Flask, SQLite, or network imports. Only business-rule logic lives here.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Doctor-name extraction
# ---------------------------------------------------------------------------

# Default pattern: strips trailing _01, -2, " 3", etc. and the extension.
# Configurable from AppConfig.doctor_regex_pattern.
_DEFAULT_PATTERN = re.compile(
    r"^(.*?)(?:[_\-\s]\d+)?\.(psd|psb|tif|tiff)$",
    re.IGNORECASE,
)


def extract_doctor_name(filename: str, regex: Optional[re.Pattern] = None) -> Optional[str]:
    """
    Extract the normalised doctor name from a filename.

    Examples:
        "Dr_Smith.psd"      → "Dr_Smith"
        "Dr_Smith_01.psd"   → "Dr_Smith"
        "Dr-Ahmed-03.tif"   → "Dr-Ahmed"
        "Dr. Mona 4.tiff"   → "Dr. Mona"

    Returns None if the filename does not match any known pattern.
    """
    pattern = regex or _DEFAULT_PATTERN
    m = pattern.match(filename)
    if not m:
        return None
    return m.group(1).strip()


def parse_folder_path(file_path: str, source_root: str) -> tuple[str, str]:
    """
    Derive clinic and paper_weight from the file's folder structure relative
    to the source root.

    Expected layouts:
        <source_root>/<Clinic>/<Doctor>.psd              → clinic, "Standard"
        <source_root>/<Clinic>/<PaperWeight>/<Doctor>.psd → clinic, paper_weight

    Both clinic and paper_weight are stripped of leading/trailing whitespace.
    Returns ("Unknown", "Standard") if the path cannot be parsed.
    """
    try:
        rel = os.path.relpath(os.path.dirname(file_path), source_root)
        parts = [p.strip() for p in rel.replace("\\", "/").split("/") if p and p != "."]
        if len(parts) == 1:
            return parts[0], "Standard"
        if len(parts) >= 2:
            return parts[0], parts[1]
    except Exception:
        pass
    return "Unknown", "Standard"


def file_id(file_path: str) -> str:
    """
    Stable, unique identifier for a file path.
    SHA-1 of the normalised (lowercased, forward-slash) path string.
    """
    normalised = file_path.replace("\\", "/").lower()
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Domain entity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrescriptionFile:
    """
    Represents a single doctor prescription design file discovered on disk.

    Immutable value object — create a new instance if attributes change.
    """
    id: str                  # SHA-1 of normalised file_path
    file_path: str           # Absolute path on disk
    clinic: str
    paper_weight: str
    doctor_name: str
    file_size: int           # Bytes at last stable check
    mtime: float             # os.stat().st_mtime at last stable check
    is_skipped: bool = False # Operator exclusion flag
    status: str = "waiting"  # "waiting" | "locked"
    batch_id: Optional[str] = None  # Set when status = "locked"

    @property
    def filename(self) -> str:
        return os.path.basename(self.file_path)

    @property
    def group_key(self) -> tuple[str, str]:
        """Used to group files into batches: (clinic, paper_weight)."""
        return (self.clinic, self.paper_weight)

    @classmethod
    def from_db_row(cls, row) -> "PrescriptionFile":
        """Construct from a sqlite3.Row returned by a holding_files query."""
        return cls(
            id=row["id"],
            file_path=row["file_path"],
            clinic=row["clinic"],
            paper_weight=row["paper_weight"],
            doctor_name=row["doctor_name"],
            file_size=row["file_size"],
            mtime=row["mtime"],
            is_skipped=bool(row["is_skipped"]),
            status=row["status"],
            batch_id=row["batch_id"],
        )
