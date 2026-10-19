"""
ImageVerifierFactory — resolves the correct verifier by file extension
and exposes a single verify_file() entry point for all callers.

Supported formats:
  .tif / .tiff  → TiffVerifier  (tifffile + strided blank check)
  .jpg / .jpeg  → JpegVerifier  (Pillow JFIF/EXIF + SOF integrity)

Adding a new format:
  1. Create NewFormatVerifier(BaseImageVerifier) in its own module.
  2. Add entries to ImageVerifierFactory._registry.
  No other changes needed.
"""
from __future__ import annotations

import os
import logging

from server.adapters.image_verifier.base import BaseImageVerifier
from server.adapters.image_verifier.tiff_verifier import TiffVerifier
from server.adapters.image_verifier.jpeg_verifier import JpegVerifier

logger = logging.getLogger(__name__)


class ImageVerifierFactory:
    """
    Static factory. Resolves verifier by extension; callers never import
    a concrete verifier class directly.

    Usage:
        ok, reason = ImageVerifierFactory.verify_file(
            path="/tmp/batch_B00142_PRINT.tif",
            expected_width_px=5906,
            expected_height_px=4016,
            expected_dpi=300,
        )
    """

    # Registry maps lowercase extension → singleton verifier instance.
    # Singletons are safe because verifiers are stateless.
    _registry: dict[str, BaseImageVerifier] = {
        ".tif":  TiffVerifier(),
        ".tiff": TiffVerifier(),
        ".jpg":  JpegVerifier(),
        ".jpeg": JpegVerifier(),
    }

    @classmethod
    def verify_file(
        cls,
        path: str,
        expected_width_px: int,
        expected_height_px: int,
        expected_dpi: int,
        px_tolerance: int = 2,
    ) -> tuple[bool, str]:
        """
        Verify an output image file against its expected print specification.

        Returns:
            (True, "")           — file passes all checks.
            (False, reason_str)  — file failed; reason describes the problem.
        """
        ext = os.path.splitext(path)[1].lower()
        verifier = cls._registry.get(ext)

        if verifier is None:
            msg = f"No verifier registered for extension '{ext}' (path: {path})"
            logger.warning(msg)
            return False, msg

        try:
            ok, reason = verifier.verify(
                path,
                expected_width_px,
                expected_height_px,
                expected_dpi,
                px_tolerance,
            )
        except Exception as exc:
            reason = f"Verifier raised an unexpected exception: {exc}"
            logger.exception("Verifier exception for %s", path)
            return False, reason

        if not ok:
            logger.warning("Verification failed for %s: %s", path, reason)

        return ok, reason

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """Returns a sorted list of file extensions this factory can verify."""
        return sorted(cls._registry.keys())
