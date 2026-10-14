"""EXIF tag numbers shared by the photo reader and the metadata writer."""

from __future__ import annotations

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
T_MAKE, T_MODEL, T_ORIENTATION, T_DATETIME = 0x010F, 0x0110, 0x0112, 0x0132
T_DT_ORIGINAL, T_FOCAL, T_FOCAL35, T_LENS = 0x9003, 0x920A, 0xA405, 0xA434
