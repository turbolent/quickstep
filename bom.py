"""Read the OPENSTEP 4.2 Indexing Kit BOM format (not macOS BOMStore)."""

import argparse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
import stat
import struct
import sys


class BomError(ValueError):
    """Invalid or unsupported OPENSTEP BOM data."""


@dataclass(frozen=True)
class Architecture:
    cpu_type: int
    cpu_subtype: int
    size: int
    checksum: int


@dataclass(frozen=True)
class Entry:
    path: str
    mode: int
    uid: int
    gid: int
    mtime: int
    inode: int
    size: int = 0
    checksum: int | None = None
    device: int | None = None
    link_target: str | None = None
    hard_link_to: str | None = None
    architectures: tuple[Architecture, ...] = ()


def _slice(data: bytes, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise BomError(f"truncated BOM record at offset {offset:#x}, size {size}")
    return data[offset:offset + size]


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(_slice(data, offset, 2), "big")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(_slice(data, offset, 4), "big")


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def _cstring(data: bytes) -> bytes:
    if not data.endswith(b"\0") or b"\0" in data[:-1]:
        raise BomError("expected one NUL-terminated string")
    return data[:-1]


class _Store:
    PAGE_SIZE = 8192

    def __init__(self, data: bytes) -> None:
        if (len(data) < self.PAGE_SIZE or data[16:24] not in
                (b"\x60\0\0\0\0\0BI", b"\0\0\0\0\0\0BI")
                or data[28:32] != b"allo"):
            raise BomError("not a supported big-endian OPENSTEP IXStore BOM")
        if _u32(data, 32) != len(data) or len(data) % self.PAGE_SIZE:
            raise BomError("invalid IXStore file size")
        # Native mkbom also writes a single 6 KiB map. CD BOMs commonly
        # reserve two 3 KiB copies. The active map starts at 0x800 in both.
        # Do not scan free space for records: it can contain stale copies.
        map_size = _u32(data, 0x800)
        if map_size not in (0xbf0, 0x17f0):
            raise BomError("unsupported IXStore object-map layout")
        self.count = _u32(data, 0x808)
        if not 1 < self.count <= map_size // 8:
            raise BomError("invalid IXStore object count")
        self.data = data

    def object(self, identifier: int) -> bytes:
        if not 0 < identifier < self.count:
            raise BomError(f"invalid IXStore object reference: {identifier}")
        address = _u32(self.data, 0x810 + identifier * 8)
        flags = _u32(self.data, 0x814 + identifier * 8)
        page = _slice(self.data, address & ~(self.PAGE_SIZE - 1), self.PAGE_SIZE)
        slot = address & (self.PAGE_SIZE - 1)
        if flags == 0x80000000 and slot == 1:
            return page
        if flags != 0 or not 0 < slot <= _u32(page, 28):
            raise BomError(f"unsupported IXStore allocation for object {identifier}")
        index, stored_id, offset, size = struct.unpack(
            ">IIHH", _slice(page, 24 + slot * 12, 12))
        if index != slot or stored_id != identifier:
            raise BomError(f"IXStore allocation mismatch for object {identifier}")
        return _slice(page, offset, size)

    def records(self, identifier: int) -> Iterator[tuple[bytes, bytes]]:
        pending = [identifier]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                raise BomError("duplicate or cyclic IXBTree child reference")
            seen.add(current)
            node = self.object(current)
            if _u16(node, 18) & 0x8000:
                count = _u16(node, 26) + 1
                children = [_u32(node, 28 + i * 4) >> 13 for i in range(count)]
                pending.extend(reversed(children))
                continue
            count = _u32(node, 32)
            _slice(node, 36, count * 2)
            for i in range(count):
                offset = 16 + _u16(node, 36 + i * 2)
                if offset < 36 + count * 2:
                    raise BomError("IXBTree record overlaps its offset table")
                key_size = (_u16(node, offset) & 0x7fff) >> 1
                value_size = _u16(node, offset + 2)
                key = _slice(node, offset + 4, key_size)
                value = _slice(node, offset + 4 + key_size, value_size)
                yield key, value

    def directory(self, identifier: int) -> dict[bytes, int]:
        result: dict[bytes, int] = {}
        for key, value in self.records(identifier):
            name = _cstring(key)
            if name in result:
                raise BomError(f"duplicate IXStore directory entry: {name!r}")
            _cstring(value[4:])  # Object class name.
            result[name] = _u32(value, 0)
        return result


def _entry(value: bytes) -> Entry:
    mode, uid, gid, mtime, inode = struct.unpack(">HHHII", _slice(value, 0, 14))
    entry = Entry("", mode, uid, gid, mtime, inode)
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFREG:
        size, checksum = _u32(value, 14), _u32(value, 18)
        architectures: tuple[Architecture, ...] = ()
        if len(value) != 22:
            count = _u32(value, 22)
            if len(value) != 26 + count * 16:
                raise BomError("invalid Mach-O architecture metadata length")
            architectures = tuple(Architecture(*struct.unpack(
                ">iiII", _slice(value, 26 + i * 16, 16))) for i in range(count))
        return replace(entry, size=size, checksum=checksum, architectures=architectures)
    if kind == stat.S_IFLNK:
        target = _cstring(value[14:])
        # lsbom reports the traditional 16-bit rotating BSD sum for symlinks.
        checksum = 0
        for byte in target:
            checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + byte) & 0xffff
        return replace(entry, size=len(target), checksum=checksum, link_target=_text(target))
    if kind == stat.S_IFDIR and len(value) == 18:
        return replace(entry, size=_u32(value, 14))
    if kind in (stat.S_IFCHR, stat.S_IFBLK) and len(value) == 16:
        return replace(entry, device=_u16(value, 14))
    raise BomError(f"unsupported BOM record: mode {mode:o}, length {len(value)}")


def parse(data: bytes) -> list[Entry]:
    """Decode the complete inventory, including hard-link aliases, by path."""
    store = _Store(data)
    try:
        root = store.directory(1)[b"DefaultUnixBTree"]
        directory = store.directory(root)
        info = dict(store.records(directory[b"MiscInfo"]))
        if info.get(b"BOM Format Version") != b"1\0":
            raise BomError("unsupported BOM format version")
        nodes: dict[int, tuple[int, str, Entry]] = {}
        for key, value in store.records(directory[b"Main"]):
            parent = _u32(key, 0)
            name = _text(key[4:])
            if not name or "/" in name or "\0" in name or name == "..":
                raise BomError(f"invalid BOM basename: {name!r}")
            entry = _entry(value)
            if entry.inode in nodes or entry.inode == 1:
                raise BomError(f"duplicate or reserved BOM inode: {entry.inode}")
            nodes[entry.inode] = (parent, name, entry)

        paths: dict[int, str] = {1: ""}
        for inode in nodes:
            trail: list[int] = []
            seen: set[int] = set()
            current = inode
            while current not in paths:
                if current in seen or current not in nodes:
                    raise BomError(f"invalid parent chain for BOM inode {inode}")
                seen.add(current)
                trail.append(current)
                parent = nodes[current][0]
                if parent != 1 and (parent not in nodes or
                                    not stat.S_ISDIR(nodes[parent][2].mode)):
                    raise BomError(f"invalid parent directory for BOM inode {current}")
                current = parent
            for current in reversed(trail):
                parent, name, _ = nodes[current]
                paths[current] = paths[parent] + ("/" if paths[parent] else "") + name

        entries = {paths[inode]: replace(node[2], path=paths[inode])
                   for inode, node in nodes.items()}
        if len(entries) != len(nodes) or "." not in entries:
            raise BomError("duplicate paths or missing root directory")
        aliases: dict[str, str] = {}
        for key, value in store.records(directory[b"Links"]):
            path = _text(key)
            if not value or value[0] not in (0, 1):
                raise BomError("invalid hard-link record")
            if value[0] == 0:
                target = _text(_cstring(value[1:]))
                if path in entries or path in aliases:
                    raise BomError(f"duplicate hard-link path: {path!r}")
                aliases[path] = target
        for path, target in aliases.items():
            visited = {path}
            while target not in entries:
                if target in visited or target not in aliases:
                    raise BomError(f"invalid hard-link chain for {path!r}")
                visited.add(target)
                target = aliases[target]
            original = entries[target]
            entries[path] = replace(original, path=path,
                                    hard_link_to=original.hard_link_to or target)
        return sorted(entries.values(), key=lambda entry: entry.path)
    except KeyError as exc:
        raise BomError(f"missing BOM table: {exc}") from exc


def format_entry(entry: Entry) -> str:
    """The tab-separated fields emitted by OPENSTEP's unfiltered lsbom."""
    fields = [entry.path, f"{entry.mode:o}", f"{entry.uid}/{entry.gid}"]
    if entry.checksum is not None:
        checksum = f"{entry.checksum:05d}" if entry.link_target is not None else str(entry.checksum)
        fields.extend((str(entry.size), checksum))
    if entry.device is not None:
        fields.append(str(entry.device))
    if entry.link_target is not None:
        fields.append(entry.link_target)
    return "\t".join(fields)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("bom", type=Path)
    parser.add_argument("-s", "--paths-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        entries = parse(args.bom.read_bytes())
        for entry in entries:
            print(entry.path if args.paths_only else format_entry(entry))
    except (OSError, BomError) as exc:
        print(f"bom.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
