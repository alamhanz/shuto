import io

from PIL import Image, IptcImagePlugin

from shuto.metadata.embed import embed_metadata


def _jpeg_with_exif() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _scan(b: bytes) -> bytes:
    return b[b.index(b"\xff\xda") :]


def test_embed_is_lossless_and_readable():
    src = _jpeg_with_exif()
    out = embed_metadata(src, "Torii & <gates>", "Torii & <gates> 京都", ["torii", "伏見稲荷"])
    assert _scan(out) == _scan(src)
    with Image.open(io.BytesIO(out)) as im:
        im.load()
        assert im.getexif()[0x010F] == "TestCam"
        iptc = IptcImagePlugin.getiptcinfo(im)
        assert iptc[(2, 5)] == b"Torii & <gates>"
        assert iptc[(2, 120)] == "Torii & <gates> 京都".encode()
        assert iptc[(2, 25)] == [b"torii", "伏見稲荷".encode()]
        xmp = im.info["xmp"].decode()
        assert "Torii &amp; &lt;gates&gt;" in xmp and "<rdf:li>伏見稲荷</rdf:li>" in xmp


def test_embed_replaces_existing_metadata():
    once = embed_metadata(_jpeg_with_exif(), "old", "old", ["old"])
    twice = embed_metadata(once, "new", "new", ["new"])
    assert twice.count(b"Photoshop 3.0") == 1
    assert twice.count(b"http://ns.adobe.com/xap/1.0/") == 1
    assert b"old" not in twice
