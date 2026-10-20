"""
PlanTree — in-memory queue tree with revision tracking and boot_id.

Built from the current contents of holding_files and non-done batches.
Served as an immutable snapshot; callers swap the reference atomically.

Structure exposed to /state:
    {
        "boot_id": "uuid",
        "rev":     57,
        "mode":    "running",
        "tree":    [ <ClinicNode>, ... ],
        "counters": { "waiting": 3, "ready": 4, ... },
        "warnings": { "disk_warning": false, "csv_locked": false }
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

from server.models.prescription import PrescriptionFile
from server.models.batch import Batch, sort_files_for_batch, SLOT_ORDER, SLOT_COORDS


# ---------------------------------------------------------------------------
# Tree node types
# ---------------------------------------------------------------------------

@dataclass
class FileNode:
    """Leaf node — one doctor file."""
    id: str
    doctor_name: str
    file_path: str
    status: str             # "waiting" | "locked"
    is_skipped: bool
    slot_preview: Optional[str]   # "TL" | "TR" | "BL" | "BR" | None


@dataclass
class BatchNode:
    """Intermediate node — one planned or in-progress batch."""
    batch_id: str
    batch_number: Optional[str]   # None until leased
    status: str                   # "planned" | "ready" | "leased" | "done" | …
    slot_count: int
    is_forced: bool
    files: List[FileNode] = field(default_factory=list)

    @property
    def label(self) -> str:
        num = self.batch_number or f"PREVIEW-{self.batch_id[-6:]}"
        filled = sum(1 for f in self.files if not f.is_skipped)
        return f"[{filled}/{self.slot_count}] {num}"


@dataclass
class PaperNode:
    """Groups batches of the same paper weight within a clinic."""
    paper_weight: str
    batches: List[BatchNode] = field(default_factory=list)

    @property
    def waiting_count(self) -> int:
        return sum(1 for b in self.batches if b.status == "planned")

    @property
    def ready_count(self) -> int:
        return sum(1 for b in self.batches if b.status == "ready")


@dataclass
class ClinicNode:
    """Root tree node — one clinic."""
    clinic: str
    papers: List[PaperNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan Tree builder
# ---------------------------------------------------------------------------

class PlanTree:
    """
    Immutable snapshot of the current queue state.

    Build with PlanTree.build(...) from a list of PrescriptionFile objects.
    Do not mutate after construction; swap the reference in QueueService instead.
    """

    def __init__(
        self,
        boot_id: str,
        rev: int,
        mode: str,
        clinics: List[ClinicNode],
        counters: Dict[str, int],
        warnings: Dict[str, bool],
    ):
        self.boot_id  = boot_id
        self.rev      = rev
        self.mode     = mode
        self.clinics  = clinics
        self.counters = counters
        self.warnings = warnings

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        files: List[PrescriptionFile],
        boot_id: str,
        rev: int,
        mode: str = "idle",
        warnings: Optional[Dict[str, bool]] = None,
    ) -> "PlanTree":
        """
        Group files by (clinic, paper_weight), assign provisional slot positions,
        and return an immutable PlanTree snapshot.

        Only 'waiting' (non-locked, non-skipped) files are considered for
        provisional batch grouping. Locked and skipped files appear in the
        tree but are tagged accordingly.
        """
        warnings = warnings or {"disk_warning": False, "csv_locked": False}

        # Group all files by clinic → paper_weight
        grouped: Dict[str, Dict[str, List[PrescriptionFile]]] = {}
        for f in files:
            grouped.setdefault(f.clinic, {}).setdefault(f.paper_weight, []).append(f)

        clinic_nodes: List[ClinicNode] = []
        counters: Dict[str, int] = {
            "waiting": 0, "ready": 0, "locked": 0, "skipped": 0
        }

        for clinic_name in sorted(grouped.keys()):
            paper_map = grouped[clinic_name]
            paper_nodes: List[PaperNode] = []

            for paper_weight in sorted(paper_map.keys()):
                all_files = paper_map[paper_weight]

                # Separate skipped from active
                active  = [f for f in all_files if not f.is_skipped and f.status == "waiting"]
                locked  = [f for f in all_files if f.status == "locked"]
                skipped = [f for f in all_files if f.is_skipped]

                # Sort active by slot-ordering rule
                active_sorted = sort_files_for_batch(active)

                batch_nodes: List[BatchNode] = []
                batch_idx = 0

                # Chunk active files into groups of 4 (provisional batches)
                for chunk_start in range(0, max(len(active_sorted), 1), 4):
                    chunk = active_sorted[chunk_start: chunk_start + 4]
                    if not chunk:
                        break

                    batch_idx += 1
                    preview_id = f"preview_{clinic_name}_{paper_weight}_{batch_idx}"
                    is_ready = len(chunk) == 4
                    b_status = "ready" if is_ready else "planned"

                    file_nodes: List[FileNode] = []
                    for i, f in enumerate(chunk):
                        slot = SLOT_ORDER[i] if i < 4 else None
                        file_nodes.append(FileNode(
                            id=f.id,
                            doctor_name=f.doctor_name,
                            file_path=f.file_path,
                            status=f.status,
                            is_skipped=False,
                            slot_preview=slot,
                        ))

                    batch_nodes.append(BatchNode(
                        batch_id=preview_id,
                        batch_number=None,
                        status=b_status,
                        slot_count=4,
                        is_forced=False,
                        files=file_nodes,
                    ))

                    if is_ready:
                        counters["ready"] += 1
                    else:
                        counters["waiting"] += 1

                # Add locked files as separate locked-batch nodes
                for lf in locked:
                    counters["locked"] += 1
                    # Locked files are already represented via committed batches;
                    # we show them in the tree as "locked" leaves under a stub node.
                    stub_id = f"locked_{lf.id}"
                    fn = FileNode(
                        id=lf.id,
                        doctor_name=lf.doctor_name,
                        file_path=lf.file_path,
                        status="locked",
                        is_skipped=False,
                        slot_preview=None,
                    )
                    batch_nodes.append(BatchNode(
                        batch_id=stub_id,
                        batch_number=None,
                        status="locked",
                        slot_count=1,
                        is_forced=False,
                        files=[fn],
                    ))

                # Add skipped file nodes into a virtual "skipped" batch stub
                for sf in skipped:
                    counters["skipped"] += 1
                    fn = FileNode(
                        id=sf.id,
                        doctor_name=sf.doctor_name,
                        file_path=sf.file_path,
                        status="waiting",
                        is_skipped=True,
                        slot_preview=None,
                    )
                    batch_nodes.append(BatchNode(
                        batch_id=f"skipped_{sf.id}",
                        batch_number=None,
                        status="skipped",
                        slot_count=1,
                        is_forced=False,
                        files=[fn],
                    ))

                paper_nodes.append(PaperNode(
                    paper_weight=paper_weight,
                    batches=batch_nodes,
                ))

            clinic_nodes.append(ClinicNode(clinic=clinic_name, papers=paper_nodes))

        return cls(
            boot_id=boot_id,
            rev=rev,
            mode=mode,
            clinics=clinic_nodes,
            counters=counters,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "boot_id":  self.boot_id,
            "rev":      self.rev,
            "mode":     self.mode,
            "counters": self.counters,
            "warnings": self.warnings,
            "tree":     [self._clinic_to_dict(c) for c in self.clinics],
        }

    def _clinic_to_dict(self, c: ClinicNode) -> dict:
        return {
            "type":   "clinic",
            "name":   c.clinic,
            "papers": [self._paper_to_dict(p) for p in c.papers],
        }

    def _paper_to_dict(self, p: PaperNode) -> dict:
        return {
            "type":         "paper",
            "paper_weight": p.paper_weight,
            "batches":      [self._batch_to_dict(b) for b in p.batches],
        }

    def _batch_to_dict(self, b: BatchNode) -> dict:
        return {
            "type":         "batch",
            "batch_id":     b.batch_id,
            "batch_number": b.batch_number,
            "status":       b.status,
            "label":        b.label,
            "slot_count":   b.slot_count,
            "is_forced":    b.is_forced,
            "files":        [self._file_to_dict(f) for f in b.files],
        }

    def _file_to_dict(self, f: FileNode) -> dict:
        return {
            "type":         "file",
            "id":           f.id,
            "doctor_name":  f.doctor_name,
            "file_path":    f.file_path,
            "status":       f.status,
            "is_skipped":   f.is_skipped,
            "slot_preview": f.slot_preview,
        }
