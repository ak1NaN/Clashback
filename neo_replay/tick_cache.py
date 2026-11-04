"""Checked, bounded storage for native battle snapshot bytes, without native imports.

The index is the commit marker: it is published only after the complete payload.
Existing caches are never overwritten. A reader keeps the verified payload open.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zlib

SCHEMA = 1
MAX_TICKS = 7201
MAX_RAW_SIZE = 1 << 20
MAX_TOTAL_SIZE = 256 << 20
MAX_INDEX_SIZE = 8 << 20
PAYLOAD_NAME = "snapshot.bin"
INDEX_NAME = "snapshot-index.json"


class TickCacheError(ValueError):
    """The snapshot container is incomplete, incompatible, or corrupt."""


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise TickCacheError(f"Invalid {name}: {value!r}")
    return value


def _digest(value, name):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise TickCacheError(f"Invalid {name}")
    return value


def _metadata(metadata):
    if not isinstance(metadata, dict):
        raise TickCacheError("Snapshot metadata must be an object")
    if not isinstance(metadata.get("build"), str) or not metadata["build"]:
        raise TickCacheError("Snapshot build must be a nonempty string")
    _digest(metadata.get("libg_sha256"), "libg_sha256")
    _digest(metadata.get("replay_sha256"), "replay_sha256")


class TickCacheWriter:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        if any((self.directory / name).exists() for name in (PAYLOAD_NAME, INDEX_NAME)):
            raise FileExistsError("Snapshot cache already exists; choose a new directory")
        self._temporary = tempfile.TemporaryDirectory(prefix=".snapshot-", dir=self.directory)
        self._temp = Path(self._temporary.name)
        self._file = (self._temp / PAYLOAD_NAME).open("wb")
        self._hash = hashlib.sha256()
        self._entries = []
        self._raw_total = self._size = 0
        self._closed = False

    def add(self, tick, data):
        if self._closed:
            raise TickCacheError("Snapshot writer is closed")
        try:
            _integer(tick, "tick")
            if len(self._entries) >= MAX_TICKS:
                raise TickCacheError("Too many snapshot ticks")
            if self._entries and tick != self._entries[-1]["tick"] + 1:
                raise TickCacheError("Snapshot ticks must be consecutive")
            if not isinstance(data, bytes):
                raise TickCacheError("Snapshot data must be bytes")
            if not 0 < len(data) <= MAX_RAW_SIZE:
                raise TickCacheError("Snapshot raw size exceeds bounds")
            if self._raw_total + len(data) > MAX_TOTAL_SIZE:
                raise TickCacheError("Snapshot raw total exceeds limit")
            compressed = zlib.compress(data)
            if self._size + len(compressed) > MAX_TOTAL_SIZE:
                raise TickCacheError("Snapshot payload exceeds limit")
            self._file.write(compressed)
            self._entries.append({"tick": tick, "offset": self._size,
                                  "compressed_size": len(compressed), "raw_size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
            self._hash.update(compressed)
            self._size += len(compressed)
            self._raw_total += len(data)
        except Exception:
            self.close()
            raise

    def finish(self, metadata):
        if self._closed:
            raise TickCacheError("Snapshot writer is closed")
        published_payload = False
        try:
            _metadata(metadata)
            if not self._entries:
                raise TickCacheError("Snapshot cache cannot be empty")
            reserved = {"schema", "first_tick", "last_tick", "entries", "payload_sha256"}
            if reserved.intersection(metadata):
                raise TickCacheError("Snapshot metadata contains reserved container fields")
            index = {**metadata, "schema": SCHEMA, "first_tick": self._entries[0]["tick"],
                     "last_tick": self._entries[-1]["tick"], "entries": self._entries,
                     "payload_sha256": self._hash.hexdigest()}
            encoded = (json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(encoded) > MAX_INDEX_SIZE:
                raise TickCacheError("Snapshot index exceeds limit")
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            with (self._temp / INDEX_NAME).open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard links publish a complete file atomically and refuse to replace an
            # existing destination, including another writer racing this one.
            os.link(self._temp / PAYLOAD_NAME, self.directory / PAYLOAD_NAME)
            published_payload = True
            os.link(self._temp / INDEX_NAME, self.directory / INDEX_NAME)
            return index
        except Exception:
            if published_payload:
                (self.directory / PAYLOAD_NAME).unlink(missing_ok=True)
            raise
        finally:
            self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            self._file.close()
            self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class TickCacheReader:
    def __init__(self, directory, *, expected_engine, expected_replay):
        self.directory = Path(directory)
        self._file = None
        try:
            _digest(expected_engine, "expected_engine")
            _digest(expected_replay, "expected_replay")
            with (self.directory / INDEX_NAME).open("rb") as stream:
                encoded = stream.read(MAX_INDEX_SIZE + 1)
            if len(encoded) > MAX_INDEX_SIZE:
                raise TickCacheError("Snapshot index exceeds limit")
            index = json.loads(encoded)
            _metadata(index)
            if type(index.get("schema")) is not int or index["schema"] != SCHEMA:
                raise TickCacheError("Unsupported snapshot schema")
            if index["libg_sha256"] != expected_engine:
                raise TickCacheError("Snapshot engine hash mismatch")
            if index["replay_sha256"] != expected_replay:
                raise TickCacheError("Snapshot replay hash mismatch")
            self.first_tick = _integer(index.get("first_tick"), "first_tick")
            self.last_tick = _integer(index.get("last_tick"), "last_tick", self.first_tick)
            entries = index.get("entries")
            if not isinstance(entries, list) or not 0 < len(entries) <= MAX_TICKS:
                raise TickCacheError("Invalid snapshot entry count")
            if len(entries) != self.last_tick - self.first_tick + 1:
                raise TickCacheError("Snapshot tick range is not consecutive")
            offset = raw_total = 0
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise TickCacheError("Invalid snapshot entry")
                if _integer(entry.get("tick"), "entry tick") != self.first_tick + i:
                    raise TickCacheError("Snapshot entry ticks are not consecutive")
                if _integer(entry.get("offset"), "entry offset") != offset:
                    raise TickCacheError("Snapshot offsets are not contiguous")
                offset += _integer(entry.get("compressed_size"), "compressed_size", 1, MAX_TOTAL_SIZE)
                raw_total += _integer(entry.get("raw_size"), "raw_size", 1, MAX_RAW_SIZE)
                if offset > MAX_TOTAL_SIZE or raw_total > MAX_TOTAL_SIZE:
                    raise TickCacheError("Snapshot total exceeds limit")
                _digest(entry.get("sha256"), "entry sha256")
            _digest(index.get("payload_sha256"), "payload_sha256")
            self._file = (self.directory / PAYLOAD_NAME).open("rb")
            if os.fstat(self._file.fileno()).st_size != offset:
                raise TickCacheError("Snapshot payload size mismatch")
            if hashlib.file_digest(self._file, "sha256").hexdigest() != index["payload_sha256"]:
                raise TickCacheError("Snapshot payload hash mismatch")
            self._entries = entries
            self.index = index
        except (OSError, ValueError, TypeError) as error:
            self.close()
            if isinstance(error, TickCacheError):
                raise
            raise TickCacheError(f"Cannot open snapshot cache: {error}") from error

    def get(self, tick):
        _integer(tick, "tick")
        if self._file is None:
            raise TickCacheError("Snapshot reader is closed")
        if not self.first_tick <= tick <= self.last_tick:
            raise KeyError(tick)
        entry = self._entries[tick - self.first_tick]
        self._file.seek(entry["offset"])
        compressed = self._file.read(entry["compressed_size"])
        if len(compressed) != entry["compressed_size"]:
            raise TickCacheError("Truncated snapshot block")
        try:
            decoder = zlib.decompressobj()
            data = decoder.decompress(compressed, entry["raw_size"] + 1)
        except zlib.error as error:
            raise TickCacheError("Invalid compressed snapshot block") from error
        # Never call unbounded flush(): a corrupt or forged stream must not expand
        # beyond the declared, already bounded raw size.
        if len(data) != entry["raw_size"] or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise TickCacheError("Snapshot decompression length or stream mismatch")
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise TickCacheError("Snapshot raw hash mismatch")
        return data

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
