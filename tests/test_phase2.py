"""
Phase 2 tests: Scanner, PlanTree, QueueService snapshot, and /state endpoint.

All tests use an isolated temp directory with a real SQLite database.
No network calls, no real Photoshop processes.
"""
import os
import time
import tempfile
import pytest

from server.config import AppConfig
from server.database import init_db, get_connection
from server.app import create_app
from server.models.prescription import (
    extract_doctor_name,
    parse_folder_path,
    file_id,
    PrescriptionFile,
)
from server.models.batch import sort_files_for_batch, SLOT_COORDS, SLOT_ORDER
from server.models.plan_tree import PlanTree
from server.models.selection_rules import SelectionChange
from server.adapters.filesystem_adapter import FileSystemAdapter, safe_move
from server.services.queue_service import QueueService, StaleRevisionError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def temp_config(tmpdir_path):
    source = os.path.join(tmpdir_path, "Input")
    finished = os.path.join(tmpdir_path, "Finished")
    os.makedirs(source, exist_ok=True)
    os.makedirs(finished, exist_ok=True)
    cfg = AppConfig(
        database_path=os.path.join(tmpdir_path, "test.db"),
        log_dir=os.path.join(tmpdir_path, "logs"),
        secret_token="test-token",
        source_folder=source,
        finished_folder=finished,
        scan_interval_seconds=5.0,
    )
    init_db(cfg.database_path)
    return cfg


@pytest.fixture
def client(temp_config):
    app = create_app(custom_config=temp_config)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, temp_config
    # Stop daemon threads so they release SQLite WAL before temp dir cleanup
    for key in ("FINALIZER_WORKER", "LEASE_REAPER", "SCANNER_SERVICE"):
        svc = app.config.get(key)
        if svc and hasattr(svc, "stop"):
            svc.stop(timeout=2.0)


def _write_psd(folder: str, name: str, content: bytes = b"FAKE_PSD") -> str:
    """Write a fake PSD file and return its absolute path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


# ===========================================================================
# 1. Doctor name extraction
# ===========================================================================

class TestExtractDoctorName:
    def test_plain(self):
        assert extract_doctor_name("Dr_Smith.psd") == "Dr_Smith"

    def test_numeric_suffix_underscore(self):
        assert extract_doctor_name("Dr_Smith_01.psd") == "Dr_Smith"

    def test_numeric_suffix_hyphen(self):
        assert extract_doctor_name("Dr-Ahmed-03.tif") == "Dr-Ahmed"

    def test_numeric_suffix_space(self):
        assert extract_doctor_name("Dr. Mona 4.tiff") == "Dr. Mona"

    def test_psb_extension(self):
        assert extract_doctor_name("Dr_Ziad_2.psb") == "Dr_Ziad"

    def test_unrecognised_extension(self):
        assert extract_doctor_name("random_file.docx") is None


# ===========================================================================
# 2. Folder path parsing
# ===========================================================================

class TestParseFolderPath:
    def test_two_level(self, tmpdir_path):
        """<root>/<Clinic>/<PaperWeight>/<Doctor>.psd"""
        path = os.path.join(tmpdir_path, "Al-Amal", "80gsm", "Dr_X.psd")
        clinic, pw = parse_folder_path(path, tmpdir_path)
        assert clinic == "Al-Amal"
        assert pw == "80gsm"

    def test_one_level_defaults_standard(self, tmpdir_path):
        """<root>/<Clinic>/<Doctor>.psd → paper = Standard"""
        path = os.path.join(tmpdir_path, "Al-Amal", "Dr_X.psd")
        clinic, pw = parse_folder_path(path, tmpdir_path)
        assert clinic == "Al-Amal"
        assert pw == "Standard"


# ===========================================================================
# 3. Slot ordering rule
# ===========================================================================

class TestSlotOrdering:
    def _make_file(self, mtime: float, path: str) -> PrescriptionFile:
        return PrescriptionFile(
            id=file_id(path),
            file_path=path,
            clinic="Clinic",
            paper_weight="Standard",
            doctor_name="Dr",
            file_size=100,
            mtime=mtime,
        )

    def test_oldest_first(self):
        f1 = self._make_file(200.0, "/root/Clinic/Standard/Dr_B.psd")
        f2 = self._make_file(100.0, "/root/Clinic/Standard/Dr_A.psd")
        result = sort_files_for_batch([f1, f2])
        assert result[0].file_path == f2.file_path  # older mtime first

    def test_tie_break_by_path(self):
        f1 = self._make_file(100.0, "/root/Clinic/Standard/Dr_Z.psd")
        f2 = self._make_file(100.0, "/root/Clinic/Standard/Dr_A.psd")
        result = sort_files_for_batch([f1, f2])
        assert result[0].file_path == f2.file_path  # Dr_A < Dr_Z


# ===========================================================================
# 4. PlanTree builder
# ===========================================================================

class TestPlanTree:
    def _make_file(self, clinic, paper, doctor, mtime=100.0, skipped=False, status="waiting"):
        path = f"/root/{clinic}/{paper}/{doctor}.psd"
        return PrescriptionFile(
            id=file_id(path),
            file_path=path,
            clinic=clinic,
            paper_weight=paper,
            doctor_name=doctor,
            file_size=1000,
            mtime=mtime,
            is_skipped=skipped,
            status=status,
        )

    def test_four_files_form_one_ready_batch(self):
        files = [self._make_file("Clinic", "Standard", f"Dr_{i}") for i in range(4)]
        tree = PlanTree.build(files, boot_id="boot1", rev=1)
        assert len(tree.clinics) == 1
        clinic = tree.clinics[0]
        assert clinic.clinic == "Clinic"
        paper = clinic.papers[0]
        assert paper.ready_count == 1
        assert paper.waiting_count == 0

    def test_three_files_form_one_planned_batch(self):
        files = [self._make_file("Clinic", "Standard", f"Dr_{i}") for i in range(3)]
        tree = PlanTree.build(files, boot_id="boot1", rev=1)
        paper = tree.clinics[0].papers[0]
        assert paper.waiting_count == 1
        assert paper.ready_count == 0

    def test_five_files_split_into_two_batches(self):
        files = [self._make_file("Clinic", "Standard", f"Dr_{i}") for i in range(5)]
        tree = PlanTree.build(files, boot_id="boot1", rev=1)
        paper = tree.clinics[0].papers[0]
        total_batches = [b for b in paper.batches if b.status in ("ready", "planned")]
        assert len(total_batches) == 2

    def test_skipped_files_appear_in_tree_but_not_batched(self):
        active = [self._make_file("Clinic", "Standard", f"Dr_{i}") for i in range(3)]
        skipped = self._make_file("Clinic", "Standard", "Dr_Skip", skipped=True)
        tree = PlanTree.build(active + [skipped], boot_id="boot1", rev=1)
        assert tree.counters["skipped"] == 1
        # Active 3 form a planned batch
        paper = tree.clinics[0].papers[0]
        planned = [b for b in paper.batches if b.status == "planned"]
        assert len(planned) == 1

    def test_serialisation_contains_boot_id_and_rev(self):
        files = [self._make_file("Clinic", "Standard", "Dr_X")]
        tree = PlanTree.build(files, boot_id="my-boot", rev=42)
        d = tree.to_dict()
        assert d["boot_id"] == "my-boot"
        assert d["rev"] == 42
        assert "tree" in d

    def test_two_clinics_separate_nodes(self):
        f1 = self._make_file("Clinic_A", "Standard", "Dr_X")
        f2 = self._make_file("Clinic_B", "Standard", "Dr_Y")
        tree = PlanTree.build([f1, f2], boot_id="b", rev=1)
        assert len(tree.clinics) == 2


# ===========================================================================
# 5. SelectionChange parsing
# ===========================================================================

class TestSelectionChange:
    def test_valid_payload(self):
        sc = SelectionChange.from_payload({"rev": 5, "skip": ["a", "b"], "include": []})
        assert sc.rev == 5
        assert sc.skip == ["a", "b"]

    def test_missing_rev_raises(self):
        with pytest.raises(ValueError, match="rev"):
            SelectionChange.from_payload({"skip": []})

    def test_overlap_raises(self):
        with pytest.raises(ValueError, match="cannot appear"):
            SelectionChange.from_payload({"rev": 1, "skip": ["x"], "include": ["x"]})

    def test_non_string_ids_raise(self):
        with pytest.raises(ValueError):
            SelectionChange.from_payload({"rev": 1, "skip": [123], "include": []})


# ===========================================================================
# 6. FileSystemAdapter — stability check
# ===========================================================================

class TestFileSystemAdapter:
    def test_file_not_inserted_on_first_scan(self, temp_config):
        """A file seen for the first time is NOT inserted (needs two stable passes)."""
        folder = os.path.join(temp_config.source_folder, "Clinic_A", "Standard")
        _write_psd(folder, "Dr_First.psd")
        adapter = FileSystemAdapter(
            temp_config.source_folder,
            temp_config.allowed_extensions,
            temp_config.doctor_regex,
        )
        conn = get_connection(temp_config.database_path)
        try:
            inserted, _ = adapter.scan(conn)
            assert len(inserted) == 0  # First pass: candidate only
        finally:
            conn.close()

    def test_file_inserted_on_second_scan(self, temp_config):
        """A stable file is inserted after two consecutive passes."""
        folder = os.path.join(temp_config.source_folder, "Clinic_A", "Standard")
        _write_psd(folder, "Dr_Second.psd")
        adapter = FileSystemAdapter(
            temp_config.source_folder,
            temp_config.allowed_extensions,
            temp_config.doctor_regex,
        )
        conn = get_connection(temp_config.database_path)
        try:
            adapter.scan(conn)   # Pass 1: records as candidate
            inserted, _ = adapter.scan(conn)   # Pass 2: stable → insert
            assert len(inserted) == 1
            # Verify it's in the DB
            row = conn.execute(
                "SELECT * FROM holding_files WHERE file_path = ?;",
                (os.path.join(folder, "Dr_Second.psd"),)
            ).fetchone()
            assert row is not None
            assert row["doctor_name"] == "Dr_Second"
            assert row["clinic"] == "Clinic_A"
            assert row["paper_weight"] == "Standard"
            assert row["status"] == "waiting"
        finally:
            conn.close()

    def test_deleted_file_pruned_from_db(self, temp_config):
        """A waiting file deleted from disk is pruned from holding_files."""
        folder = os.path.join(temp_config.source_folder, "Clinic_A", "Standard")
        path = _write_psd(folder, "Dr_Gone.psd")
        adapter = FileSystemAdapter(
            temp_config.source_folder,
            temp_config.allowed_extensions,
            temp_config.doctor_regex,
        )
        conn = get_connection(temp_config.database_path)
        try:
            adapter.scan(conn)
            adapter.scan(conn)   # Now in DB

            os.remove(path)
            _, deleted = adapter.scan(conn)
            assert path in deleted

            row = conn.execute(
                "SELECT * FROM holding_files WHERE file_path = ?;", (path,)
            ).fetchone()
            assert row is None
        finally:
            conn.close()

    def test_changed_file_resets_skip(self, temp_config):
        """If a file's size changes, is_skipped resets to 0."""
        folder = os.path.join(temp_config.source_folder, "Clinic_A", "Standard")
        path = _write_psd(folder, "Dr_Changed.psd", b"v1")
        adapter = FileSystemAdapter(
            temp_config.source_folder,
            temp_config.allowed_extensions,
            temp_config.doctor_regex,
        )
        conn = get_connection(temp_config.database_path)
        try:
            adapter.scan(conn)
            adapter.scan(conn)   # In DB as waiting

            # Operator skips it
            fid = file_id(path)
            conn.execute("UPDATE holding_files SET is_skipped=1 WHERE id=?;", (fid,))
            conn.commit()

            # File gets updated on disk
            with open(path, "wb") as f:
                f.write(b"v2_larger_content")

            adapter.scan(conn)
            adapter.scan(conn)   # Two more passes to re-stabilise

            row = conn.execute(
                "SELECT is_skipped FROM holding_files WHERE id=?;", (fid,)
            ).fetchone()
            assert row is not None
            assert row["is_skipped"] == 0  # Skip was reset
        finally:
            conn.close()

    def test_folder_exclusion_prevents_insert(self, temp_config):
        """Files inside an excluded folder are never inserted."""
        folder = os.path.join(temp_config.source_folder, "Excluded_Clinic", "Standard")
        _write_psd(folder, "Dr_X.psd")
        conn = get_connection(temp_config.database_path)
        try:
            excluded_path = folder.replace("\\", "/")
            conn.execute(
                "INSERT INTO folder_exclusions (folder_path) VALUES (?);",
                (excluded_path,)
            )
            conn.commit()

            adapter = FileSystemAdapter(
                temp_config.source_folder,
                temp_config.allowed_extensions,
                temp_config.doctor_regex,
            )
            adapter.scan(conn)
            inserted, _ = adapter.scan(conn)
            assert len(inserted) == 0
        finally:
            conn.close()


# ===========================================================================
# 7. safe_move
# ===========================================================================

class TestSafeMove:
    def test_same_volume_move(self, tmpdir_path):
        src = os.path.join(tmpdir_path, "source.psd")
        dst = os.path.join(tmpdir_path, "subdir", "dest.psd")
        with open(src, "wb") as f:
            f.write(b"content")
        safe_move(src, dst)
        assert not os.path.exists(src)
        assert os.path.exists(dst)
        with open(dst, "rb") as f:
            assert f.read() == b"content"


# ===========================================================================
# 8. QueueService snapshot & selection
# ===========================================================================

class TestQueueService:
    def _populate_file(self, db_path, path, clinic, paper, doctor):
        """Directly insert a holding_files row."""
        fid = file_id(path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO holding_files "
                "(id, file_path, clinic, paper_weight, doctor_name, file_size, mtime) "
                "VALUES (?, ?, ?, ?, ?, 1000, 100.0);",
                (fid, path, clinic, paper, doctor)
            )
            conn.commit()
        finally:
            conn.close()
        return fid

    def test_initial_snapshot_has_boot_id(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        snap = qs.get_snapshot()
        assert snap is not None
        assert len(snap.boot_id) == 36   # UUID4

    def test_rebuild_snapshot_reflects_db(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        fid = self._populate_file(
            temp_config.database_path,
            "/fake/Clinic/Standard/Dr_A.psd",
            "Clinic", "Standard", "Dr_A"
        )
        conn = get_connection(temp_config.database_path)
        try:
            qs.rebuild_snapshot(conn)
        finally:
            conn.close()
        snap = qs.get_snapshot()
        assert snap.counters["waiting"] > 0 or snap.counters["ready"] > 0

    def test_rev_increments_on_rebuild(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        rev_before = qs.rev
        conn = get_connection(temp_config.database_path)
        try:
            qs.rebuild_snapshot(conn)
        finally:
            conn.close()
        assert qs.rev == rev_before + 1

    def test_stale_rev_raises(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        change = SelectionChange(rev=9999, skip=[], include=[])
        with pytest.raises(StaleRevisionError):
            qs.apply_selection(change)

    def test_apply_selection_skip(self, temp_config):
        qs = QueueService(temp_config)
        qs.initialise()
        fid = self._populate_file(
            temp_config.database_path,
            "/fake/Clinic/Standard/Dr_B.psd",
            "Clinic", "Standard", "Dr_B"
        )
        conn = get_connection(temp_config.database_path)
        try:
            qs.rebuild_snapshot(conn)
        finally:
            conn.close()
        current_rev = qs.rev
        change = SelectionChange(rev=current_rev, skip=[fid], include=[])
        new_snap = qs.apply_selection(change)
        assert new_snap.counters["skipped"] == 1


# ===========================================================================
# 9. /state HTTP endpoint
# ===========================================================================

class TestStateEndpoint:
    def test_get_state_returns_200(self, client):
        c, cfg = client
        resp = c.get("/state", headers={"X-Token": cfg.secret_token})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "boot_id" in data
        assert "rev" in data
        assert "tree" in data

    def test_get_state_304_on_matching_rev(self, client):
        c, cfg = client
        resp1 = c.get("/state", headers={"X-Token": cfg.secret_token})
        data = resp1.get_json()
        rev = data["rev"]
        boot_id = data["boot_id"]
        resp2 = c.get(
            f"/state?rev={rev}&boot_id={boot_id}",
            headers={"X-Token": cfg.secret_token}
        )
        assert resp2.status_code == 304

    def test_get_state_200_on_different_rev(self, client):
        c, cfg = client
        resp = c.get(
            "/state?rev=9999&boot_id=wrong",
            headers={"X-Token": cfg.secret_token}
        )
        assert resp.status_code == 200

    def test_post_selection_missing_rev(self, client):
        c, cfg = client
        resp = c.post(
            "/selection",
            json={"skip": []},
            headers={"X-Token": cfg.secret_token}
        )
        assert resp.status_code == 422

    def test_post_selection_stale_rev(self, client):
        c, cfg = client
        resp = c.post(
            "/selection",
            json={"rev": 9999, "skip": [], "include": []},
            headers={"X-Token": cfg.secret_token}
        )
        assert resp.status_code == 409

    def test_post_selection_valid(self, client):
        c, cfg = client
        # Get current rev
        state = c.get("/state", headers={"X-Token": cfg.secret_token}).get_json()
        rev = state["rev"]
        resp = c.post(
            "/selection",
            json={"rev": rev, "skip": [], "include": []},
            headers={"X-Token": cfg.secret_token}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "rev" in data

    def test_state_unauthenticated(self, client):
        c, _ = client
        resp = c.get("/state")
        assert resp.status_code == 401
