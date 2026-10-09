import struct
import zlib
from pathlib import Path

_HEADER = struct.Struct("<4sIQIBB16sH")
_ENTRY = struct.Struct("<256sIHBBII")
_SOLID = 0x04


class UnsupportedPak(Exception):
    pass


def lz4_block(src: bytes, size: int) -> bytes:
    out, i = bytearray(), 0
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
        start = len(out) - offset
        for k in range(length + 4):
            out.append(out[start + k])
    if len(out) != size:
        raise UnsupportedPak(f"lz4 size mismatch: {len(out)} != {size}")
    return bytes(out)


def read_meta(pak: Path) -> bytes | None:
    with pak.open("rb") as f:
        sig, version, list_offset, _, flags, _, _, _ = _HEADER.unpack(
            f.read(_HEADER.size)
        )
        if sig != b"LSPK" or version != 18:
            raise UnsupportedPak(f"{pak.name}: signature {sig!r}, version {version}")
        if flags & _SOLID:
            raise UnsupportedPak(f"{pak.name}: solid package")
        f.seek(list_offset)
        count, list_size = struct.unpack("<II", f.read(8))
        entries = lz4_block(f.read(list_size), count * _ENTRY.size)
        for name, off1, off2, part, method, on_disk, size in _ENTRY.iter_unpack(
            entries
        ):
            path = name.split(b"\0", 1)[0].decode("utf-8").replace("\\", "/")
            parts = path.split("/")
            if len(parts) != 3 or parts[0] != "Mods" or parts[2] != "meta.lsx":
                continue
            if part:
                raise UnsupportedPak(f"{pak.name}: meta.lsx in archive part {part}")
            f.seek(off1 | off2 << 32)
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
