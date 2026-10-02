"""Write title/description/keywords into a JPEG's IPTC (APP13) and XMP (APP1) segments.

Lossless: only metadata segments are rewritten, the compressed image data is untouched.
Existing IPTC and XMP blocks are replaced; EXIF and ICC profiles are kept.
Shutterstock (like most agencies) reads these fields when the file arrives over FTPS.
"""

from __future__ import annotations

import struct
from xml.sax.saxutils import escape

XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXT_HEADER = b"http://ns.adobe.com/xmp/extension/\x00"
PS_HEADER = b"Photoshop 3.0\x00"
EXIF_HEADER = b"Exif\x00\x00"
ICC_HEADER = b"ICC_PROFILE\x00"

APP0, APP1, APP2, APP13, SOS = 0xE0, 0xE1, 0xE2, 0xED, 0xDA


def _truncate_utf8(text: str, max_bytes: int) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return raw
    return raw[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")


def _iim(record: int, dataset: int, value: bytes) -> bytes:
    return struct.pack(">BBBH", 0x1C, record, dataset, len(value)) + value


def build_iptc(title: str, description: str, keywords: list[str]) -> bytes:
    """APP13 payload with an IPTC-IIM block (UTF-8)."""
    iim = _iim(1, 90, b"\x1b%G")  # coded character set = UTF-8
    iim += _iim(2, 0, b"\x00\x04")  # record version
    iim += _iim(2, 5, _truncate_utf8(title, 64))  # Object Name (title)
    iim += _iim(2, 120, _truncate_utf8(description, 2000))  # Caption/Abstract
    for kw in keywords:
        iim += _iim(2, 25, _truncate_utf8(kw, 64))  # Keywords (repeatable)
    if len(iim) % 2:
        iim += b"\x00"
    resource = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(iim))
    return PS_HEADER + resource + iim


def build_xmp(title: str, description: str, keywords: list[str]) -> bytes:
    """APP1 payload with a minimal XMP packet (dc:title, dc:description, dc:subject)."""
    t, d = escape(title), escape(description)
    items = "".join(f"<rdf:li>{escape(k)}</rdf:li>" for k in keywords)
    xml = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{t}</rdf:li></rdf:Alt></dc:title>'
        f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{d}</rdf:li></rdf:Alt>'
        "</dc:description>"
        f"<dc:subject><rdf:Bag>{items}</rdf:Bag></dc:subject>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )
    return XMP_HEADER + xml.encode("utf-8")


def _segment(marker: int, payload: bytes) -> bytes:
    if len(payload) + 2 > 0xFFFF:
        raise ValueError("metadata segment too large")
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def embed_metadata(jpeg: bytes, title: str, description: str, keywords: list[str]) -> bytes:
    if jpeg[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    head: list[bytes] = []  # leading APP0 / EXIF / ICC segments (kept in place)
    rest: list[bytes] = []  # every other pre-scan segment
    pos = 2
    while True:
        if jpeg[pos] != 0xFF:
            raise ValueError(f"bad JPEG marker at {pos}")
        while jpeg[pos + 1] == 0xFF:  # fill bytes
            pos += 1
        marker = jpeg[pos + 1]
        if marker == SOS:
            scan = jpeg[pos:]
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # standalone markers
            rest.append(jpeg[pos : pos + 2])
            pos += 2
            continue
        (length,) = struct.unpack(">H", jpeg[pos + 2 : pos + 4])
        seg = jpeg[pos : pos + 2 + length]
        payload = seg[4:]
        pos += 2 + length

        is_xmp = marker == APP1 and payload.startswith((XMP_HEADER, XMP_EXT_HEADER))
        is_iptc = marker == APP13 and payload.startswith(PS_HEADER)
        if is_xmp or is_iptc:
            continue  # replaced below
        is_head = (
            marker == APP0
            or (marker == APP1 and payload.startswith(EXIF_HEADER))
            or (marker == APP2 and payload.startswith(ICC_HEADER))
        )
        (head if is_head and not rest else rest).append(seg)

    new = [
        _segment(APP1, build_xmp(title, description, keywords)),
        _segment(APP13, build_iptc(title, description, keywords)),
    ]
    return b"\xff\xd8" + b"".join(head + new + rest) + scan
