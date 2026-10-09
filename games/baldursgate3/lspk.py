import struct
import zlib
from pathlib import Path
from typing import BinaryIO

_HEADER = struct.Struct("<4sIQIBB16s")
_ENTRY = struct.Struct("<256sIHBBII")
_ENTRY15 = struct.Struct("<256sQQQIIII")
_SOLID = 0x04


class UnsupportedPak(Exception):
    pass


def lz4_block(src: bytes, size: int) -> bytes:
    out, i = bytearray(), 0
    try:
        while i < len(src):
            token = src[i]
            i += 1
            length = token >> 4
            if length == 15:
                while src[i] == 255:
                    length += 255
                    i += 1
                length += src[i]
                i += 1
            out += src[i : i + length]
            i += length
            if i >= len(src):
                break
            offset = src[i] | src[i + 1] << 8
            i += 2
            length = token & 15
            if length == 15:
                while src[i] == 255:
                    length += 255
                    i += 1
                length += src[i]
                i += 1
            if not 0 < offset <= len(out):
                raise UnsupportedPak(f"lz4 offset {offset} out of range")
            start = len(out) - offset
            for k in range(length + 4):
                out.append(out[start + k])
    except IndexError:
        raise UnsupportedPak("lz4 block truncated") from None
    if len(out) != size:
        raise UnsupportedPak(f"lz4 size mismatch: {len(out)} != {size}")
    return bytes(out)


def _file_list(
    f: BinaryIO, pak: Path
) -> tuple[bool, list[tuple[str, int, int, int, int, int]]]:
    sig, version, list_offset, _, flags, _, _ = _HEADER.unpack(f.read(_HEADER.size))
    if sig != b"LSPK" or version not in (15, 16, 18):
        raise UnsupportedPak(f"{pak.name}: signature {sig!r}, version {version}")
    f.seek(list_offset)
    count, list_size = struct.unpack("<II", f.read(8))
    entry = _ENTRY if version == 18 else _ENTRY15
    raw = entry.iter_unpack(lz4_block(f.read(list_size), count * entry.size))
    if version == 18:
        rows = [
            (name, off1 | off2 << 32, part, method, on_disk, size)
            for name, off1, off2, part, method, on_disk, size in raw
        ]
    else:
        rows = [
            (name, offset, part, method, on_disk, size)
            for name, offset, on_disk, size, part, method, _, _ in raw
        ]
    return bool(flags & _SOLID), [
        (name.split(b"\0", 1)[0].decode("utf-8").replace("\\", "/"), *rest)
        for name, *rest in rows
    ]


def list_files(pak: Path) -> list[str]:
    with pak.open("rb") as f:
        return [entry[0] for entry in _file_list(f, pak)[1]]


def read_meta(pak: Path) -> bytes | None:
    with pak.open("rb") as f:
        solid, entries = _file_list(f, pak)
        for path, offset, part, method, on_disk, size in entries:
            parts = path.split("/")
            if len(parts) != 3 or parts[0] != "Mods" or parts[2] != "meta.lsx":
                continue
            if solid or part:
                raise UnsupportedPak(f"{pak.name}: solid or archive part {part}")
            f.seek(offset)
            data = f.read(on_disk)
            match method & 0x0F:
                case 0:
                    return data
                case 1:
                    return zlib.decompress(data)
                case 2:
                    return lz4_block(data, size)
                case other:
                    raise UnsupportedPak(f"{pak.name}: compression method {other}")
    return None
