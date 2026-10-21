"""
tools/fake_plugin.py — Simulates the Photoshop plugin worker for E2E testing.

Simulates Photoshop processing:
  1. Requests a lease via GET /next-job (spec-compliant endpoint).
  2. While 'rendering', sends periodic heartbeats via POST /job-heartbeat.
  3. Generates a valid output image (TIFF or JPEG) in a temporary directory.
  4. Submits the completion report via POST /job-result.
  5. Repeats for N batches (default 50) or until queue is empty.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fake_plugin")


class FakePhotoshopPlugin:
    """
    Simulates the Roshta Photoshop plugin client.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str,
        render_delay_seconds: float = 0.05,
        heartbeat_interval: float = 1.0,
        output_format: str = "tiff",
        output_dir: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.render_delay = render_delay_seconds
        self.heartbeat_interval = heartbeat_interval
        self.output_format = output_format.lower()
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            self.temp_dir = output_dir
        else:
            self.temp_dir = self._create_temp_dir()

    def _create_temp_dir(self) -> str:
        local_tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp")
        os.makedirs(local_tmp, exist_ok=True)
        for env_k in ("TEMP", "TMP"):
            p = os.environ.get(env_k)
            if p:
                try:
                    os.makedirs(p, exist_ok=True)
                except Exception:
                    pass
        try:
            probe = tempfile.mkdtemp(prefix="_probe_")
            os.rmdir(probe)
            return tempfile.mkdtemp(prefix="roshta_fake_plugin_")
        except Exception:
            tempfile.tempdir = local_tmp
            return tempfile.mkdtemp(prefix="roshta_fake_plugin_", dir=local_tmp)

    def _http_request(
        self, endpoint: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Token", self.auth_token)
        req.add_header("Authorization", f"Bearer {self.auth_token}")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                body = resp.read()
                parsed = json.loads(body.decode("utf-8")) if body else None
                return status, parsed
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = body.decode("utf-8")
            return exc.code, parsed
        except Exception as exc:
            logger.error("HTTP request error to %s: %s", url, exc)
            return 0, str(exc)

    def lease_batch(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """GET /next-job — spec-compliant endpoint."""
        qs = "?force=1" if force else ""
        status, data = self._http_request(f"/next-job{qs}", method="GET")
        if status == 200 and isinstance(data, dict):
            return data
        if status == 204:
            return None
        logger.warning("next-job returned status %s: %s", status, data)
        return None

    def send_heartbeat(self, batch_id: str, lease_id: str) -> bool:
        status, _ = self._http_request(
            "/job-heartbeat",
            method="POST",
            payload={"batch_id": batch_id, "lease_id": lease_id},
        )
        return status == 200

    def submit_result(
        self,
        batch_id: str,
        lease_id: str,
        success: bool,
        print_temp_path: Optional[str] = None,
        duration_ms: int = 100,
        error_message: Optional[str] = None,
    ) -> bool:
        payload = {
            "batch_id": batch_id,
            "lease_id": lease_id,
            "success": success,
            "print_temp_path": print_temp_path,
            "duration_ms": duration_ms,
            "error_message": error_message,
        }
        status, _ = self._http_request("/job-result", method="POST", payload=payload)
        return status == 202

    def create_dummy_render_file(self, batch_id: str) -> str:
        """Creates a non-blank valid image file (TIFF or JPEG) with DPI metadata."""
        ext = "tif" if self.output_format == "tiff" else "jpg"
        path = os.path.join(self.temp_dir, f"render_{batch_id}.{ext}")

        # 300x200 test image with colored content (so it passes non-blank check)
        arr = np.zeros((200, 300, 3), dtype=np.uint8)
        arr[50:150, 50:250] = [100, 150, 200]
        img = Image.fromarray(arr)

        if ext == "tif":
            img.save(path, format="TIFF", dpi=(300, 300), compression="tiff_lzw")
        else:
            img.save(path, format="JPEG", dpi=(300, 300), quality=95)

        return path

    def run_batch(self, contract: Dict[str, Any]) -> bool:
        batch_id = contract["batch_id"]
        lease_id = contract["lease_id"]
        slots = contract.get("slots", [])
        logger.info(
            "Leased batch %s (lease: %s) with %d slots: %s",
            batch_id,
            lease_id[:8],
            len(slots),
            [s.get("doctor_name") for s in slots if not s.get("is_empty")],
        )

        # Heartbeat daemon during rendering simulation
        stop_heartbeat = threading.Event()

        def heartbeat_loop():
            while not stop_heartbeat.wait(self.heartbeat_interval):
                ok = self.send_heartbeat(batch_id, lease_id)
                if not ok:
                    logger.warning("Heartbeat failed for batch %s", batch_id)

        hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        hb_thread.start()

        start_time = time.time()
        try:
            # Simulate Photoshop work delay
            if self.render_delay > 0:
                time.sleep(self.render_delay)

            render_path = self.create_dummy_render_file(batch_id)
            duration_ms = int((time.time() - start_time) * 1000)

            # Submit success result
            ok = self.submit_result(
                batch_id=batch_id,
                lease_id=lease_id,
                success=True,
                print_temp_path=render_path,
                duration_ms=duration_ms,
            )
            logger.info("Batch %s reported 202 Accepted (took %d ms)", batch_id, duration_ms)
            return ok
        finally:
            stop_heartbeat.set()
            hb_thread.join(timeout=2.0)

    def run_simulation(
        self,
        max_batches: int = 50,
        force: bool = False,
        stop_when_empty: bool = True,
        poll_interval: float = 0.2,
    ) -> int:
        completed = 0
        logger.info("Starting fake plugin simulation (target: %d batches)...", max_batches)

        while completed < max_batches:
            contract = self.lease_batch(force=force)
            if contract is None:
                if stop_when_empty:
                    logger.info("Queue empty (204 No Content). Ending simulation.")
                    break
                time.sleep(poll_interval)
                continue

            success = self.run_batch(contract)
            if success:
                completed += 1
            else:
                logger.error("Failed to process batch %s", contract.get("batch_id"))

        logger.info("Simulation finished. Processed %d batches successfully.", completed)
        return completed


def main():
    parser = argparse.ArgumentParser(description="Simulate Photoshop plugin for E2E testing.")
    parser.add_argument("--server", default="http://127.0.0.1:5000", help="Roshta server base URL")
    parser.add_argument("--token", default="secret-token-12345", help="API bearer token")
    parser.add_argument("--batches", type=int, default=50, help="Maximum batches to process")
    parser.add_argument("--delay", type=float, default=0.05, help="Simulated render delay in seconds")
    parser.add_argument("--force", action="store_true", help="Force lease partial batches (< 4 files)")
    parser.add_argument("--format", choices=["tiff", "jpeg"], default="tiff", help="Output render format")
    args = parser.parse_args()

    plugin = FakePhotoshopPlugin(
        base_url=args.server,
        auth_token=args.token,
        render_delay_seconds=args.delay,
        output_format=args.format,
    )

    count = plugin.run_simulation(max_batches=args.batches, force=args.force)
    sys.exit(0 if count > 0 else 1)


if __name__ == "__main__":
    main()
