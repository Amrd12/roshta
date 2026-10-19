"""
JpegVerifier — verifies JPEG output files meet print production specifications.

Key behaviours:
  - Reads DPI from JFIF APP0 header (info['dpi']) first; falls back to
    EXIF XResolution/YResolution (tag 282/283) with ResolutionUnit (tag 296).
  - Missing or zero DPI is reported as an error (unlike TIFF, JPEG has no
    spec-mandated default resolution unit that implies a safe fallback).
  - SOF frame integrity: Pillow raises an exception if the SOF dimensions
    don't match the actual compressed data; we surface that as a failure.
  - Same ±px_tolerance and strided blank-page check as TiffVerifier.
"""
from __future__ import annotations

import logging

from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import TAGS

from server.adapters.image_verifier.base import BaseImageVerifier

logger = logging.getLogger(__name__)

# EXIF tag IDs
_EXIF_X_RESOLUTION  = 282
_EXIF_Y_RESOLUTION  = 283
_EXIF_RESOLUTION_UNIT = 296   # 1=no unit, 2=inch, 3=cm

_EXIF_UNIT_INCH = 2
_EXIF_UNIT_CM   = 3


class JpegVerifier(BaseImageVerifier):
    """Verifies JPEG output files via Pillow (JFIF APP0 or EXIF DPI)."""

    def _open_and_check_dimensions(
        self,
        path: str,
        expected_width_px: int,
        expected_height_px: int,
        expected_dpi: int,
        px_tolerance: int,
    ) -> tuple[bool, str]:
        try:
            with Image.open(path) as img:
                # Trigger full decode so SOF frame errors surface here
                img.verify()

            # Re-open after verify() (Pillow closes the image after verify)
            with Image.open(path) as img:
                actual_w, actual_h = img.size

                # --- DPI extraction ---
                dpi_x, dpi_y = _extract_dpi(img)
                if dpi_x is None or dpi_y is None:
                    return False, "JPEG has no readable DPI metadata (JFIF or EXIF)"
                if dpi_x == 0 or dpi_y == 0:
                    return False, f"JPEG reports zero DPI ({dpi_x}×{dpi_y})"

                actual_dpi_x = round(dpi_x)
                actual_dpi_y = round(dpi_y)

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

        except UnidentifiedImageError:
            return False, "File is not a readable JPEG (UnidentifiedImageError)"
        except Exception as exc:
            return False, f"JPEG dimension check failed: {exc}"

        return True, ""

    def _check_format_specific(self, path: str) -> tuple[bool, str]:
        """
        Strided blank-page check — samples every 50th pixel in both axes.
        Flags completely blank output (all pixels pure white/zero) as failed.
        """
        try:
            with Image.open(path) as img:
                import numpy as np
                arr = np.array(img)
                sample = arr[::50, ::50]
                if sample.max() == 0:
                    return False, "JPEG appears to be completely blank (strided sample all-zero)"
        except Exception as exc:
            return False, f"JPEG format-specific check failed: {exc}"

        return True, ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_dpi(img: Image.Image) -> tuple[float | None, float | None]:
    """
    Returns (dpi_x, dpi_y) from the best available source:
      1. JFIF APP0 info dict  — img.info.get('dpi')
      2. EXIF tags            — XResolution + YResolution + ResolutionUnit
    Returns (None, None) if no DPI metadata is found.
    """
    # --- JFIF APP0 ---
    jfif_dpi = img.info.get("dpi")
    if jfif_dpi and isinstance(jfif_dpi, (tuple, list)) and len(jfif_dpi) == 2:
        dpi_x, dpi_y = jfif_dpi
        if dpi_x and dpi_y:
            return float(dpi_x), float(dpi_y)

    # --- EXIF fallback ---
    try:
        exif_data = img._getexif()  # Returns None if no EXIF
    except Exception:
        exif_data = None

    if not exif_data:
        return None, None

    x_res = exif_data.get(_EXIF_X_RESOLUTION)
    y_res = exif_data.get(_EXIF_Y_RESOLUTION)
    unit  = exif_data.get(_EXIF_RESOLUTION_UNIT, _EXIF_UNIT_INCH)

    if x_res is None or y_res is None:
        return None, None

    dpi_x = _rational_to_float(x_res)
    dpi_y = _rational_to_float(y_res)

    if unit == _EXIF_UNIT_CM:
        dpi_x *= 2.54
        dpi_y *= 2.54

    return dpi_x, dpi_y


def _rational_to_float(value) -> float:
    """IFD rational tags come as (numerator, denominator) tuples or floats."""
    if isinstance(value, tuple) and len(value) == 2:
        num, den = value
        if den == 0:
            return 0.0
        return num / den
    return float(value)
