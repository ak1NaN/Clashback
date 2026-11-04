"""Read the game's LZMA-compressed CSV/TOML without changing native IDs."""
import csv
import io
import lzma
from pathlib import Path
import struct


def unpack(data):
    if len(data) < 9:
        raise ValueError("Truncated game table")
    prop = data[0]; q = prop // 9
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
        "id": lzma.FILTER_LZMA1, "dict_size": struct.unpack_from("<I", data, 1)[0],
        "lc": prop % 9, "lp": q % 5, "pb": q // 5}])
    size = struct.unpack_from("<I", data, 5)[0]
    if size > 64 * 1024 * 1024:
        raise ValueError("Unexpectedly large game table")
    result = decoder.decompress(data[9:], max_length=size+1)
    if len(result) == size+1 and result[-1:] == b"\0":
        result = result[:size]
    if len(result) != size:
        raise ValueError("Game table length mismatch")
    return result


def text(path):
    return unpack(Path(path).read_bytes()).decode("utf-8")


def table(path):
    rows = list(csv.DictReader(io.StringIO(text(path))))
    return [r for r in rows[1:] if r.get("Name")]


def repack(original, value):
    """Preserve the game's raw-LZMA property/dictionary header when editing text."""
    data = value.encode("utf-8")
    prop = original[0]; q = prop // 9
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack_from("<I", original, 1)[0],
                "lc": prop % 9, "lp": q % 5, "pb": q // 5}]
    packed = original[:5] + struct.pack("<I", len(data)) + lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)
    if unpack(packed) != data: raise ValueError("Game table compression round-trip failed")
    return packed
