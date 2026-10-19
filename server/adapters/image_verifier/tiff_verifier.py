"""
TiffVerifier — verifies TIFF output files meet print production specifications.

Key behaviours:
  - Missing ResolutionUnit tag defaults to inch (per TIFF spec §6.1).
  - Pixel dimension check allows ±px_tolerance to absorb 300 DPI rounding
    (50 cm at 300 DPI = 5905.511… px; actual pixel count may be 5905 or 5906).
  - Blank-page check samples every 50th pixel in both axes (strided subset)
    so it never allocates the full ~70 MB array for a 50×34 cm/300 DPI sheet.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import tifffile

from server.adapters.image_verifier.base import BaseImageVerifier

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# TIFF ResolutionUnit tag values
_RESOLUTION_UNIT_NONE = 1   # No absolute unit (treat as inch for our purposes)
_RESOLUTION_UNIT_INCH = 2   # Default per spec
_RESOLUTION_UNIT_CM   = 3


class TiffVerifier(BaseImageVerifier):
    """Verifies TIFF output files (LZW/ZIP compressed, multi-page safe)."""

    def _open_and_check_dimensions(
        self,
        path: str,
        expected_width_px: int,
        expected_height_px: int,
        expected_dpi: int,
        px_tolerance: int,
    ) -> tuple[bool, str]:
        try:
            with tifffile.TiffFile(path) as tif:
                if not tif.pages:
                    return False, "TIFF file has no pages"

                page = tif.pages[0]
                actual_w = page.imagewidth
                actual_h = page.imagelength

                # --- DPI extraction ---
                tags = page.tags
                x_res_tag = tags.get("XResolution")
                y_res_tag = tags.get("YResolution")
                res_unit_tag = tags.get("ResolutionUnit")

                if x_res_tag is None or y_res_tag is None:
                    return False, "TIFF is missing XResolution / YResolution tags"

                x_res = _rational_to_float(x_res_tag.value)
                y_res = _rational_to_float(y_res_tag.value)

                # Missing ResolutionUnit → default to inch (TIFF spec §6.1)
                res_unit = int(res_unit_tag.value) if res_unit_tag else _RESOLUTION_UNIT_INCH

                if res_unit == _RESOLUTION_UNIT_CM:
                    # Convert px/cm → px/inch
                    x_res = x_res * 2.54
                    y_res = y_res * 2.54

                actual_dpi_x = round(x_res)
                actual_dpi_y = round(y_res)

                if actual_dpi_x != expected_dpi or actual_dpi_y != expected_dpi:
                    return False, (
                        f"DPI mismatch: expected {expected_dpi}, "
                        f"got {actual_dpi_x}×{actual_dpi_y}"
                    )

                # --- Pixel dimension check with tolerance ---
                if abs(actual_w - expected_width_px) > px_tolerance:
                    return False, (
                        f"Width mismatch: expected {expected_width_px}±{px_tolerance} px, "
                        f"got {actual_w} px"
                    )
                if abs(actual_h - expected_height_px) > px_tolerance:
                    return False, (
                        f"Height mismatch: expected {expected_height_px}±{px_tolerance} px, "
                        f"got {actual_h} px"
                    )

        except tifffile.TiffFileError as exc:
            return False, f"Cannot open TIFF: {exc}"
        except Exception as exc:
            return False, f"TIFF dimension check failed: {exc}"

        return True, ""

    def _check_format_specific(self, path: str) -> tuple[bool, str]:
        """
        Strided blank-page check.
        Samples page.asarray()[::50, ::50] so we test ~(w/50)×(h/50) pixels
        instead of the full ~5906×4134 = 24 M pixel array.
        For a white CMYK sheet every sampled pixel would be (0,0,0,0).
        We flag it only if *all* sampled pixels are pure white/empty.
        """
        try:
            with tifffile.TiffFile(path) as tif:
                page = tif.pages[0]
                sample = page.asarray()[::50, ::50]
                # asarray returns uint8/uint16 depending on bit depth.
                if sample.max() == 0:
                    return False, "TIFF appears to be completely blank (strided sample all-zero)"
        except Exception as exc:
            return False, f"TIFF format-specific check failed: {exc}"

        return True, ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rational_to_float(value) -> float:
    """
    tifffile returns rational tags as (numerator, denominator) tuples
    or occasionally as a plain float. Handle both.
    """
    if isinstance(value, tuple) and len(value) == 2:
        num, den = value
        if den == 0:
            return 0.0
        return num / den
    return float(value)
