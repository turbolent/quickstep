"""Debug-only stripping for OPENSTEP's little-endian i386 Mach-O drivers."""

import struct


def strip_driver_debug(data: bytes) -> bytes:
    """Remove STABS, retaining runtime symbols, sections and relocations.

    Accept the legacy MH_PRELOAD layout emitted by OPENSTEP DriverKit: segment
    contents and relocation records precede a final symbol/string table. Keep
    thread commands opaque (modern LLVM rejects OPENSTEP's thread-state flavor).
    Reject other layouts instead of risking a driver that cannot be linked by
    sarld. Already stripped drivers are returned byte-for-byte unchanged.
    """
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError("cannot strip OPENSTEP driver: " + message)

    def span(offset: int, size: int, end: int) -> None:
        require(0 <= offset <= end and 0 <= size <= end - offset, "invalid file extent")

    require(len(data) >= 28, "truncated Mach-O header")
    magic, cpu, subtype, kind, count, command_bytes, flags = struct.unpack_from("<7I", data)
    require((magic, cpu, kind) == (0xfeedface, 7, 5), "expected an i386 MH_PRELOAD binary")
    span(28, command_bytes, len(data))
    command_end = 28 + command_bytes
    offset = 28
    symtab: tuple[int, int, int, int, int] | None = None
    extents: list[tuple[int, int]] = []
    relocation_extents: list[tuple[int, int]] = []
    relocations: list[int] = []
    for _ in range(count):
        span(offset, 8, command_end)
        command, size = struct.unpack_from("<2I", data, offset)
        require(size >= 8 and size % 4 == 0, "invalid load command size")
        span(offset, size, command_end)
        if command == 1:  # LC_SEGMENT
            require(size >= 56, "truncated segment")
            segment = struct.unpack_from("<II16s8I", data, offset)
            file_offset, file_size, sections = segment[5], segment[6], segment[9]
            require(size == 56 + sections * 68, "invalid section count")
            if file_size:
                extents.append((file_offset, file_size))
            for index in range(sections):
                section = struct.unpack_from("<16s16s9I", data, offset + 56 + index * 68)
                length, start, relocs, nrelocs, section_flags = (
                    section[3], section[4], section[6], section[7], section[8])
                if length and (section_flags & 0xff) != 1:  # S_ZEROFILL has no file data
                    extents.append((start, length))
                if nrelocs:
                    span(relocs, nrelocs * 8, len(data))
                    relocation_extents.append((relocs, nrelocs * 8))
                    relocations.extend(range(relocs, relocs + nrelocs * 8, 8))
        elif command == 2:  # LC_SYMTAB
            require(size == 24 and symtab is None, "invalid or duplicate symbol table")
            symtab = (offset, *struct.unpack_from("<4I", data, offset + 8))
        else:
            require(command in (4, 5), f"unsupported load command {command:#x}")
        offset += size
    require(offset == command_end and symtab is not None, "missing symbol table or invalid commands")
    assert symtab is not None
    command_offset, symbol_offset, symbol_count, string_offset, string_size = symtab
    require(symbol_offset >= command_end, "symbol table overlaps load commands")
    span(symbol_offset, symbol_count * 12, len(data))
    require(string_offset == symbol_offset + symbol_count * 12 and
            string_offset + string_size == len(data), "symbol/string tables must end the file")
    for start, length in extents + relocation_extents:
        span(start, length, symbol_offset)
        require(start >= command_end, "segment or relocation data overlaps load commands")
    for index, (start, length) in enumerate(relocation_extents):
        for other, other_length in extents + relocation_extents[:index]:
            require(start + length <= other or other + other_length <= start,
                    "relocation data overlaps another file extent")
    strings = data[string_offset:]

    def string(index: int) -> bytes:
        require(index < len(strings), "invalid symbol string index")
        end = strings.find(b"\0", index)
        require(end >= 0, "unterminated symbol string")
        return strings[index:end]

    symbols = list(struct.iter_unpack("<IBBHI", data[symbol_offset:string_offset]))
    for strx, typ, section, description, value in symbols:
        string(strx)
        if not (typ & 0xe0) and (typ & 0x0e) == 0x0a:  # N_INDR's value is another string index
            string(value)
    keep = {old: new for new, old in enumerate(i for i, s in enumerate(symbols) if not s[1] & 0xe0)}
    for relocation in relocations:
        address, bits = struct.unpack_from("<II", data, relocation)
        if not address & 0x80000000 and bits & 0x08000000:  # external, non-scattered
            require((bits & 0xffffff) in keep, "relocation references a missing or debug symbol")
    if len(keep) == symbol_count:
        return data

    result = bytearray(data[:symbol_offset])
    for relocation in relocations:
        address, bits = struct.unpack_from("<II", data, relocation)
        if not address & 0x80000000 and bits & 0x08000000:
            struct.pack_into("<I", result, relocation + 4, (bits & 0xff000000) | keep[bits & 0xffffff])
    new_strings = bytearray(4)
    string_indexes: dict[bytes, int] = {b"": 0}

    def intern(index: int) -> int:
        name = string(index)
        if name not in string_indexes:
            string_indexes[name] = len(new_strings)
            new_strings.extend(name + b"\0")
        return string_indexes[name]

    for old in keep:
        strx, typ, section, description, value = symbols[old]
        strx = intern(strx)
        if (typ & 0x0e) == 0x0a:
            value = intern(value)
        result.extend(struct.pack("<IBBHI", strx, typ, section, description, value))
    new_strings.extend(b"\0" * (-len(new_strings) % 4))
    struct.pack_into("<4I", result, command_offset + 8,
                     symbol_offset, len(keep), len(result), len(new_strings))
    result.extend(new_strings)
    return bytes(result)
