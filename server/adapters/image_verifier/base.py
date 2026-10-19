"""
BaseImageVerifier — Template-method ABC for output image verification.

Subclasses must implement:
  _open_and_check_dimensions() — reads the file and validates w/h/dpi
  _check_format_specific()     — any format-level integrity checks

Callers should not instantiate verifiers directly; use ImageVerifierFactory.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseImageVerifier(ABC):
    """
    Template-method base class for all image output verifiers.

    The public `verify()` method enforces the common contract (dimensions
    and DPI tolerance) and then delegates to the subclass for any
    format-specific integrity check (e.g. JPEG SOF frame, TIFF page count).
    """

    def verify(
        self,
        path: str,
        expected_width_px: int,
        expected_height_px: int,
        expected_dpi: int,
        px_tolerance: int = 2,
    ) -> tuple[bool, str]:
        """
        Returns (True, "") on success or (False, reason) on failure.

        Args:
            path:               Absolute path to the output image file.
            expected_width_px:  Expected canvas width in pixels.
            expected_height_px: Expected canvas height in pixels.
            expected_dpi:       Expected resolution (dots per inch).
            px_tolerance:       Maximum allowed pixel deviation on each axis.
                                Default 2 px absorbs 300 DPI rounding artefacts
                                (e.g. 50 cm → 5905.5118 px → rounds to 5906).
        """
        ok, msg = self._open_and_check_dimensions(
            path,
            expected_width_px,
            expected_height_px,
            expected_dpi,
            px_tolerance,
        )
        if not ok:
            return False, msg
        return self._check_format_specific(path)

    # ------------------------------------------------------------------
    # Abstract interface — subclasses must implement both methods.
    # ------------------------------------------------------------------

    @abstractmethod
    def _open_and_check_dimensions(
        self,
        path: str,
        expected_width_px: int,
        expected_height_px: int,
        expected_dpi: int,
        px_tolerance: int,
    ) -> tuple[bool, str]:
        """
        Open the file, extract width, height, and DPI, and verify them
        against the expected values within px_tolerance.
        """

    @abstractmethod
    def _check_format_specific(self, path: str) -> tuple[bool, str]:
        """
        Any additional format-level integrity checks (e.g. page count for
        multi-page TIFFs, SOF frame consistency for JPEGs).
        """
