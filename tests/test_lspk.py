import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from games.baldursgate3 import lspk

META = b'<?xml version="1.0" encoding="UTF-8"?><save><region id="Config"/></save>'


def lz4_literals(data: bytes) -> bytes:
    n = len(data)
    out = bytearray([min(n, 15) << 4])
    if n >= 15:
        rest = n - 15
        out += b"\xff" * (rest // 255) + bytes([rest % 255])
    return bytes(out + data)


def build_pak(
    tmp_path: Path, files: list[tuple[str, bytes, int]], flags: int = 0, part: int = 0
) -> Path:
    body, entries, offset = bytearray(), bytearray(), 40
    for name, data, method in files:
        stored = {0: data, 1: zlib.compress(data), 2: lz4_literals(data), 3: data}[
            method
        ]
        size = 0 if method == 0 else len(data)
        entries += name.encode().ljust(256, b"\0") + struct.pack(
            "<IHBBII", offset + len(body), 0, part, method, len(stored), size
        )
        body += stored
    file_list = lz4_literals(bytes(entries))
    header = struct.pack(
        "<4sIQIBB16sH",
        b"LSPK",
        18,
        40 + len(body),
        8 + len(file_list),
        flags,
        0,
        b"",
        1,
    )
    pak = tmp_path / "test.pak"
    pak.write_bytes(
        header + body + struct.pack("<II", len(files), len(file_list)) + file_list
    )
    return pak


class ReadMetaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_reads_meta_for_each_method(self):
        for method in (0, 1, 2):
            with self.subTest(method=method):
                pak = build_pak(
                    self.tmp,
                    [
                        ("Public/X/a.lsx", b"other" * 50, method),
                        ("Mods/X/meta.lsx", META, method),
                    ],
                )
                self.assertEqual(lspk.read_meta(pak), META)

    def test_override_pak_has_no_meta(self):
        self.assertIsNone(
            lspk.read_meta(build_pak(self.tmp, [("Public/X/a.lsx", b"x", 2)]))
        )

    def test_ignores_meta_outside_mods(self):
        pak = build_pak(
            self.tmp, [("Public/X/meta.lsx", b"wrong", 0), ("Mods/X/meta.lsx", META, 0)]
        )
        self.assertEqual(lspk.read_meta(pak), META)

    def test_unsupported_raises(self):
        cases = {"zstd": (3, 0, 0), "solid": (2, 4, 0), "multipart": (2, 0, 1)}
        for label, (method, flags, part) in cases.items():
            with self.subTest(label), self.assertRaises(lspk.UnsupportedPak):
                lspk.read_meta(
                    build_pak(
                        self.tmp, [("Mods/X/meta.lsx", META, method)], flags, part
                    )
                )

    def test_rejects_other_versions(self):
        pak = build_pak(self.tmp, [("Mods/X/meta.lsx", META, 0)])
        data = pak.read_bytes()
        pak.write_bytes(data[:4] + struct.pack("<I", 16) + data[8:])
        with self.assertRaises(lspk.UnsupportedPak):
            lspk.read_meta(pak)

    def test_lz4_block_overlapping_match(self):
        self.assertEqual(lspk.lz4_block(b"\x35abc\x03\x00", 12), b"abc" * 4)

    def test_list_files_includes_unsupported_paks(self):
        files = [("Mods/X/meta.lsx", META, 3), ("Public\\X\\a.lsx", b"x", 2)]
        for flags in (0, 4):
            with self.subTest(flags=flags):
                pak = build_pak(self.tmp, files, flags)
                self.assertEqual(
                    lspk.list_files(pak), ["Mods/X/meta.lsx", "Public/X/a.lsx"]
                )
