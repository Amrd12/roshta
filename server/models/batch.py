"""
Batch & BatchSlot — pure domain models for production print batches.

No Flask, SQLite, or network imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from server.models.prescription import PrescriptionFile

# ---------------------------------------------------------------------------
# Slot positions (quadrant layout on 50×34 cm canvas)
# ---------------------------------------------------------------------------

SLOT_ORDER = ("TL", "TR", "BL", "BR")

SLOT_COORDS: dict[str, tuple[float, float]] = {
    "TL": (0.0,  0.0),
    "TR": (25.0, 0.0),
    "BL": (0.0,  17.0),
    "BR": (25.0, 17.0),
}


# ---------------------------------------------------------------------------
# Slot ordering rule (per spec §3.2):
#   Oldest first_seen_at first; tie-break by file_path ASC.
# ---------------------------------------------------------------------------

def sort_files_for_batch(files: list) -> list:
    """
    Sort PrescriptionFile instances into slot order:
        primary  key: first_seen_at ascending  (oldest arrivals first)
        secondary key: file_path ascending     (deterministic tie-break)

    This is a pure function so it can be tested without a database.
    """
    return sorted(files, key=lambda f: (f.mtime, f.file_path))


# ---------------------------------------------------------------------------
# Domain entities
# ---------------------------------------------------------------------------

@dataclass
class BatchSlot:
    slot: str             # "TL" | "TR" | "BL" | "BR"
    doctor_name: str
    source_path: str
    x_cm: float
    y_cm: float
    is_empty: bool = False        # True = white-fill placeholder (force batch)
    finished_path: Optional[str] = None


@dataclass
class Batch:
    """
    Represents a production batch of up to 4 prescription designs.

    Instances here are in-memory plan previews (status='planned').
    Persisted batches are read back from SQLite via QueueService.
    """
    id: str
    clinic: str
    paper_weight: str
    slots: List[BatchSlot] = field(default_factory=list)
    status: str = "planned"       # "planned" | "ready" | "leased" | "done" | …
    batch_number: Optional[str] = None
    is_forced: bool = False

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    @property
    def is_full(self) -> bool:
        return self.slot_count == 4

    @property
    def is_ready(self) -> bool:
        """A batch is ready to lease when it has 4 non-skipped files."""
        return self.is_full

    @classmethod
    def build_planned(
        cls,
        batch_id: str,
        clinic: str,
        paper_weight: str,
        files: list,           # List[PrescriptionFile], already sorted
        is_forced: bool = False,
    ) -> "Batch":
        """
        Construct an in-memory planned batch from up to 4 PrescriptionFile objects.
        Empty slots are filled with white-fill placeholders when is_forced=True.
        """
        slots: List[BatchSlot] = []
        for i, slot_name in enumerate(SLOT_ORDER):
            x, y = SLOT_COORDS[slot_name]
            if i < len(files):
                f = files[i]
                slots.append(BatchSlot(
                    slot=slot_name,
                    doctor_name=f.doctor_name,
                    source_path=f.file_path,
                    x_cm=x,
                    y_cm=y,
                    is_empty=False,
                ))
            elif is_forced:
                slots.append(BatchSlot(
                    slot=slot_name,
                    doctor_name="[EMPTY]",
                    source_path="",
                    x_cm=x,
                    y_cm=y,
                    is_empty=True,
                ))

        return cls(
            id=batch_id,
            clinic=clinic,
            paper_weight=paper_weight,
            slots=slots,
            status="planned" if not is_forced else "ready",
            is_forced=is_forced,
        )
