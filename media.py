"""Offline OPENSTEP media and driver operations.

UFS access is delegated to the nextufs executable.
See build.py for an example CD/USB recipe using this library.
"""

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from typing import TypeAlias

from macho import strip_driver_debug


ROOT = "/private/Drivers/i386"
MANIFEST = ".media-metadata"
__all__ = [
    "DriverImage", "MediaError", "PathInput", "DiskLabel", "FilesystemInfo", "ImageInfo", "IsoLayout",
    "nextufs", "new_output", "resolve_iso_tool", "image_info", "cd_layout", "extract_ufs", "grow_image",
    "check_image", "pad_image", "copy_kernel", "patch_pic_kernel", "patch_kernel_pic_bug", "create_iso", "iso_layout", "check_payload",
    "prepare_installation_drivers", "verify_boot_cd",
    "UsbLayout", "prepare_usb_installer", "create_usb", "usb_layout", "verify_boot_usb",
    "skip_boot_language_selection",
    "remove_boot_languages",
    "remove_language_packages", "patch_builddisk_language_heading", "patch_builddisk_capacity",
    "fix_builddisk_capacity",
    "SetupPackage", "SetupChoice", "SetupCatalog", "setup_plist",
    "validate_setup_app", "prepare_setup_app", "verify_setup_app",
    "copy_directory", "verify_directory_copy",
    "copy_tar", "verify_tar_copy", "tar_entries",
    "MAX_GROWN_FLOPPY_KIB", "MAX_BOOT_FLOPPY_KIB",
]
PathInput: TypeAlias = str | os.PathLike[str]


class MediaError(Exception):
    """An operational or validation error from the media API."""


def component(name: str) -> str:
    """Accept a single image path component, never a path or NUL."""
    if not name or name in (".", "..") or any(c in name for c in "/\\\0"):
        raise MediaError(f"invalid path component: {name!r}")
    return name


def driver_name(name: str) -> str:
    if not isinstance(name, str):
        raise MediaError("driver name must be a string")
    if name.endswith(".config"):
        name = name[:-7]
    component(name)
    if any(c.isspace() for c in name):
        raise MediaError("driver names cannot contain whitespace")
    return name


@dataclass(frozen=True)
class Entry:
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    atime: int
    mtime: int
    name: str

    def __post_init__(self) -> None:
        values = (self.inode, self.mode, self.uid, self.gid, self.size, self.atime, self.mtime)
        if (any(type(value) is not int or value < 0 for value in values) or
                self.mode > 0xffff or max(self.uid, self.gid) > 0xffff or
                max(self.atime, self.mtime) > 0xffffffff):
            raise MediaError("invalid metadata: expected nonnegative integers within UFS field limits")
        if not isinstance(self.name, str) or "\0" in self.name:
            raise MediaError("invalid metadata filename")

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.mode)


def records(data: str | bytes) -> list[Entry]:
    """Decode browse --json or a JSON extraction manifest into typed entries."""
    try:
        items = json.loads(data)
        if not isinstance(items, list) or not items:
            raise ValueError("expected a nonempty array of metadata entries")
        return [Entry(**item) for item in items]
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MediaError(f"invalid JSON metadata: {exc}") from exc


def records_json(entries: Iterable[Entry]) -> bytes:
    return (json.dumps([asdict(entry) for entry in entries], indent=2) + "\n").encode("utf-8")


def manifest_records(data: bytes) -> list[Entry]:
    """Also accept binary manifests saved by earlier versions of media.py."""
    if not data.endswith(b"\0"):
        return records(data)
    result = []
    for record in data[:-1].split(b"\0"):
        fields = record.split(b"\t", 7)
        try:
            if len(fields) != 8:
                raise ValueError("wrong field count")
            values = [int(value, 8 if i == 1 else 10)
                      for i, value in enumerate(fields[:7])]
            inode, mode, uid, gid, size, atime, mtime = values
            entry = Entry(inode, mode, uid, gid, size, atime, mtime, fields[7].decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise MediaError(f"invalid metadata record: {exc}") from exc
        result.append(entry)
    return result


# A .table is a sequence of quoted assignments or bare quoted flags, not XML.
TOKEN = re.compile(r'\s+|/\*.*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"|[=;]', re.S)


def unquote(token: str) -> str:
    def escape(match: re.Match[str]) -> str:
        value = match[1]
        if value[0] in "01234567":
            return chr(int(value, 8))
        return {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(value, value)
    return re.sub(r"\\([0-7]{1,3}|.)", escape, token[1:-1], flags=re.S)


def quote(value: str) -> str:
    if "\0" in value:
        raise MediaError("table values cannot contain NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
        "\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


class Table:
    """Edit selected entries while preserving unrelated text and comments."""

    def __init__(self, data: bytes) -> None:
        self.text = data.decode("latin-1")
        self.entries: list[tuple[str, str | None, int, int]] = []
        tokens = []
        pos = 0
        while pos < len(self.text):
            match = TOKEN.match(self.text, pos)
            if not match:
                raise MediaError(f"invalid .table syntax at character {pos}")
            token = match[0]
            if not token.isspace() and not token.startswith(("/*", "//")):
                tokens.append((token, pos, match.end()))
            pos = match.end()
        i = 0
        while i < len(tokens):
            key, start, _ = tokens[i]
            if not key.startswith('"'):
                raise MediaError(f"expected quoted key at character {start}")
            i += 1
            value: str | None = None  # A bare flag, e.g. "Boot Driver";
            if i < len(tokens) and tokens[i][0] == "=":
                i += 1
                if i == len(tokens) or not tokens[i][0].startswith('"'):
                    raise MediaError(f"expected quoted value for {key}")
                value = unquote(tokens[i][0])
                i += 1
            if i == len(tokens) or tokens[i][0] != ";":
                raise MediaError(f"expected semicolon after {key}")
            self.entries.append((unquote(key), value, start, tokens[i][2]))
            i += 1

    def get(self, key: str, default: str | None = "") -> str | None:
        return next((value for name, value, _, _ in reversed(self.entries)
                     if name == key), default)

    def set(self, key: str, value: str) -> None:
        replacement = f"{quote(key)} = {quote(value)};"
        matches = [e for e in self.entries if e[0] == key]
        text = self.text
        if matches:
            # Replace the last definition and remove earlier duplicates.
            for _, _, start, end in reversed(matches):
                text = text[:start] + replacement + text[end:]
                replacement = ""
        else:
            newline = "\r\n" if "\r\n" in text else "\n"
            text += ("" if not text or text.endswith("\n") else newline) + replacement + newline
        try:
            updated = Table(text.encode("latin-1"))
        except UnicodeEncodeError as exc:
            raise MediaError("OPENSTEP table values must fit the single-byte encoding") from exc
        self.text, self.entries = updated.text, updated.entries

    def data(self) -> bytes:
        return self.text.encode("latin-1")

    def remove(self, key: str) -> None:
        text = self.text
        for name, _, start, end in reversed(self.entries):
            if name == key:
                text = text[:start] + text[end:]
        updated = Table(text.encode("latin-1"))
        self.text, self.entries = updated.text, updated.entries

    def is_boot(self) -> bool:
        value = self.get("Boot Driver", "No")
        if value is None or value.strip().lower() in ("yes", "true", "1", "boot driver"):
            return True
        if value.strip().lower() in ("no", "false", "0", ""):
            return False
        raise MediaError(f"unrecognized Boot Driver value: {value!r}")


def driver_lists(table: Table) -> list[list[str]]:
    result = []
    for key in ("Boot Drivers", "Active Drivers"):
        value = table.get(key)
        if value is None:
            raise MediaError(f"{key} must have a quoted value")
        names = value.split()
        for name in names:
            driver_name(name)
        result.append(names)
    return result


def activate(table: Table, name: str, boot: bool, dependencies: Iterable[str]) -> None:
    lists = driver_lists(table)
    target = 0 if boot else 1
    dependencies = list(dict.fromkeys(dependencies))
    if name in dependencies:
        raise MediaError("a driver cannot depend on itself")
    for dep in dependencies:
        if dep not in lists[0] and dep not in lists[1]:
            raise MediaError(f"dependency {dep} is not active; activate it first")
        if boot and dep not in lists[0]:
            raise MediaError(f"boot driver {name} cannot depend on active-stage driver {dep}")
    old_position = lists[target].index(name) if name in lists[target] else len(lists[target])
    lists = [[item for item in items if item != name] for items in lists]
    after = max((i + 1 for i, item in enumerate(lists[target])
                 if item in dependencies), default=0)
    lists[target].insert(max(min(old_position, len(lists[target])), after), name)
    for key, items in zip(("Boot Drivers", "Active Drivers"), lists):
        table.set(key, " ".join(items))


def deactivate(table: Table, name: str) -> None:
    for key, items in zip(("Boot Drivers", "Active Drivers"), driver_lists(table)):
        table.set(key, " ".join(item for item in items if item != name))


def executable(explicit: PathInput | None = None) -> str:
    if explicit:
        found = shutil.which(os.fspath(explicit))
        if found:
            return str(Path(found).absolute())
        raise MediaError(f"nextufs executable not found: {explicit}")
    name = "nextufs.exe" if os.name == "nt" else "nextufs"
    root = Path(__file__).resolve().parent
    for candidate in (root / "nextufs" / name, root / name):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise MediaError("build nextufs first, or pass --nextufs PATH")
    return found


class Image:
    """Internal nextufs adapter; use DriverImage for transactional operations."""

    def __init__(self, binary: str, path: PathInput, root: str = ROOT) -> None:
        self.binary = binary
        self.path = Path(path).absolute()
        if not root.startswith("/"):
            raise MediaError("driver root must be an absolute image path")
        for part in root.strip("/").split("/"):
            component(part)
        self.root = root.rstrip("/")

    def nextufs(self, *args: PathInput | int) -> bytes:
        return nextufs(*args, binary=self.binary)

    def inspect(self, path: str) -> list[Entry]:
        entries = records(self.nextufs("browse", "--json", self.path, path))
        if entries[0].name != ".":
            raise MediaError("missing self record in nextufs output")
        names = set()
        for entry in entries[1:]:
            component(entry.name)
            if entry.name in names:
                raise MediaError(f"duplicate directory entry: {entry.name}")
            names.add(entry.name)
        return entries

    def check_root(self) -> None:
        path = ""
        for part in self.root.strip("/").split("/"):
            path += "/" + part
            if not self.inspect(path)[0].is_dir:
                raise MediaError(f"not a directory (symlinks are not followed): {path}")

    def child(self, parent: str, name: str) -> Entry | None:
        return next((e for e in self.inspect(parent)[1:] if e.name == name), None)

    def bundle(self, name: str) -> str:
        path = self.root + "/" + component(name) + ".config"
        if not self.inspect(path)[0].is_dir:
            raise MediaError(f"driver bundle is not a directory: {path}")
        return path

    def read(self, path: str, entry: Entry | None = None) -> bytes:
        if entry is None:
            entry = self.inspect(path)[0]
        if not stat.S_ISREG(entry.mode):
            raise MediaError(f"not a regular file: {path}")
        data = self.nextufs("browse", "--raw", self.path, path)
        if len(data) != entry.size:
            raise MediaError(f"short read: {path}")
        return data

    def mutate(self, operation: str, path: str, *args: PathInput | int) -> None:
        self.nextufs("mkfile", "--" + operation, self.path, path, *args)

    def metadata(self, path: str, entry: Entry) -> None:
        self.mutate("chown", path, entry.uid, entry.gid)
        self.mutate("chmod", path, f"{stat.S_IMODE(entry.mode):o}")
        self.mutate("utimes", path, entry.atime, entry.mtime)

    def write(self, path: str, data: bytes, entry: Entry, exists: bool) -> None:
        with tempfile.TemporaryDirectory(prefix="media-table-") as temp:
            source = Path(temp) / "table"
            source.write_bytes(data)
            if exists:
                self.mutate("unlink", path)
            self.mutate("from-file", path, source)
            self.metadata(path, entry)

    def tree(self, path: str) -> list[Entry]:
        result: list[Entry] = []
        seen: set[int] = set()

        def walk(current: str, relative: str, entry: Entry) -> None:
            if not entry.is_dir and not stat.S_ISREG(entry.mode):
                raise MediaError(f"unsupported symlink or special file: {current}")
            result.append(replace(entry, name=relative))
            if entry.is_dir:
                if entry.inode in seen:
                    raise MediaError(f"directory cycle at {current}")
                seen.add(entry.inode)
                for child in self.inspect(current)[1:]:
                    walk(current + "/" + child.name,
                         child.name if relative == "." else relative + "/" + child.name, child)
        walk(path, ".", self.inspect(path)[0])
        return result


def local_stat(path: Path) -> os.stat_result:
    entry = path.lstat()
    if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400:
        raise MediaError(f"symlinks and reparse points are not supported: {path}")
    return entry


@contextmanager
def transaction(image: Image) -> Iterator[Image]:
    """Commit only a successfully edited sibling copy; never mutate the input."""
    original = local_stat(image.path)
    if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
        raise MediaError("image must be a regular file with one hard link")
    with image.path.open("rb") as source:
        header = source.read(68)
    if header[64:68] == b"\x7f\x10\xda\xbe":
        raise MediaError("transactional writes support raw/labeled images, not VDI containers")
    with tempfile.TemporaryDirectory(prefix=".media-", dir=image.path.parent) as temp:
        copy = Path(temp) / "image.img"
        shutil.copy2(image.path, copy)
        staged = Image(image.binary, copy, image.root)
        staged.check_root()
        yield staged
        current = local_stat(image.path)
        def identity(s: os.stat_result) -> tuple[int, int, int, int, int]:
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
        if identity(current) != identity(original):
            raise MediaError("image changed during the operation; refusing to replace it")
        os.replace(copy, image.path)


def host_component(name: str) -> None:
    component(name)
    if os.name == "nt" and (any(c in name for c in '<>:"|?*') or
                            any(ord(c) < 32 for c in name) or
                            name.endswith((".", " ")) or PureWindowsPath(name).is_reserved()):
        raise MediaError(f"filename cannot be represented on this host: {name!r}")


def _extract(image: Image, name: str, destination: PathInput) -> None:
    bundle = image.bundle(name)
    entries = image.tree(bundle)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise MediaError(f"destination already exists: {destination}")
    paths = set()
    for entry in entries:
        if os.path.normcase(entry.name) == os.path.normcase(MANIFEST):
            raise MediaError(f"bundle uses reserved filename {MANIFEST}")
        if entry.name != ".":
            for part in entry.name.split("/"):
                host_component(part)
        key = os.path.normcase(entry.name)
        if key in paths:
            raise MediaError(f"filenames collide on this host: {entry.name}")
        paths.add(key)
    with tempfile.TemporaryDirectory(prefix=".media-extract-", dir=destination.parent) as temp:
        staged = Path(temp) / "bundle"
        for entry in entries:
            target = staged / entry.name
            if entry.is_dir:
                target.mkdir()
            else:
                with target.open("xb") as output:
                    output.write(image.read(bundle + "/" + entry.name, entry))
        with (staged / MANIFEST).open("xb") as output:
            output.write(records_json(entries))
        # Metadata stays in the manifest, so read-only UFS directories remain editable locally.
        if destination.exists() or destination.is_symlink():
            raise MediaError(f"destination appeared during extraction: {destination}")
        staged.rename(destination)


def local_tree(source: PathInput) -> list[Entry]:
    source = Path(source).absolute()
    if not stat.S_ISDIR(local_stat(source).st_mode):
        raise MediaError("local driver must be a directory")
    saved: dict[str, Entry] = {}
    manifest = source / MANIFEST
    if manifest.exists() or manifest.is_symlink():
        if not stat.S_ISREG(local_stat(manifest).st_mode):
            raise MediaError("metadata manifest must be a regular file")
        for entry in manifest_records(manifest.read_bytes()):
            if entry.name != ".":
                for part in entry.name.split("/"):
                    component(part)
            if entry.name in saved:
                raise MediaError(f"duplicate manifest entry: {entry.name}")
            saved[entry.name] = entry
    result: list[Entry] = []

    def walk(path: Path, relative: str) -> None:
        info = local_stat(path)
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise MediaError(f"unsupported special file: {path}")
        entry = saved.get(relative)
        if entry is not None:
            if stat.S_IFMT(entry.mode) != stat.S_IFMT(info.st_mode):
                raise MediaError(f"file type differs from extraction manifest: {relative}")
        else:
            mode = stat.S_IFMT(info.st_mode) | (0o755 if stat.S_ISDIR(info.st_mode) else 0o644)
            if os.name != "nt":
                mode = info.st_mode
            entry = Entry(0, mode, 0, 0, info.st_size, int(info.st_atime), int(info.st_mtime), relative)
        result.append(entry)
        if entry.is_dir:
            for child in sorted(path.iterdir()):
                if relative == "." and child.name == MANIFEST:
                    continue
                component(child.name)
                walk(child, child.name if relative == "." else relative + "/" + child.name)
    walk(source, ".")
    return result


def _store(image: Image, name: str, source: PathInput, *, strip_debug: bool = False) -> None:
    if name == "System":
        raise MediaError("System.config may be configured, but not replaced")
    source = Path(source).absolute()
    entries = local_tree(source)
    if image.child(image.root, name + ".config") is not None:
        raise MediaError(f"{name}.config already exists; deactivate and remove it first")
    bundle = image.root + "/" + name + ".config"
    with tempfile.TemporaryDirectory(prefix="media-import-") as temp:
        for entry in entries:
            path = bundle if entry.name == "." else bundle + "/" + entry.name
            if entry.is_dir:
                image.mutate("mkdir", path)
            else:
                local = source / entry.name
                if entry.name.endswith(".table"):
                    # NXStringTable rejects CR outside quoted strings. Normalize
                    # line endings without decoding or modifying the source bundle.
                    data = local.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    local = Path(temp) / "table"
                    local.write_bytes(data)
                elif strip_debug and entry.name.endswith("_reloc"):
                    try:
                        data = strip_driver_debug(local.read_bytes())
                    except ValueError as exc:
                        raise MediaError(f"{name}.config/{entry.name}: {exc}") from exc
                    local = Path(temp) / "driver"
                    local.write_bytes(data)
                image.mutate("from-file", path, local)
    for entry in reversed(entries):
        image.metadata(bundle if entry.name == "." else bundle + "/" + entry.name, entry)


def system_table(image: Image) -> tuple[str, Table]:
    path = image.bundle("System") + "/Instance0.table"
    return path, Table(image.read(path))


def _remove(image: Image, name: str) -> None:
    if name == "System":
        raise MediaError("System.config cannot be removed")
    _, table = system_table(image)
    if any(name in items for items in driver_lists(table)):
        raise MediaError(f"{name} is active; deactivate it before removing it")
    bundle = image.bundle(name)
    for entry in reversed(image.tree(bundle)):
        path = bundle if entry.name == "." else bundle + "/" + entry.name
        image.mutate("rmdir" if entry.is_dir else "unlink", path)


def _configure(image: Image, name: str, source_table: str, overwrite: bool,
               settings: Mapping[str, str]) -> None:
    bundle = image.bundle(name)
    instance = image.child(bundle, "Instance0.table")
    target = bundle + "/Instance0.table"
    original = image.read(target, instance) if instance is not None else b""
    if instance is None or overwrite:
        source = bundle + "/" + source_table
        template = image.inspect(source)[0]
        data = image.read(source, template)
        entry = instance or template
    else:
        data = original
        entry = instance
    table = Table(data)
    for key, value in settings.items():
        table.set(key, value)
    if instance is None or original != table.data():
        image.write(target, table.data(), entry, instance is not None)


class DriverImage:
    """Driver operations on one image, with no CLI parsing or console output.

    Paths accept strings or os.PathLike objects. Each write is transactional;
    sequences of calls are not a single transaction. Operational failures raise
    MediaError (with the original exception chained for host I/O failures).
    Construction resolves the executable but does not open or edit the image.
    """

    def __init__(self, image: PathInput, *,
                 nextufs: PathInput | None = None, driver_root: str = ROOT) -> None:
        self._image = Image(executable(nextufs), image, driver_root)

    @contextmanager
    def _operation(self, *, write: bool = False) -> Iterator[Image]:
        try:
            if write:
                with transaction(self._image) as staged:
                    yield staged
            else:
                self._image.check_root()
                yield self._image
        except (OSError, UnicodeError) as exc:
            raise MediaError(str(exc)) from exc

    def list_drivers(self) -> list[str]:
        """Return sorted driver names, excluding System.config."""
        with self._operation() as image:
            return [driver_name(entry.name)
                    for entry in sorted(image.inspect(image.root)[1:], key=lambda e: e.name)
                    if entry.is_dir and entry.name.endswith(".config") and entry.name != "System.config"]

    def extract(self, driver: str, destination: PathInput) -> None:
        """Extract a bundle and its metadata into a new local directory."""
        name = driver_name(driver)
        with self._operation() as image:
            _extract(image, name, destination)

    def store(self, driver: str, source: PathInput, *, strip_debug: bool = False) -> None:
        """Store a local bundle, normalizing .table line endings to Unix LF.

        Preserve source files and metadata; refuse to replace an existing bundle.
        With strip_debug, remove only STABS from *_reloc binaries before import.
        """
        name = driver_name(driver)
        with self._operation(write=True) as image:
            _store(image, name, source, strip_debug=strip_debug)

    def remove(self, driver: str) -> None:
        """Remove an inactive bundle; System.config is protected."""
        name = driver_name(driver)
        with self._operation(write=True) as image:
            _remove(image, name)

    def configure(self, driver: str, *, source_table: str = "Default.table",
                  overwrite: bool = False, settings: Mapping[str, str] | None = None) -> None:
        """Create/edit Instance0.table using a mapping of keys to string values."""
        name = driver_name(driver)
        component(source_table)
        if settings is None:
            settings = {}
        if not isinstance(settings, Mapping):
            raise MediaError("settings must be a mapping of nonempty keys to string values")
        settings = dict(settings)
        for key, value in settings.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise MediaError("settings must be a mapping of nonempty keys to string values")
        with self._operation(write=True) as image:
            _configure(image, name, source_table, overwrite, settings)

    def activate(self, driver: str, *, dependencies: Iterable[str] = ()) -> None:
        """Activate a configured driver after the named, already-active dependencies."""
        name = driver_name(driver)
        if isinstance(dependencies, (str, bytes)) or not isinstance(dependencies, Iterable):
            raise MediaError("dependencies must be an iterable of driver names, not a string")
        self._activation(name, [driver_name(dep) for dep in dependencies])

    def deactivate(self, driver: str) -> None:
        """Remove a driver from both lists, even if its bundle is missing."""
        self._activation(driver_name(driver), None)

    def _activation(self, name: str, dependencies: Iterable[str] | None) -> None:
        if name == "System":
            raise MediaError("System.config is not an activatable driver")
        with self._operation(write=True) as image:
            path, table = system_table(image)
            before = table.data()
            if dependencies is None:
                deactivate(table, name)
            else:
                driver = Table(image.read(image.bundle(name) + "/Instance0.table"))
                activate(table, name, driver.is_boot(), dependencies)
            if before != table.data():
                image.write(path, table.data(), image.inspect(path)[0], True)


# Reusable image preparation and OPENSTEP/El Torito ISO construction.
# Boot-CD workflow limits in KiB, including the front porch; not general UFS limits.
MAX_GROWN_FLOPPY_KIB = 2560
MAX_BOOT_FLOPPY_KIB = 2880


def _subprocess_env(nextufs_binary: PathInput | None = None) -> dict[str, str] | None:
    """Add the nextufs directory, containing its DLLs, to Windows child PATH."""
    if sys.platform != "win32":
        return None
    runtime = Path(executable(nextufs_binary)).absolute().parent
    env = os.environ.copy()
    env["PATH"] = ";".join(filter(None, (str(runtime), env.get("PATH", ""))))
    return env


def nextufs(*args: PathInput | int, binary: PathInput | None = None) -> bytes:
    """Run nextufs without printing; report command failures as MediaError."""
    command = [executable(binary), *map(str, args)]
    completed = subprocess.run(command, capture_output=True,
                               env=_subprocess_env(binary))
    if completed.returncode:
        # fsck writes its errors to stdout, whereas other subcommands use stderr.
        details = "\n".join(part.decode("utf-8", errors="replace").strip()
                            for part in (completed.stdout, completed.stderr) if part.strip())
        error = f"nextufs {' '.join(command[1:])} exited with status {completed.returncode}"
        raise MediaError(error + (f":\n{details}" if details else ""))
    return completed.stdout


def grow_image(image: PathInput, size_kib: int, *,
               nextufs_binary: PathInput | None = None) -> None:
    """Grow a disposable image and its UFS in place to size_kib KiB."""
    if size_kib <= 0:
        raise ValueError("image size must be positive")
    nextufs("resize", "grow", image, size_kib, binary=nextufs_binary)


def check_image(image: PathInput, *, nextufs_binary: PathInput | None = None) -> None:
    """Run read-only fsck; raise MediaError on failure."""
    nextufs("fsck", "-n", image, binary=nextufs_binary)


def pad_image(image: PathInput, size_kib: int) -> None:
    """Append zero padding in place without changing UFS geometry; never shrink."""
    with Path(image).open("r+b") as stream:
        if size_kib * 1024 < os.fstat(stream.fileno()).st_size:
            raise ValueError("padding would shrink the image")
        stream.truncate(size_kib * 1024)


@dataclass(frozen=True)
class DiskLabel:
    name: str
    version: int
    offset: int
    sector_size: int
    front_porch_sectors: int
    root_partition: str | None


@dataclass(frozen=True)
class FilesystemInfo:
    magic: int
    block_size: int
    fragment_size: int
    fragments_per_block: int
    fragment_count: int
    data_fragment_count: int
    cylinder_groups: int
    cylinders_per_group: int
    inodes_per_group: int
    fragments_per_group: int
    free_blocks: int
    free_fragments: int
    free_inodes: int


@dataclass(frozen=True)
class ImageInfo:
    """The nextufs info --json report, including its nested objects."""

    source: str
    source_kind: str
    backing_bytes: int
    image_bytes: int
    slice_base: int
    slice_bytes: int
    superblock_base: int
    filesystem_bytes: int
    trailing_slice_slack: int
    compatibility_ceiling_bytes: int
    cylinder_summary_capacity_groups: int
    used_disk_label: bool
    label: DiskLabel
    filesystem: FilesystemInfo

    @classmethod
    def from_json(cls, data: str | bytes) -> "ImageInfo":
        try:
            values = json.loads(data)
            values["label"] = DiskLabel(**values["label"])
            values["filesystem"] = FilesystemInfo(**values["filesystem"])
            return cls(**values)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid nextufs info report: {exc}") from exc


@contextmanager
def new_output(output: PathInput, *, source: PathInput | None = None) -> Iterator[Path]:
    """Stage a new output beside its destination; publish only on success."""
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output already exists: {output}; refusing to overwrite it. Choose a different output path.")
    with tempfile.TemporaryDirectory(prefix=".media-build-", dir=output.parent) as temp:
        staged = Path(temp) / "image"
        if source is not None:
            shutil.copyfile(source, staged)
        yield staged
        try:
            # Windows rename refuses to replace an existing destination. POSIX
            # rename would overwrite it, so publish with an atomic hard link;
            # temporary-directory cleanup removes the staging link afterward.
            if sys.platform == "win32":
                staged.rename(output)
            else:
                os.link(staged, output)
        except FileExistsError as exc:
            raise FileExistsError(f"output appeared during the operation: {output}; refusing to overwrite it.") from exc


def skip_boot_language_selection(image: PathInput, *, nextufs_binary: PathInput | None = None) -> None:
    """Use the install bootloader's English fallback without disabling install mode.

    pickLanguage() ignores System.config's Language setting in install mode.
    Removing Language.table's Languages key selects English without a menu;
    an empty or single-language value would still enter the menu code.
    Keep the file and its other entries, and retain the installation confirmation.
    """
    with transaction(Image(executable(nextufs_binary), image, root="/usr/standalone/i386")) as staged:
        path = "/usr/standalone/i386/Language.table"
        entry = staged.inspect(path)[0]
        original = staged.read(path, entry)
        table = Table(original)
        table.remove("Languages")
        if table.data() != original:
            staged.write(path, table.data(), entry, exists=True)
        if staged.read(path) != table.data():
            raise ValueError("boot language table readback failed")


def remove_boot_languages(image: PathInput, *, nextufs_binary: PathInput | None = None) -> None:
    """Remove the five non-English boot translations from an English-only floppy."""
    with transaction(Image(executable(nextufs_binary), image, root="/usr/standalone/i386")) as staged:
        root = "/usr/standalone/i386"
        table = Table(staged.read(root + "/Language.table"))
        if any(name == "Languages" for name, *_ in table.entries):
            raise ValueError("disable boot language selection before removing translations")
        for language in ("French", "German", "Italian", "Spanish", "Swedish"):
            name = language + ".lproj"
            entry = staged.child(root, name)
            if entry is None:
                continue
            if not entry.is_dir:
                raise ValueError(f"boot translation is not a directory: {name}")
            path = root + "/" + name
            for item in reversed(staged.tree(path)):
                staged.mutate("rmdir" if item.is_dir else "unlink",
                              path if item.name == "." else path + "/" + item.name)


# Complete instruction sequences, including the IRQ7/IRQ15 tests and exit jump.
# Patch 4 moves this code and the spurious-interrupt counter it references.
# Keep in sync with fix-pic-bug.pl; tests compare both patchers' results.
_PIC_PROFILES = (
    ("OPENSTEP 4.2", 0x8c70a, "7d0f83fb0f7517baa0000000ec84c07c0dff05d8691f00e9260100009090"),
    ("OPENSTEP 4.2 Patch 4", 0x8c88e, "7d0f83fb0f7517baa0000000ec84c07c0dff05187a1f00e9260100009090"),
)


def _pic_replacement(original: bytes) -> bytes:
    return bytes.fromhex("7d13") + original[2:17] + bytes.fromhex("b062e620e92801000090909090")


def patch_pic_kernel(kernel: bytes) -> bytes:
    """Fix spurious IRQ15 handling in the OPENSTEP 4.2 Intel kernel.

    The kernel's interrupt handler returns from a spurious IRQ15 without an
    end-of-interrupt (EOI) command. Although the slave 8259 PIC has no real
    interrupt to acknowledge, the master PIC still has its cascade IRQ2 marked
    in service. Leaving that bit set blocks further slave-PIC interrupts,
    including IDE interrupts, and can hang disk probing or I/O.

    Replace the spurious-interrupt counter increment with ``mov al, 0x62`` /
    ``out 0x20, al``: a specific EOI for IRQ2 sent only to the master PIC.
    Adjust the exit jump and pad with NOPs to keep the kernel size unchanged.
    Retarget the spurious IRQ7 branch to skip this EOI, since a spurious
    master-PIC interrupt needs no acknowledgement.

    These edits change 12 bytes. Match the complete instruction sequence at
    the known stock or Patch 4 offset; accept an already-patched kernel and
    reject unknown, partially patched or truncated input.
    Patch source:
    https://github.com/onionmixer/OPENSTEP-BOOTCD-INTEL/blob/master/04_tools/patch_kernel_pic.py
    """
    if len(kernel) < 28 or kernel[:8] != bytes.fromhex("cefaedfe07000000"):
        raise ValueError("expected an i386 Mach-O kernel")
    for _, offset, oldhex in _PIC_PROFILES:
        old = bytes.fromhex(oldhex)
        new = _pic_replacement(old)
        if kernel[offset:offset + len(old)] in (old, new):
            return kernel[:offset] + new + kernel[offset + len(old):]
    raise ValueError("PIC patch signature mismatch: expected a stock OPENSTEP 4.2 or Patch 4 kernel")


def patch_kernel_pic_bug(source: PathInput, output: PathInput, *, nextufs_binary: PathInput | None = None) -> None:
    """Copy an image, patch /mach_kernel if needed, and replace it through nextufs.

    Preserve kernel permissions, ownership and access/modification times.
    Publish only after kernel readback and read-only fsck succeed.
    """
    with new_output(output, source=source) as staged:
        image = Image(executable(nextufs_binary), staged)
        entry = image.inspect("/mach_kernel")[0]
        kernel = image.read("/mach_kernel", entry)
        patched = patch_pic_kernel(kernel)
        if patched != kernel:
            image.write("/mach_kernel", patched, entry, exists=True)
        if image.read("/mach_kernel") != patched:
            raise ValueError("patched kernel readback failed")
        check_image(staged, nextufs_binary=nextufs_binary)


def copy_kernel(source_image: PathInput, target_image: PathInput, output: PathInput, *,
                nextufs_binary: PathInput | None = None) -> None:
    """Copy an image's i386 kernel to a new target-image copy, retaining target metadata."""
    binary = executable(nextufs_binary)
    kernel = _i386_kernel(Image(binary, source_image).read("/mach_kernel"))
    if struct.unpack_from("<I", kernel, 12)[0] != 2:
        raise ValueError("expected an executable i386 kernel")
    with new_output(output, source=target_image) as staged:
        image = Image(binary, staged)
        entry = image.inspect("/mach_kernel")[0]
        if not stat.S_ISREG(entry.mode):
            raise ValueError("target /mach_kernel must be a regular file")
        image.write("/mach_kernel", kernel, entry, exists=True)
        _verify_metadata(entry, image.inspect("/mach_kernel")[0], "/mach_kernel")
        if image.read("/mach_kernel") != kernel:
            raise ValueError("copied kernel readback failed")
        check_image(staged, nextufs_binary=binary)


def _i386_kernel(kernel: bytes) -> bytes:
    """Extract the Intel kernel from a legacy fat Mach-O, or accept a thin one."""
    if kernel[:4] == bytes.fromhex("cafebabe"):
        if len(kernel) < 8:
            raise ValueError("truncated fat Mach-O header")
        count = struct.unpack_from(">I", kernel, 4)[0]
        end = 8 + count * 20
        if end > len(kernel):
            raise ValueError("truncated fat Mach-O architecture table")
        architectures = [struct.unpack_from(">5I", kernel, pos) for pos in range(8, end, 20)]
        intel = [arch for arch in architectures if arch[0] == 7]
        if len(intel) != 1:
            raise ValueError("expected exactly one i386 kernel in fat Mach-O")
        _, _, offset, size, _ = intel[0]
        if offset < end or size < 28 or offset + size > len(kernel):
            raise ValueError("invalid i386 kernel extent in fat Mach-O")
        kernel = kernel[offset:offset + size]
    if (len(kernel) < 28 or kernel[:4] != bytes.fromhex("cefaedfe") or
            struct.unpack_from("<I", kernel, 4)[0] != 7):
        raise ValueError("expected an i386 Mach-O kernel")
    return kernel


_INSTALL_DRIVER_ARCHIVE = "/NextCD/BootDrivers.tar"
_INSTALL_KERNEL = "/NextCD/mach_kernel.picfix"
_INSTALL_PIC_SCRIPT = "/NextCD/fix-pic-bug"


def _pic_fix_script() -> bytes:
    """Read the standalone helper, compatible with OPENSTEP's bundled Perl 5."""
    # Resolve beside the library, not the caller's working directory. Text mode
    # normalizes Windows checkouts to LF for OPENSTEP's interpreter/shebang.
    return Path(__file__).with_name("fix-pic-bug.pl").read_text(encoding="ascii").encode("ascii")


_INSTALL_DRIVER_ANCHOR = b'\n${SYNC}\n\necho\n${CHECKFLOP}'
_INSTALL_DRIVER_HOOK = br'''
# BEGIN quickstep boot drivers
if [ "${ARCH}" = "i386" ]; then
@PACKAGE_HOOK@    echo "Installing boot-floppy drivers on the startup disk..."
    if ${TAR} -xpf "${CDDIR}/BootDrivers.tar" -C "${HD}/usr/Devices"; then
        echo "Boot-floppy drivers installed."
    else
        echo "Cannot install boot-floppy drivers; installation stopped."
        exit 1
    fi
@REMOVE_HOOK@    echo "Configuring installed-system drivers..."
    SYSTEM_CONFIG="${HD}/usr/Devices/System.config"
    if [ -f "${SYSTEM_CONFIG}/Default.table" ]; then
        # Instance tables take precedence over Default.table when present.
        # OPENSTEP sh does not expand globs joined to a quoted path prefix.
        (cd "${SYSTEM_CONFIG}" || exit 1
        for TABLE_FILE in Default.table Instance[0-9]*.table
        do
            TABLE_FILE="${SYSTEM_CONFIG}/${TABLE_FILE}"
            if [ -f "${TABLE_FILE}" ]; then
                # Use legacy awk syntax: no -v option or ternary expressions.
                if ${CP} -p "${TABLE_FILE}" "${TABLE_FILE}.quickstep" &&
                    ${AWK} '
                    BEGIN {
                        boot = "@BOOT_DRIVERS@"
                        count = split(boot, drivers, " ")
                        added = "@ACTIVE_DRIVERS@"
                        additions = split(added, extra, " ")
                        removed = "@REMOVED_DRIVERS@"
                        removals = split(removed, excluded, " ")
                        value = ""
                    }
                    /^[ \t]*"Boot Drivers"[ \t]*=/ { next }
                    /^[ \t]*"Active Drivers"[ \t]*=/ {
                        split($0, fields, "\"")
                        total = split(fields[4], active, " ")
                        for (i = 1; i <= total; i++) {
                            keep = 1
                            for (j = 1; j <= count; j++)
                                if (active[i] == drivers[j]) keep = 0
                            for (j = 1; j <= additions; j++)
                                if (active[i] == extra[j]) keep = 0
                            for (j = 1; j <= removals; j++)
                                if (active[i] == excluded[j]) keep = 0
                            if (keep) {
                                if (value == "") value = active[i]
                                else value = value " " active[i]
                            }
                        }
                        next
                    }
                    { print }
                    END {
                        if (added != "") {
                            if (value == "") value = added
                            else value = value " " added
                        }
                        printf "\"Active Drivers\" = \"%s\";\n", value
                        printf "\"Boot Drivers\" = \"%s\";\n", boot
                    }
                    ' "${TABLE_FILE}" > "${TABLE_FILE}.quickstep" &&
                    ${MV} "${TABLE_FILE}.quickstep" "${TABLE_FILE}"; then
                    echo "Configured boot drivers in ${TABLE_FILE}."
                else
                    echo "Cannot configure boot drivers in ${TABLE_FILE}; installation stopped."
                    exit 1
                fi
            fi
        done
        ) || exit 1
    else
        echo "Missing installed System.config/Default.table; installation stopped."
        exit 1
    fi
@PIC_HOOK@fi
# END quickstep boot drivers
'''
_INSTALL_REMOVE_DRIVERS_HOOK = br'''    echo "Removing excluded drivers from the startup disk..."
    for DRIVER in @REMOVED_DRIVERS@
    do
        if /bin/rm -rf "${HD}/usr/Devices/${DRIVER}.config"; then
            echo "Removed ${DRIVER}."
        else
            echo "Cannot remove ${DRIVER}; installation stopped."
            exit 1
        fi
    done
'''
_INSTALL_PIC_HOOK = br'''    echo "Installing PIC-patched kernel on the startup disk..."
    if ${CP} -p "${CDDIR}/mach_kernel.picfix" "${HD}/mach_kernel"; then
        echo "PIC-patched kernel installed."
    else
        echo "Cannot install PIC-patched kernel; installation stopped."
        exit 1
    fi
    echo "Installing /usr/bin/fix-pic-bug on the startup disk..."
    if ${CP} -p "${CDDIR}/fix-pic-bug" "${HD}/usr/bin/fix-pic-bug"; then
        echo "PIC patch helper installed; run it after replacing the kernel."
    else
        echo "Cannot install PIC patch helper; installation stopped."
        exit 1
    fi
'''


def _patch_cd_installer(script: bytes, boot_drivers: Iterable[str], *, fix_pic_bug: bool = False,
                        package_hook: bytes = b"", active_drivers: Iterable[str] = (),
                        removed_drivers: Iterable[str] = ()) -> bytes:
    """Install drivers and optionally a patched kernel/helper before final reboot."""
    names = list(boot_drivers)
    active = list(active_drivers)
    removed = list(removed_drivers)
    # These names are embedded in both a shell argument and an OPENSTEP table.
    if not names or any(re.fullmatch(r"[A-Za-z0-9_.+-]+", name) is None for name in names + active + removed):
        raise ValueError("expected a nonempty list of safe boot-driver names")
    if len(set(names + active)) != len(names + active):
        raise ValueError("duplicate installation driver names")
    if "System" in removed or set(removed) & set(names + active):
        raise ValueError("cannot remove System.config or an activated installation driver")
    hook = _INSTALL_DRIVER_HOOK.replace(b"@BOOT_DRIVERS@", " ".join(names).encode("ascii"))
    hook = hook.replace(b"@ACTIVE_DRIVERS@", " ".join(active).encode("ascii"))
    hook = hook.replace(b"@REMOVE_HOOK@", _INSTALL_REMOVE_DRIVERS_HOOK if removed else b"")
    hook = hook.replace(b"@REMOVED_DRIVERS@", " ".join(removed).encode("ascii"))
    hook = hook.replace(b"@PIC_HOOK@", _INSTALL_PIC_HOOK if fix_pic_bug else b"")
    hook = hook.replace(b"@PACKAGE_HOOK@", package_hook)
    if script.count(_INSTALL_DRIVER_ANCHOR) != 1:
        raise ValueError("unsupported rc.cdrom: expected one final sync/eject sequence")
    if hook + _INSTALL_DRIVER_ANCHOR in script:
        return script
    if b"# BEGIN quickstep boot drivers" in script:
        raise ValueError("rc.cdrom contains a different boot-driver hook")
    return script.replace(_INSTALL_DRIVER_ANCHOR, hook + _INSTALL_DRIVER_ANCHOR, 1)


def _boot_driver_archive(boot: PathInput, *, nextufs_binary: PathInput | None = None,
                         packaged_drivers: Iterable[str] = ()) -> bytes:
    """Package configured bundles, not the floppy's CD-only System.config."""
    drivers = DriverImage(boot, nextufs=nextufs_binary)
    image = Image(executable(nextufs_binary), boot)
    names = drivers.list_drivers()
    packaged = set(packaged_drivers)
    if not packaged <= set(names):
        raise ValueError("packaged driver is missing from the boot floppy")
    if not names:
        raise ValueError("boot floppy has no driver bundles")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in names:
            bundle = image.bundle(name)
            entries = image.tree(bundle)
            if not any(entry.name == "Default.table" for entry in entries):
                raise ValueError(f"{name}.config has no Default.table")
            instance = image.inspect(bundle + "/Instance0.table")[0]
            configuration = image.read(bundle + "/Instance0.table", instance)
            # A package installs the original bundle from its BOM. Add only the
            # configured instance; do not overwrite receipted files with the
            # boot floppy's copy or promote its instance to Default.table.
            if name in packaged:
                entries = [replace(instance, name="Instance0.table")]
            for entry in entries:
                # The installed loader must use the same variant as the boot floppy.
                # In particular, EIDE's generic Default.table is not its PIIX table.
                if entry.name == "Default.table":
                    entry = replace(instance, name="Default.table")
                    data = configuration
                else:
                    data = b"" if entry.is_dir else image.read(bundle + "/" + entry.name, entry)
                relative = name + ".config" + ("" if entry.name == "." else "/" + entry.name)
                member = tarfile.TarInfo(relative)
                member.mode, member.uid, member.gid = stat.S_IMODE(entry.mode), entry.uid, entry.gid
                member.mtime = entry.mtime
                member.type = tarfile.DIRTYPE if entry.is_dir else tarfile.REGTYPE
                member.size = len(data)
                archive.addfile(member, None if entry.is_dir else io.BytesIO(data))
    return output.getvalue()


def prepare_installation_drivers(boot: PathInput, user_ufs: PathInput, output: PathInput, *,
                                 nextufs_binary: PathInput | None = None,
                                 fix_pic_bug: bool = False, package_hook: bytes = b"",
                                 packaged_drivers: Iterable[str] = (),
                                 removed_drivers: Iterable[str] = ()) -> None:
    """Prepare a User UFS installer with floppy drivers and an optional PIC fix.

    Stage a legacy-compatible tar archive on the CD and patch rc.cdrom to unpack
    it after base-system copying/configuration, before reboot. Keep the installer's
    generated System.config and language settings, but replace its Boot Drivers
    with the floppy's ordered list and remove those names from Active Drivers.
    Merge packaged active drivers in their floppy order, retaining other active
    drivers and removing duplicates of the added names.
    Update Default.table and any instance tables; do not copy CD-boot settings.
    With fix_pic_bug, derive the installed kernel from the User CD, not the boot
    floppy. Stage a thin, patched copy for rc.cdrom to install after ditto's
    base-system copy; leave the CD's original /mach_kernel untouched.
    Also install /usr/bin/fix-pic-bug so Patch 4's kernel can be patched later.
    Otherwise, leave kernel installation entirely to the original installer.
    Run package_hook before copying driver configurations. For packaged_drivers,
    stage only Instance0.table; their complete bundles come from package_hook.
    Remove excluded bundles and activation entries from the startup disk after
    base-system and package copying. Keep the CD's original base-system payload.
    """
    packaged_drivers = tuple(packaged_drivers)
    with new_output(output, source=user_ufs) as staged:
        image = Image(executable(nextufs_binary), staged)
        path = "/etc/rc.cdrom"
        entry = image.inspect(path)[0]
        _, system = system_table(Image(executable(nextufs_binary), boot))
        boot_names, active_names = driver_lists(system)
        script = _patch_cd_installer(image.read(path, entry), boot_names,
                                     fix_pic_bug=fix_pic_bug, package_hook=package_hook,
                                     active_drivers=(name for name in active_names if name in packaged_drivers),
                                     removed_drivers=removed_drivers)
        archive = _boot_driver_archive(boot, nextufs_binary=nextufs_binary, packaged_drivers=packaged_drivers)
        for payload in (_INSTALL_DRIVER_ARCHIVE, _INSTALL_KERNEL, _INSTALL_PIC_SCRIPT):
            if image.child("/NextCD", payload.rsplit("/", 1)[1]) is not None:
                raise ValueError(f"User filesystem already contains {payload}")
        metadata = replace(entry, mode=stat.S_IFREG | 0o644, size=len(archive))
        image.write(_INSTALL_DRIVER_ARCHIVE, archive, metadata, exists=False)
        if fix_pic_bug:
            kernel_entry = image.inspect("/mach_kernel")[0]
            kernel = patch_pic_kernel(_i386_kernel(image.read("/mach_kernel", kernel_entry)))
            image.write(_INSTALL_KERNEL, kernel, replace(kernel_entry, size=len(kernel)), exists=False)
            if image.read(_INSTALL_KERNEL) != kernel:
                raise ValueError("installation kernel payload readback failed")
            helper = _pic_fix_script()
            image.write(_INSTALL_PIC_SCRIPT, helper,
                        replace(entry, mode=stat.S_IFREG | 0o755, uid=0, gid=0, size=len(helper)), exists=False)
            if image.read(_INSTALL_PIC_SCRIPT) != helper:
                raise ValueError("PIC patch helper payload readback failed")
        image.write(path, script, entry, exists=True)
        if image.read(path) != script or image.read(_INSTALL_DRIVER_ARCHIVE) != archive:
            raise ValueError("installation-driver payload readback failed")


def image_info(image: PathInput, *, nextufs_binary: PathInput | None = None) -> ImageInfo:
    """Inspect a raw or labeled UFS image without modifying it."""
    return ImageInfo.from_json(nextufs("info", "--json", image, binary=nextufs_binary))


def cd_layout(user_cd: PathInput, *, nextufs_binary: PathInput | None = None) -> ImageInfo:
    """Inspect an OPENSTEP CD whose UFS starts immediately after its front porch."""
    info = image_info(user_cd, nextufs_binary=nextufs_binary)
    label = info.label
    if (not info.used_disk_label or label.version != 0x646c5633 or
            label.sector_size != 2048 or
            info.slice_base != label.front_porch_sectors * 2048):
        raise ValueError("expected a dlV3 User CD with its UFS slice immediately after the front porch")
    return info


def extract_ufs(image: PathInput, output: PathInput, *,
                nextufs_binary: PathInput | None = None) -> ImageInfo:
    """Copy a raw or labeled image's UFS slice to a new file; return its source layout."""
    info = image_info(image, nextufs_binary=nextufs_binary)
    with new_output(output) as staged, Path(image).open("rb") as source, staged.open("wb") as target:
        source.seek(info.slice_base)
        remaining = info.slice_bytes
        while remaining:
            data = source.read(min(1024 * 1024, remaining))
            if not data:
                raise ValueError("truncated UFS slice")
            target.write(data)
            remaining -= len(data)
    return info


def _directory(image: Image, path: str) -> Entry:
    """Require an absolute directory path without traversing symlinks."""
    if not path.startswith("/"):
        raise ValueError(f"expected an absolute image directory: {path}")
    current = ""
    entry = image.inspect("/")[0]
    for part in path.strip("/").split("/") if path != "/" else ():
        current += "/" + component(part)
        entry = image.inspect(current)[0]
        if not entry.is_dir:
            raise ValueError(f"not a directory: {current}")
    return entry


_BUILDDISK_LANGUAGE_ROW = bytes.fromhex(
    "6a008b95dcfdffff52428995dcfdffff8b0db0c91600518b15acc91600528b4d088b492051"
    "e82eb0ff0483c40850e825b0ff0489c683c424"
    "8b550883bab0000000000f84b50000008b82b0000000807819000f84a5000000")
_BUILDDISK_LANGUAGE_ROW_FIXED = bytes.fromhex(
    "83c4148b55088b82b000000085c00f84e9000000807819000f84df0000009090"
    "6a008b95dcfdffff52428995dcfdffff8b0db0c91600518b15acc91600528b4d088b492051"
    "e80eb0ff0483c40850e805b0ff0489c683c410")


@dataclass(frozen=True)
class _BuildDiskPatch:
    offset: int  # File offset within the Intel Mach-O slice (VA minus 0x2000).
    original: bytes
    fixed: bytes


_BUILDDISK_LANGUAGE_PATCHES = (
    _BuildDiskPatch(0x641f, _BUILDDISK_LANGUAGE_ROW, _BUILDDISK_LANGUAGE_ROW_FIXED),
)
_BUILDDISK_CAPACITY_PATCHES = (
    # The sole caller now receives KiB, so it must not divide by 1024 again.
    _BuildDiskPatch(0x5b5f, bytes.fromhex("c1e80a"), bytes.fromhex("909090")),
    # Retarget the non-512-byte-sector and valid-MBR branches into the new tail.
    _BuildDiskPatch(0xf257, bytes.fromhex("755e"), bytes.fromhex("7544")),
    _BuildDiskPatch(0xf280, bytes.fromhex("741e"), bytes.fromhex("7405")),
    _BuildDiskPatch(0xf282, bytes.fromhex(
        "57e8b10fff048b45d8c1e009eb3457e8a30fff048b430cc1e009eb269090"
        "8d98be01000031c0807b04a774e24083c31083f8037ef1"
        "57e87c0fff048b45d80faf45dc"), bytes.fromhex(
        "8b45d8eb258d98be010000b904000000807b04a7741183c310e2f5"
        "8b45d8f765dc0facd00aeb058b430cd1e85057e8840fff0483c40458eb09"
        "909090909090909090")),
)
_BUILDDISK_PROFILES = (
    # Stock OPENSTEP 4.2 three-architecture executable and its Intel slice.
    (0x36000, "78bc4589785421a0b0e0b6a5281458c8e10125a0a3bb67fe8478e4c0b51f2fa5"),
    (0, "f6274672318d3d0acae7dd5489242516d7529ea37261428a0e5f8641892cc8ff"),
)


def _patch_builddisk(binary: bytes, patches: tuple[_BuildDiskPatch, ...]) -> bytes:
    """Recognize stock or fully applied fixes, then apply one independent fix."""
    for base, checksum in _BUILDDISK_PROFILES:
        original = bytearray(binary)
        for group in (_BUILDDISK_LANGUAGE_PATCHES, _BUILDDISK_CAPACITY_PATCHES):
            actual = tuple(binary[base + p.offset:base + p.offset + len(p.original)] for p in group)
            if actual not in (tuple(p.original for p in group), tuple(p.fixed for p in group)):
                break
            for p in group:
                original[base + p.offset:base + p.offset + len(p.original)] = p.original
        else:
            if hashlib.sha256(original).hexdigest() != checksum:
                continue
            result = bytearray(binary)
            for p in patches:
                result[base + p.offset:base + p.offset + len(p.original)] = p.fixed
            return bytes(result)
    raise ValueError("unsupported BuildDisk executable (expected stock OPENSTEP 4.2 or known complete fixes)")


def patch_builddisk_language_heading(binary: bytes) -> bytes:
    """Create BuildDisk's Languages heading only when language packages exist.

    At i386 VA 0x841f, move the package/language guard before addRow. Adjust
    both conditional branches and objc_msgSend calls, retaining the 20-byte
    stack cleanup owed by the preceding Essentials row on either exit.
    The unused row otherwise keeps its prototype tag as well as its title;
    merely blanking the NIB title leaves an unsafe selectable cell.
    Accept known stock 4.2 executables and either of our complete fixes.
    """
    return _patch_builddisk(binary, _BUILDDISK_LANGUAGE_PATCHES)


def patch_builddisk_capacity(binary: bytes) -> bytes:
    """Fix the 4 GiB disk-size overflow in the Intel OPENSTEP 4.2 BuildDisk.

    The helper at VA 0x111b0 multiplies sectors by sector size in 32-bit bytes;
    its sole caller at 0x7b51 then divides by 1024. Exactly 4 GiB wraps to zero,
    disabling package selection and offering to initialize the disk as swap.

    Return KiB instead: divide 512-byte sector counts by two, or use unsigned
    MUL's 64-bit product followed by SHRD for other sector sizes. Preserve the
    MBR's first NeXT-partition selection, whole-disk fallback, error returns,
    and callee-saved registers. Share close(), saving the result across it.
    Remove the caller's now-redundant shift. All edits are same-length; no
    Mach-O layout or other architecture changes. This fixes reporting, not
    the kernel/filesystem's maximum supported partition size.
    """
    return _patch_builddisk(binary, _BUILDDISK_CAPACITY_PATCHES)


def fix_builddisk_capacity(user_ufs: PathInput, output: PathInput, *,
                           nextufs_binary: PathInput | None = None) -> None:
    """Copy a User UFS with the guarded BuildDisk capacity fix, retaining metadata."""
    binary = executable(nextufs_binary)
    _raw_ufs_info(user_ufs, binary)
    source = Image(binary, user_ufs)
    application = "/NextAdmin/BuildDisk.app"
    app_entry = _directory(source, application)
    path = application + "/BuildDisk"
    entry = source.inspect(path)[0]
    original = source.read(path, entry)
    patched = patch_builddisk_capacity(original)
    with new_output(output, source=user_ufs) as staged:
        target = Image(binary, staged)
        if patched != original:
            target.write(path, patched, entry, exists=True)
            target.metadata(application, app_entry)
        if target.read(path) != patched:
            raise ValueError("BuildDisk capacity fix readback failed")
        _verify_metadata(entry, target.inspect(path)[0], path)
        _verify_metadata(app_entry, target.inspect(application)[0], application)
        check_image(staged, nextufs_binary=binary)


def remove_language_packages(user_ufs: PathInput, output: PathInput, *,
                             nextufs_binary: PathInput | None = None) -> None:
    """Copy a User UFS without the five optional non-English Essentials packages.

    Remove both payload bundles and their receipt entries, which the installer
    uses as its package inventory. English is part of BaseSystem, not a separate
    package. Leave localized files in the base filesystem and all other packages
    alone. Missing packages are allowed, so an already-pruned image is valid.
    Omit the unused Languages heading in the stock Intel BuildDisk, if present.
    """
    packages = {language + "Essentials.pkg" for language in
                ("French", "German", "Italian", "Spanish", "Swedish")}
    binary = executable(nextufs_binary)
    _raw_ufs_info(user_ufs, binary)
    source = Image(binary, user_ufs)
    parents: list[tuple[str, Entry]] = []
    trees: list[tuple[str, list[Entry]]] = []
    # Validate every target before editing the staged copy. Refuse symlinks,
    # including in parent paths, rather than following them outside a bundle.
    for parent in ("/NextCD/Packages", "/NextLibrary/Receipts"):
        parents.append((parent, _directory(source, parent)))
        for entry in source.inspect(parent)[1:]:
            if entry.name in packages:
                path = parent + "/" + entry.name
                _directory(source, path)
                trees.append((path, source.tree(path)))
    application = "/NextAdmin/BuildDisk.app"
    builddisk = None
    if source.child("/", "NextAdmin") is not None:
        _directory(source, "/NextAdmin")
        if source.child("/NextAdmin", "BuildDisk.app") is not None:
            app_metadata = _directory(source, application)
            path = application + "/BuildDisk"
            entry = source.inspect(path)[0]
            original = source.read(path, entry)
            patched = patch_builddisk_language_heading(original)
            builddisk = (path, entry, patched, original != patched)
    with new_output(output, source=user_ufs) as staged:
        target = Image(binary, staged)
        if builddisk is not None:
            path, entry, patched, changed = builddisk
            if changed:
                print("Removing BuildDisk's unused language heading...", flush=True)
                target.write(path, patched, entry, exists=True)
                target.metadata(application, app_metadata)
            if target.read(path) != patched:
                raise ValueError("BuildDisk language-heading fix readback failed")
        for root, entries in trees:
            print(f"Removing {root}...", flush=True)
            for entry in reversed(entries):
                path = root if entry.name == "." else root + "/" + entry.name
                target.mutate("rmdir" if entry.is_dir else "unlink", path)
        print("Verifying language-package removal...", flush=True)
        for parent, entry in parents:
            target.metadata(parent, entry)
            expected = {child.name for child in source.inspect(parent)[1:]} - packages
            if {child.name for child in target.inspect(parent)[1:]} != expected:
                raise ValueError(f"package inventory differs after removal: {parent}")


def _raw_ufs_info(path: PathInput, binary: PathInput | None) -> ImageInfo:
    info = image_info(path, nextufs_binary=binary)
    if (info.used_disk_label or info.slice_base != 0 or
            info.filesystem_bytes != Path(path).stat().st_size):
        raise ValueError("expected an unpadded raw UFS filesystem")
    return info


def _directory_capacity(image: Path, entries: list[Entry], binary: PathInput | None) -> None:
    info = _raw_ufs_info(image, binary)
    block = info.filesystem.block_size
    required = sum(max(1, (entry.size + block - 1) // block) * block for entry in entries)
    reserve = max(16 * 1024 * 1024, (required + 9) // 10)
    while True:
        fs = info.filesystem
        free = fs.free_blocks * block + fs.free_fragments * fs.fragment_size
        if free >= required + reserve and fs.free_inodes >= len(entries):
            return
        group = fs.fragments_per_group * fs.fragment_size
        deficit = max(required + reserve - free, group)
        size = ((info.filesystem_bytes + deficit + group - 1) // group) * group
        if size > info.compatibility_ceiling_bytes:
            raise ValueError("directory copy would exceed the OPENSTEP filesystem size limit")
        print(f"Growing destination UFS to {size // 1024} KiB...", flush=True)
        grow_image(image, size // 1024, nextufs_binary=binary)
        info = _raw_ufs_info(image, binary)


def copy_directory(source_image: PathInput, source_path: str, target_ufs: PathInput,
                   target_path: str, output: PathInput, *,
                   nextufs_binary: PathInput | None = None) -> None:
    """Copy directory contents into a new raw UFS, growing it when necessary.

    Existing destination directories retain their metadata; existing children
    are never overwritten or merged. New directories and regular files retain
    source metadata and bytes, with no format-specific transformations.
    Symlinks and special files are unsupported. Inputs remain untouched.
    """
    print(f"Inspecting directory {source_path}...", flush=True)
    if not target_path.startswith("/"):
        raise ValueError(f"expected an absolute image directory: {target_path}")
    binary = executable(nextufs_binary)
    source = Image(binary, source_image)
    source_root = _directory(source, source_path)
    source_path, target_path = source_path.rstrip("/"), target_path.rstrip("/")
    entries = source.tree(source_path or "/")
    target = Image(binary, target_ufs)
    parent, _, name = target_path.rpartition("/")
    _directory(target, parent or "/")
    existing = target.child(parent or "/", component(name)) if target_path else target.inspect("/")[0]
    if existing is not None:
        _directory(target, target_path or "/")
        children = {entry.name for entry in target.inspect(target_path or "/")[1:]}
        for entry in entries[1:]:
            if entry.name.split("/", 1)[0] in children:
                raise ValueError(f"destination already contains {target_path}/{entry.name}")
    with new_output(output, source=target_ufs) as staged:
        _directory_capacity(staged, entries, binary)
        target = Image(binary, staged)
        if existing is None:
            target.mutate("mkdir", target_path)
        for entry in entries[1:]:
            if "/" not in entry.name:
                print(f"Copying {source_path}/{entry.name} to {target_path}/{entry.name}...", flush=True)
            path = target_path + "/" + entry.name
            if entry.is_dir:
                target.mutate("mkdir", path)
            else:
                target.write(path, source.read(source_path + "/" + entry.name, entry), entry, exists=False)
        for entry in reversed(entries[1:]):
            if entry.is_dir:
                target.metadata(target_path + "/" + entry.name, entry)
        target.metadata(target_path or "/", existing if existing is not None else source_root)
        verify_directory_copy(source_image, source_path or "/", staged, target_path or "/",
                              nextufs_binary=binary)


def _verify_metadata(expected: Entry, actual: Entry, path: str) -> None:
    """Compare portable metadata, excluding filesystem-local inode/allocation data."""
    if ((expected.mode, expected.uid, expected.gid, expected.atime, expected.mtime) !=
            (actual.mode, actual.uid, actual.gid, actual.atime, actual.mtime)):
        raise ValueError(f"copied metadata differs: {path}")


def verify_directory_copy(source_image: PathInput, source_path: str, target_image: PathInput,
                          target_path: str, *, nextufs_binary: PathInput | None = None) -> None:
    """Check copied contents and metadata; allow unrelated destination children.

    The destination root's metadata is excluded because a pre-existing directory
    retains its own metadata. Inode numbers and directory allocation sizes need
    not match across filesystems.
    """
    print(f"Verifying copied directory {target_path}...", flush=True)
    binary = executable(nextufs_binary)
    source, target = Image(binary, source_image), Image(binary, target_image)
    _directory(source, source_path)
    _directory(target, target_path)
    for entry in source.tree(source_path)[1:]:
        path = target_path.rstrip("/") + "/" + entry.name
        actual = target.inspect(path)[0]
        _verify_metadata(entry, actual, path)
        if not entry.is_dir and (actual.size != entry.size or
                target.read(path, actual) != source.read(source_path.rstrip("/") + "/" + entry.name, entry)):
            raise ValueError(f"copied file contents differ: {path}")


@dataclass(frozen=True)
class _TarEntry:
    entry: Entry
    member: tarfile.TarInfo | None


def _tar_entries(archive: tarfile.TarFile) -> list[_TarEntry]:
    """Validate the whole archive before writing, including implicit parents."""
    entries: dict[str, _TarEntry] = {}
    seen: set[str] = set()
    for member in archive.getmembers():
        if not (member.isdir() or member.isfile()) or member.issparse():
            raise ValueError(f"unsupported tar entry type: {member.name}")
        name = member.name
        if name.startswith("/") or PureWindowsPath(name).drive:
            raise ValueError(f"absolute tar path: {name}")
        while name.startswith("./"):
            name = name[2:]
        name = name.rstrip("/") if member.isdir() else name
        if name in seen:
            raise ValueError(f"duplicate tar entry: {name}")
        seen.add(name)
        if name in ("", ".") and member.isdir():
            continue  # The existing destination root keeps its metadata.
        parts = name.split("/")
        for part in parts:
            component(part)
        timestamp = int(member.mtime)
        if (member.size < 0 or not 0 <= timestamp <= 0xffffffff or
                not 0 <= member.uid <= 0xffff or not 0 <= member.gid <= 0xffff):
            raise ValueError(f"tar metadata cannot be represented in OPENSTEP UFS: {name}")
        mode = (stat.S_IFDIR if member.isdir() else stat.S_IFREG) | stat.S_IMODE(member.mode)
        entry = Entry(0, mode, member.uid, member.gid, member.size, timestamp, timestamp, name)
        previous = entries.get(name)
        if previous is not None and previous.entry.is_dir and not entry.is_dir:
            raise ValueError(f"tar file is also used as a parent directory: {name}")
        entries[name] = _TarEntry(entry, member)
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            previous = entries.get(parent)
            if previous is not None and not previous.entry.is_dir:
                raise ValueError(f"tar parent is not a directory: {parent}")
            if previous is None:
                entries[parent] = _TarEntry(replace(entry, name=parent, mode=stat.S_IFDIR | 0o755, size=0), None)
    return sorted(entries.values(), key=lambda item: (item.entry.name.count("/"), item.entry.name))


def _tar_read(archive: tarfile.TarFile, item: _TarEntry) -> bytes:
    if item.member is None or not item.member.isfile():
        raise ValueError(f"not an archived regular file: {item.entry.name}")
    stream = archive.extractfile(item.member)
    if stream is None:
        raise ValueError(f"cannot read tar entry: {item.entry.name}")
    with stream:
        data = stream.read()
    if len(data) != item.entry.size:
        raise ValueError(f"truncated tar entry: {item.entry.name}")
    return data


def tar_entries(archive: PathInput) -> list[Entry]:
    """Inspect validated archive entries, including implicit parent directories."""
    with tarfile.open(archive, "r:*") as source:
        return [item.entry for item in _tar_entries(source)]


def copy_tar(archive: PathInput, target_ufs: PathInput, target_path: str, output: PathInput, *,
             nextufs_binary: PathInput | None = None) -> None:
    """Import a tar into an existing directory in a new, automatically grown UFS.

    Preserve bytes, modes, ownership and mtime; use mtime for atime. Missing
    parent directories use mode 0755 and their first child's ownership/mtime.
    Reject collisions, unsafe names, links and special files. No host extraction,
    script execution or interpretation of nested archives is performed.
    """
    print(f"Inspecting archive {archive}...", flush=True)
    binary = executable(nextufs_binary)
    with tarfile.open(archive, "r:*") as source:
        entries = _tar_entries(source)
        target = Image(binary, target_ufs)
        root = _directory(target, target_path)
        target_path = target_path.rstrip("/")
        children = {entry.name for entry in target.inspect(target_path or "/")[1:]}
        for item in entries:
            if item.entry.name.split("/", 1)[0] in children:
                raise ValueError(f"destination already contains {target_path}/{item.entry.name}")
        with new_output(output, source=target_ufs) as staged:
            _directory_capacity(staged, [item.entry for item in entries], binary)
            target = Image(binary, staged)
            for item in entries:
                path = target_path + "/" + item.entry.name
                if "/" not in item.entry.name:
                    print(f"Copying {item.entry.name} from {archive} to {path}...", flush=True)
                if item.entry.is_dir:
                    target.mutate("mkdir", path)
                else:
                    target.write(path, _tar_read(source, item), item.entry, exists=False)
            for item in reversed(entries):
                if item.entry.is_dir:
                    target.metadata(target_path + "/" + item.entry.name, item.entry)
            target.metadata(target_path or "/", root)
            verify_tar_copy(archive, staged, target_path or "/", nextufs_binary=binary)


def verify_tar_copy(archive: PathInput, target_image: PathInput, target_path: str, *,
                    nextufs_binary: PathInput | None = None) -> None:
    """Compare imported archive contents and metadata through a raw UFS or ISO."""
    print(f"Verifying {archive}...", flush=True)
    target = Image(executable(nextufs_binary), target_image)
    _directory(target, target_path)
    with tarfile.open(archive, "r:*") as source:
        for item in _tar_entries(source):
            entry = item.entry
            path = target_path.rstrip("/") + "/" + entry.name
            actual = target.inspect(path)[0]
            _verify_metadata(entry, actual, path)
            if not entry.is_dir and (actual.size != entry.size or target.read(path, actual) != _tar_read(source, item)):
                raise ValueError(f"imported tar file contents differ: {path}")


@dataclass(frozen=True)
class SetupPackage:
    name: str
    version: str
    dependencies: tuple[str, ...] = ()
    relocatable: bool = False
    restart_required: bool = False
    fix_pic_after: bool = False
    post_install: tuple[str, ...] = ()


@dataclass(frozen=True)
class SetupChoice:
    category: str
    title: str
    packages: tuple[str, ...]
    default_selected: bool = False


@dataclass(frozen=True)
class SetupCatalog:
    packages: tuple[SetupPackage, ...]
    choices: tuple[SetupChoice, ...]


def setup_plist(catalog: SetupCatalog, *, fix_pic_bug: bool = False) -> bytes:
    """Validate and serialize a catalog using OPENSTEP's ASCII plist syntax."""
    def quote(value: str) -> str:
        if not value or any(not 32 <= ord(c) < 127 for c in value):
            raise ValueError("Setup catalog strings must be nonempty printable ASCII")
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

    def names(values: tuple[str, ...]) -> str:
        return "(" + ", ".join(quote(value) for value in values) + ")"

    def flag(value: bool) -> str:
        if type(value) is not bool:
            raise ValueError("Setup catalog flags must be booleans")
        return "YES" if value else "NO"

    packages = {package.name: package for package in catalog.packages}
    if len(packages) != len(catalog.packages):
        raise ValueError("duplicate Setup package name")
    for package in catalog.packages:
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", package.name) or package.name in (".", ".."):
            raise ValueError(f"invalid Setup package name: {package.name!r}")
        if any(name not in packages for name in package.dependencies):
            raise ValueError(f"unknown dependency of Setup package {package.name}")
        if package.post_install and not package.post_install[0].startswith("/"):
            raise ValueError(f"Setup post-install executable must be absolute: {package.name}")
    resolved: set[str] = set()
    while len(resolved) < len(packages):
        ready = {p.name for p in catalog.packages if set(p.dependencies) <= resolved} - resolved
        if not ready:
            raise ValueError("cycle in Setup package dependencies")
        resolved.update(ready)
    for choice in catalog.choices:
        if not choice.packages or any(name not in packages for name in choice.packages):
            raise ValueError(f"empty or unknown packages in Setup choice: {choice.title}")
    lines = ["{", "    FormatVersion = 1;", f"    FixPICBug = {flag(fix_pic_bug)};", "    Packages = ("]
    for package in catalog.packages:
        lines.append("        { " + f"Name = {quote(package.name)}; Version = {quote(package.version)}; "
                     f"Dependencies = {names(package.dependencies)}; Relocatable = {flag(package.relocatable)}; "
                     f"RestartRequired = {flag(package.restart_required)}; FixPICAfter = {flag(package.fix_pic_after)}; "
                     f"PostInstall = {names(package.post_install)}; " + "},")
    lines.append("    );\n    Choices = (")
    for choice in catalog.choices:
        lines.append("        { " + f"Category = {quote(choice.category)}; Title = {quote(choice.title)}; "
                     f"Packages = {names(choice.packages)}; DefaultSelected = {flag(choice.default_selected)}; " + "},")
    lines.append("    );\n}\n")
    return "\n".join(lines).encode("ascii")


@dataclass(frozen=True)
class _SetupFile:
    name: str
    mode: int
    data: bytes = b""


def _setup_files(bundle: PathInput, configuration: bytes | None = None) -> list[_SetupFile]:
    root = Path(bundle)
    if root.name != "Setup.app" or not stat.S_ISDIR(local_stat(root).st_mode):
        raise ValueError(f"expected a Setup.app directory: {root}")
    result: list[_SetupFile] = []

    def visit(path: Path, name: str) -> None:
        mode = local_stat(path).st_mode
        if name in ("Setup.app/Setup.plist", "Setup.app/Setup.options"):
            if not stat.S_ISREG(mode):
                raise ValueError(f"{name} is reserved for the generated configuration file")
            return
        if stat.S_ISDIR(mode):
            result.append(_SetupFile(name, stat.S_IFDIR | 0o755))
            for child in sorted(path.iterdir()):
                component(child.name)
                visit(child, name + "/" + child.name)
        elif stat.S_ISREG(mode):
            permissions = 0o755 if name == "Setup.app/Setup" else 0o644
            result.append(_SetupFile(name, stat.S_IFREG | permissions, path.read_bytes()))
        else:
            raise ValueError(f"unsupported Setup.app file: {path}")

    visit(root, "Setup.app")
    executable_file = next((item for item in result if item.name == "Setup.app/Setup"), None)
    if executable_file is None or not stat.S_ISREG(executable_file.mode):
        raise ValueError("Setup.app must contain a regular Setup executable")
    try:
        intel = _i386_kernel(executable_file.data)
        if struct.unpack_from("<I", intel, 12)[0] != 2:  # MH_EXECUTE, not a library/object.
            raise ValueError("Mach-O file is not an executable")
    except (ValueError, struct.error) as exc:
        raise ValueError(f"Setup.app/Setup must be an Intel Mach-O executable: {exc}") from exc
    if configuration is not None:
        result.append(_SetupFile("Setup.app/Setup.plist", stat.S_IFREG | 0o644, configuration))
    return result


def validate_setup_app(bundle: PathInput) -> None:
    """Validate the supplied release bundle without executing it or changing it."""
    _setup_files(bundle)


def prepare_setup_app(bundle: PathInput, user_ufs: PathInput, output: PathInput, *,
                      catalog: SetupCatalog, fix_pic_bug: bool = False,
                      nextufs_binary: PathInput | None = None) -> None:
    """Add a local release bundle and per-CD options to a new UFS copy."""
    files = _setup_files(bundle, setup_plist(catalog, fix_pic_bug=fix_pic_bug))
    with tempfile.TemporaryDirectory(prefix="media-setup-") as temp:
        archive = Path(temp) / "Setup.tar"
        with tarfile.open(archive, "w") as target:
            for item in files:
                member = tarfile.TarInfo(item.name)
                member.mode = stat.S_IMODE(item.mode)
                member.type = tarfile.DIRTYPE if stat.S_ISDIR(item.mode) else tarfile.REGTYPE
                member.size = len(item.data)
                target.addfile(member, io.BytesIO(item.data) if member.isfile() else None)
        copy_tar(archive, user_ufs, "/", output, nextufs_binary=nextufs_binary)


def verify_setup_app(bundle: PathInput, image: PathInput, *, catalog: SetupCatalog, fix_pic_bug: bool = False,
                     nextufs_binary: PathInput | None = None) -> None:
    """Read back the complete bundle, permissions and PIC policy through UFS/ISO."""
    print("Verifying Setup.app and its CD configuration...", flush=True)
    expected = _setup_files(bundle, setup_plist(catalog, fix_pic_bug=fix_pic_bug))
    target = Image(executable(nextufs_binary), image)
    actual = {"Setup.app" if entry.name == "." else "Setup.app/" + entry.name: entry
              for entry in target.tree("/Setup.app")}
    if actual.keys() != {item.name for item in expected}:
        raise ValueError("Setup.app inventory differs")
    for item in expected:
        entry = actual[item.name]
        if (entry.mode, entry.uid, entry.gid) != (item.mode, 0, 0):
            raise ValueError(f"Setup.app metadata differs: {item.name}")
        if not entry.is_dir and target.read("/" + item.name, entry) != item.data:
            raise ValueError(f"Setup.app contents differ: {item.name}")


_USB_PARTITION_OFFSET = 1024  # LBA 2: CHS 0/0/3, independent of BIOS geometry.
_USB_FRONT_PORCH = 160 * 1024
_USB_LOADER_OFFSETS = (32 * 1024, 96 * 1024)
_USB_LABEL_OFFSETS = (7680, 15360, 23040)
_USB_LOADER_LIMIT = 44 * 1024  # boot1's fixed second-stage read size.
_USB_SYSTEM_TABLE = ROOT + "/System.config/Instance0.table"


def _patch_usb_installer(script: bytes) -> bytes:
    fdisk = b"${FDISK} $livedisk"
    selection = b"diskie=`${PICKDISK} ${disknum}`\n"
    if script.count(selection) != 1 or script.count(fdisk) != 9 or b"# BEGIN quickstep USB" in script:
        raise ValueError("unsupported USB installer script")
    # fdisk otherwise indexes BIOS geometry by the OS device number. Booting
    # USB as BIOS disk zero makes that geometry belong to the source stick.
    script = script.replace(fdisk, fdisk + b" -useAllSectors")
    return script.replace(selection, selection + b'''
# BEGIN quickstep USB source protection
source_disk=`${FINDROOT} | ${SED} 's/b$/a/'`
if [ "/dev/${diskie}" = "${source_disk}" ]; then
    echo "The selected disk is the USB installation source. Restart and select a destination disk."
    exit 1
fi
# END quickstep USB source protection
''')


def prepare_usb_installer(ufs: PathInput, output: PathInput, *,
                          nextufs_binary: PathInput | None = None) -> None:
    """Prepare USB destination selection without changing the installed system."""
    with new_output(output, source=ufs) as staged:
        image = Image(executable(nextufs_binary), staged)
        path = "/private/etc/rc.cdrom"
        entry = image.inspect(path)[0]
        parent = image.inspect("/private/etc")[0]
        expected = _patch_usb_installer(image.read(path, entry))
        image.write(path, expected, entry, True)
        image.metadata("/private/etc", parent)
        if image.read(path) != expected:
            raise ValueError("USB installer script readback differs")
        check_image(staged, nextufs_binary=nextufs_binary)


@dataclass(frozen=True)
class UsbLayout:
    """Byte offsets in a whole USB disk, not relative to its MBR partition."""

    image_bytes: int
    boot_offset: int
    boot_bytes: int
    installer_offset: int
    installer_bytes: int


def _usb_layout_for_sizes(boot_bytes: int, installer_bytes: int) -> UsbLayout:
    if any(size <= 0 or size % 1024 for size in (boot_bytes, installer_bytes)):
        raise ValueError("USB filesystems must have positive, KiB-aligned sizes")
    if boot_bytes > MAX_BOOT_FLOPPY_KIB * 1024:
        raise ValueError("USB boot filesystem exceeds the prepared floppy size limit")
    align = 1024 * 1024
    boot_offset = _USB_PARTITION_OFFSET + _USB_FRONT_PORCH
    installer_offset = (boot_offset + boot_bytes + align - 1) // align * align
    image_bytes = (installer_offset + installer_bytes + align - 1) // align * align
    if image_bytes // 512 > 0xffffffff:
        raise ValueError("USB image exceeds MBR addressing limits")
    return UsbLayout(image_bytes, boot_offset, boot_bytes, installer_offset, installer_bytes)


def _usb_checksum(label: bytes) -> int:
    data = bytearray(label[:0x230])
    data[4:8] = b"\0" * 4
    data[0x22e:0x230] = b"\0\0"
    checksum = sum(struct.unpack(">280H", data))
    while checksum >> 16:
        checksum = (checksum & 0xffff) + (checksum >> 16)
    return checksum


def _usb_label(layout: UsbLayout, boot_fs: FilesystemInfo, installer_fs: FilesystemInfo) -> bytes:
    """Encode the native m68k disk_label/disktab wire format used by Intel OPENSTEP.

    Partition records are 46 bytes, with signed 32-bit base/size fields.
    Front-porch and boot-loader addresses are absolute logical sectors even
    when the labels themselves live inside a nonzero MBR partition.
    """
    label = bytearray(7680)
    label[:4] = b"dlV3"
    struct.pack_into(">I", label, 8, layout.image_bytes // 1024)
    label[0x0c:0x0c + 12] = b"OPENSTEP USB"
    label[0x2c:0x2c + 12] = b"OPENSTEP USB"
    label[0x44:0x44 + 17] = b"removable_rw_scsi"
    struct.pack_into(">IIIII", label, 0x5c, 1024, 64, 32,
                     (layout.image_bytes + 2097151) // 2097152, 3600)
    struct.pack_into(">H", label, 0x70, layout.boot_offset // 1024)
    struct.pack_into(">II", label, 0x7c,
                     *[(_USB_PARTITION_OFFSET + offset) // 1024 for offset in _USB_LOADER_OFFSETS])
    label[0x84:0x84 + 12] = b"mach_kernel\0"
    label[0xbc:0xbe] = b"ab"
    for index in range(8):
        part = 0xbe + index * 46
        struct.pack_into(">ii", label, part, -1, -1)
    for index, (offset, size, fs) in enumerate((
            (layout.boot_offset, layout.boot_bytes, boot_fs),
            (layout.installer_offset, layout.installer_bytes, installer_fs))):
        part = 0xbe + index * 46
        struct.pack_into(">iiHH", label, part,
                         (offset - layout.boot_offset) // 1024, size // 1024,
                         fs.block_size, fs.fragment_size)
        label[part + 12] = ord("t")
        struct.pack_into(">HH", label, part + 14, fs.cylinders_per_group, 4096)
        label[part + 18] = 10
        label[part + 37:part + 44] = b"4.3BSD\0"
    struct.pack_into(">H", label, 0x22e, _usb_checksum(label))
    return bytes(label)


def _usb_bootloaders(ufs: PathInput, binary: PathInput | None) -> dict[str, bytes]:
    image = Image(executable(binary), ufs)
    loaders = {name: image.read("/usr/standalone/i386/" + name)
               for name in ("boot0", "boot1", "boot")}
    for name in ("boot0", "boot1"):
        data = loaders[name]
        if len(data) != 512 or data[510:] != b"\x55\xaa" or any(data[446:510]):
            raise ValueError(f"invalid native USB bootloader {name}: expected a 512-byte boot sector with an empty partition table")
    if not 0 < len(loaders["boot"]) <= _USB_LOADER_LIMIT:
        raise ValueError("native USB second-stage bootloader does not fit boot1's load area")
    return loaders


def _usb_boot_table(data: bytes) -> bytes:
    table = Table(data)
    flags = [flag for flag in (table.get("Kernel Flags") or "").split()
             if not flag.startswith("rootdev=") and flag != "-a"]
    table.set("Kernel Flags", " ".join(flags + ["rootdev=sd1b"]))
    table.set("Install Mode", "Yes")
    return table.data()


def _usb_ufs_patches(ufs: PathInput) -> dict[int, bytes]:
    """Use the label's 1 KiB logical blocks in every installer superblock.

    The CD uses 2 KiB blocks. OPENSTEP's kernel trusts fs_fsbtodb instead of
    recalculating it at mount time. Geometry and file contents stay unchanged;
    this filesystem is only used read-only by the installer.
    """
    with Path(ufs).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        stream.seek(8192)
        sb = stream.read(8192)
        if len(sb) != 8192 or sb[1372:1376] != bytes.fromhex("00011954"):
            raise ValueError("missing USB installer superblock")
        sblkno = struct.unpack_from(">I", sb, 8)[0]
        delta, mask = struct.unpack_from(">II", sb, 24)
        groups = struct.unpack_from(">I", sb, 44)[0]
        fragment = struct.unpack_from(">I", sb, 52)[0]
        fpg = struct.unpack_from(">I", sb, 188)[0]
        if fragment < 1024 or fragment & (fragment - 1) or not fpg or not 0 < groups <= size // 8192:
            raise ValueError("invalid USB installer filesystem geometry")
        shift = struct.pack(">I", (fragment // 1024).bit_length() - 1)
        offsets = {8192} | {(group * fpg + delta * (group & ~mask) + sblkno) * fragment
                            for group in range(groups)}
        patches = {}
        for offset in sorted(offsets):
            if offset < 8192 or offset + 8192 > size:
                raise ValueError("USB installer backup superblock is out of bounds")
            stream.seek(offset + 1372)
            if stream.read(4) != bytes.fromhex("00011954"):
                raise ValueError("missing USB installer backup superblock")
            patches[offset + 100] = shift
    return patches


def _check_extent(image: PathInput, offset: int, source: PathInput, *,
                  patches: Mapping[int, bytes] | None = None) -> None:
    with Path(image).open("rb") as target, Path(source).open("rb") as original:
        target.seek(offset)
        position = 0
        while data := original.read(1024 * 1024):
            if patches:
                expected = bytearray(data)
                for start, replacement in patches.items():
                    low, high = max(start, position), min(start + len(replacement), position + len(data))
                    if low < high:
                        expected[low - position:high - position] = replacement[low - start:high - start]
                data = expected
            if target.read(len(data)) != data:
                raise ValueError(f"image payload differs from {source} at byte offset {offset}")
            offset += len(data)
            position += len(data)


def usb_layout(path: PathInput) -> UsbLayout:
    """Validate our USB MBR, all three native labels and the two UFS extents."""
    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        mbr = stream.read(512)
        if (len(mbr) != 512 or mbr[510:] != b"\x55\xaa" or
                mbr[446:454] != bytes.fromhex("80000300a7feffff") or any(mbr[462:510]) or
                struct.unpack_from("<II", mbr, 454) != (2, size // 512 - 2)):
            raise ValueError("invalid USB MBR or NeXT partition bounds")
        labels = []
        for relative in _USB_LABEL_OFFSETS:
            offset = _USB_PARTITION_OFFSET + relative
            stream.seek(offset)
            label = bytearray(stream.read(7680))
            if (len(label) != 7680 or label[:4] != b"dlV3" or
                    struct.unpack_from(">I", label, 4)[0] != offset // 512 or
                    struct.unpack_from(">H", label, 0x22e)[0] != _usb_checksum(label)):
                raise ValueError("invalid USB NeXT label location or checksum")
            label[4:8] = b"\0" * 4
            labels.append(label)
        if labels[1:] != labels[:1] * 2:
            raise ValueError("USB NeXT label copies differ")
        label = labels[0]
        if (struct.unpack_from(">I", label, 0x5c)[0] != 1024 or label[0xbc:0xbe] != b"ab" or
                struct.unpack_from(">II", label, 0x7c) != (33, 97) or
                struct.unpack_from(">I", label, 8)[0] != size // 1024):
            raise ValueError("invalid USB label geometry or boot addresses")
        front = struct.unpack_from(">H", label, 0x70)[0] * 1024
        extents = []
        for index in range(8):
            part = 0xbe + 46 * index
            base, count = struct.unpack_from(">ii", label, part)
            if index >= 2:
                if (base, count) != (-1, -1):
                    raise ValueError("unexpected USB NeXT partition")
                continue
            if base < 0 or count <= 0 or label[part + 37:part + 44] != b"4.3BSD\0":
                raise ValueError("invalid USB UFS partition")
            extents.extend((front + base * 1024, count * 1024))
        layout = UsbLayout(size, *extents)
        if layout != _usb_layout_for_sizes(layout.boot_bytes, layout.installer_bytes):
            raise ValueError("invalid USB filesystem bounds or alignment")
        for offset in (layout.boot_offset, layout.installer_offset):
            stream.seek(offset + 8192 + 1372)
            if stream.read(4) != bytes.fromhex("00011954"):
                raise ValueError("missing USB UFS superblock")
            stream.seek(offset + 8192 + 52)
            fragment = struct.unpack(">I", stream.read(4))[0]
            stream.seek(offset + 8192 + 100)
            shift = struct.unpack(">I", stream.read(4))[0]
            if shift > 8 or fragment != 1024 << shift:
                raise ValueError("USB UFS block addressing differs from the disk label")
    return layout


def _check_usb_boot_files(boot: PathInput, usb: PathInput, binary: PathInput | None) -> None:
    original, target = (Image(executable(binary), path) for path in (boot, usb))
    before, after = (sorted(image.tree("/"), key=lambda entry: entry.name) for image in (original, target))
    if [entry.name for entry in before] != [entry.name for entry in after]:
        raise ValueError("USB boot filesystem entries differ from the prepared boot image")
    for entry, actual in zip(before, after):
        path = "/" + entry.name if entry.name != "." else "/"
        _verify_metadata(entry, actual, path)
        if not entry.is_dir:
            expected = original.read(path, entry)
            if path == _USB_SYSTEM_TABLE:
                expected = _usb_boot_table(expected)
            if target.read(path, actual) != expected:
                raise ValueError(f"USB boot file differs: {path}")


def create_usb(boot: PathInput, ufs: PathInput, output: PathInput, *,
               nextufs_binary: PathInput | None = None) -> UsbLayout:
    """Assemble a native BIOS disk with boot UFS a and read-only installer UFS b."""
    loaders = _usb_bootloaders(ufs, nextufs_binary)
    installer_info = _raw_ufs_info(ufs, nextufs_binary)
    patches = _usb_ufs_patches(ufs)
    with new_output(output) as staged:
        raw_boot = staged.parent / "boot.ufs"
        extract_ufs(boot, raw_boot, nextufs_binary=nextufs_binary)
        image = Image(executable(nextufs_binary), raw_boot)
        entry = image.inspect(_USB_SYSTEM_TABLE)[0]
        parent = _USB_SYSTEM_TABLE.rsplit("/", 1)[0]
        parent_entry = image.inspect(parent)[0]
        image.write(_USB_SYSTEM_TABLE, _usb_boot_table(image.read(_USB_SYSTEM_TABLE, entry)), entry, True)
        image.metadata(parent, parent_entry)
        check_image(raw_boot, nextufs_binary=nextufs_binary)
        boot_info = _raw_ufs_info(raw_boot, nextufs_binary)
        layout = _usb_layout_for_sizes(boot_info.filesystem_bytes, installer_info.filesystem_bytes)
        label = _usb_label(layout, boot_info.filesystem, installer_info.filesystem)
        mbr = bytearray(loaders["boot0"])
        mbr[446:454] = bytes.fromhex("80000300a7feffff")
        struct.pack_into("<II", mbr, 454, 2, layout.image_bytes // 512 - 2)
        with staged.open("w+b") as target:
            target.truncate(layout.image_bytes)
            target.write(mbr)
            target.seek(_USB_PARTITION_OFFSET)
            target.write(loaders["boot1"])
            for relative in _USB_LABEL_OFFSETS:
                offset = _USB_PARTITION_OFFSET + relative
                copy = bytearray(label)
                struct.pack_into(">I", copy, 4, offset // 512)
                target.seek(offset)
                target.write(copy)
            for relative in _USB_LOADER_OFFSETS:
                target.seek(_USB_PARTITION_OFFSET + relative)
                target.write(loaders["boot"])
            for offset, source in ((layout.boot_offset, raw_boot), (layout.installer_offset, ufs)):
                target.seek(offset)
                with Path(source).open("rb") as payload:
                    shutil.copyfileobj(payload, target, 1024 * 1024)
            for offset, replacement in patches.items():
                target.seek(layout.installer_offset + offset)
                target.write(replacement)
        if usb_layout(staged) != layout:
            raise ValueError("assembled USB layout differs")
        _check_extent(staged, layout.boot_offset, raw_boot)
        _check_extent(staged, layout.installer_offset, ufs, patches=patches)
        _check_usb_boot_files(boot, staged, nextufs_binary)
    return layout


def resolve_iso_tool(tool: PathInput | None = None, *,
                     nextufs_binary: PathInput | None = None) -> str:
    """Resolve a mkisofs-compatible executable before starting expensive work."""
    env = _subprocess_env(nextufs_binary)
    search_path = env["PATH"] if env is not None else None
    executable = shutil.which(os.fspath(tool), path=search_path) if tool else next(
        (found for name in ("mkisofs", "genisoimage", "xorriso")
         if (found := shutil.which(name, path=search_path))), None)
    if not executable:
        if tool is not None:
            raise FileNotFoundError(f"ISO tool executable not found: {tool}")
        raise FileNotFoundError("install mkisofs, genisoimage, or xorriso, or pass --iso-tool PATH")
    return str(Path(executable).absolute())


def iso_command(boot: PathInput, ufs: PathInput, output: PathInput,
                tool: PathInput | None = None, *, volume_id: str = "OPENSTEP",
                nextufs_binary: PathInput | None = None) -> list[str]:
    """Use the common mkisofs interface, with floppy emulation (not no-emulation)."""
    executable = resolve_iso_tool(tool, nextufs_binary=nextufs_binary)
    command = [executable]
    if Path(executable).stem.lower() == "xorriso":
        command += ["-as", "mkisofs"]
    return command + ["-iso-level", "3", "-V", volume_id,
                      "-b", "000BOOT.IMG", "-c", "BOOT.CAT", "-boot-load-size", "1",
                      "-o", os.fspath(output), "-graft-points",
                      "000BOOT.IMG=" + Path(boot).as_posix(),
                      "USER.UFS=" + Path(ufs).as_posix()]


@dataclass(frozen=True)
class IsoLayout:
    boot_lba: int
    ufs_lba: int
    catalog_lba: int
    ufs_bytes: int


def iso_layout(path: PathInput) -> IsoLayout:
    """Read only the ISO root entries and BIOS boot catalog needed by this builder."""
    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size

        def read(offset: int, count: int) -> bytes:
            if offset < 0 or count < 0 or offset + count > size:
                raise ValueError("ISO extent lies outside the image")
            stream.seek(offset)
            data = stream.read(count)
            if len(data) != count:
                raise ValueError("truncated ISO")
            return data

        root = None
        catalog_lba = None
        for sector in range(16, 80):
            descriptor = read(sector * 2048, 2048)
            if descriptor[1:7] != b"CD001\x01":
                raise ValueError("invalid ISO volume descriptor")
            if descriptor[0] == 1:
                if struct.unpack_from("<H", descriptor, 128)[0] != 2048:
                    raise ValueError("expected 2048-byte ISO sectors")
                root = descriptor[156:190]
            elif descriptor[0] == 0 and descriptor[7:39].rstrip(b"\0 ") == b"EL TORITO SPECIFICATION":
                catalog_lba = struct.unpack_from("<I", descriptor, 71)[0]
            elif descriptor[0] == 255:
                break
        if root is None or catalog_lba is None:
            raise ValueError("missing primary volume descriptor or El Torito boot record")

        def extent(record: bytes) -> tuple[int, int]:
            lba = struct.unpack_from("<I", record, 2)[0]
            length = struct.unpack_from("<I", record, 10)[0]
            if (lba != struct.unpack_from(">I", record, 6)[0] or
                    length != struct.unpack_from(">I", record, 14)[0]):
                raise ValueError("inconsistent ISO directory extent")
            if lba * 2048 + length > size:
                raise ValueError("ISO directory extent lies outside the image")
            return lba, length

        root_lba, root_size = extent(root)
        if root_size > 1024 * 1024:
            raise ValueError("unexpectedly large ISO root directory")
        directory = read(root_lba * 2048, root_size)
        entries = {}
        offset = 0
        while offset < len(directory):
            length = directory[offset]
            if not length:
                offset = (offset // 2048 + 1) * 2048
                continue
            record = directory[offset:offset + length]
            if length < 34 or len(record) != length or 33 + record[32] > length:
                raise ValueError("invalid ISO directory record")
            if not record[25] & 2:
                name = record[33:33 + record[32]]
                if record[25] & 128 or name in entries:
                    raise ValueError("unexpected multi-extent or duplicate ISO file")
                entries[name] = extent(record)
            offset += length
        try:
            boot_lba, boot_size = entries[b"000BOOT.IMG;1"]
            ufs_lba, ufs_bytes = entries[b"USER.UFS;1"]
            cat_lba, _ = entries[b"BOOT.CAT;1"]
        except KeyError as exc:
            raise ValueError(f"missing ISO file: {exc}") from exc
        if boot_size != MAX_BOOT_FLOPPY_KIB * 1024 or cat_lba != catalog_lba:
            raise ValueError("invalid boot image size or catalog location")
        catalog = read(catalog_lba * 2048, 2048)
        if (catalog[:2] != b"\x01\x00" or catalog[30:32] != b"\x55\xaa" or
                sum(struct.unpack("<16H", catalog[:32])) & 0xffff or
                catalog[32:34] != b"\x88\x03" or
                struct.unpack_from("<H", catalog, 38)[0] != 1 or
                struct.unpack_from("<I", catalog, 40)[0] != boot_lba):
            raise ValueError("expected a valid BIOS 2.88 MiB floppy catalog with one load sector")
    return IsoLayout(boot_lba, ufs_lba, catalog_lba, ufs_bytes)


def create_iso(boot: PathInput, user_cd: PathInput, ufs: PathInput, output: PathInput, *,
               volume_id: str = "OPENSTEP", iso_tool: PathInput | None = None,
               nextufs_binary: PathInput | None = None) -> IsoLayout:
    """Combine BIOS floppy booting and OPENSTEP's native UFS CD discovery.

    The BIOS uses the ISO's El Torito catalog to load the floppy image.
    OPENSTEP instead uses a NeXT label in the ISO system area to locate the
    User UFS payload. Both views must point at the extents actually allocated
    by the ISO tool; the ISO filesystem alone is not enough for OPENSTEP.
    """
    boot, user_cd, ufs = map(Path, (boot, user_cd, ufs))
    with new_output(output) as staged:
        if boot.stat().st_size != MAX_BOOT_FLOPPY_KIB * 1024:
            raise ValueError(f"boot image must be exactly {MAX_BOOT_FLOPPY_KIB} KiB")
        label_info = cd_layout(user_cd, nextufs_binary=nextufs_binary)
        ufs_info = _raw_ufs_info(ufs, nextufs_binary)

        # 1. Copy inputs under fixed ISO names. Relative paths avoid Windows
        # drive-letter parsing differences in mkisofs-compatible programs.
        stage = staged.parent
        staged_boot, staged_ufs = stage / "000BOOT.IMG", stage / "USER.UFS"
        shutil.copyfile(boot, staged_boot)
        shutil.copyfile(ufs, staged_ufs)

        # 2. Build the ISO and its BIOS boot catalog in floppy-emulation mode.
        # The one-sector initial load preserves the stock floppy boot protocol.
        command = iso_command(staged_boot.name, staged_ufs.name, staged.name, iso_tool,
                              volume_id=volume_id, nextufs_binary=nextufs_binary)
        completed = subprocess.run(command, cwd=stage, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=_subprocess_env(nextufs_binary))
        if completed.returncode:
            detail = completed.stdout.decode("utf-8", errors="replace").strip()
            message = f"ISO tool {command[0]} failed with exit status {completed.returncode}"
            raise MediaError(message + (f":\n{detail}" if detail else ""))

        # 3. Read the generated directory extents and validate the boot catalog.
        # Different ISO tools may place the boot image and User UFS differently.
        layout = iso_layout(staged)

        # 4. Add OPENSTEP's view of the disc in the reserved ISO system area.
        # Rebase the original NeXT label to USER.UFS and recompute its checksum.
        _hybrid_label(user_cd, staged, layout.ufs_lba, label_info.label.offset, ufs_info.filesystem_bytes)
        _check_iso_ufs(staged, layout, ufs_info, nextufs_binary)

        # 5. Check both embedded payloads before new_output publishes the ISO.
        # The label write must not alter the floppy or the User filesystem.
        check_payload(staged, layout.boot_lba, staged_boot)
        check_payload(staged, layout.ufs_lba, staged_ufs)
    return layout


def _hybrid_label(user_cd: PathInput, output: Path, ufs_lba: int, label_offset: int, ufs_bytes: int) -> None:
    if not 0 <= ufs_lba <= 0xffff:
        raise ValueError("User UFS lies beyond the NeXT label's front-porch field")
    with Path(user_cd).open("rb") as source:
        source.seek(label_offset)
        label = bytearray(source.read(7680))
    if len(label) != 7680 or label[:4] != b"dlV3":
        raise ValueError("missing or truncated dlV3 label")
    root = label[0xbc] - ord("a")
    if not 0 <= root < 8 or struct.unpack_from(">H", label, 0x5e)[0] != 2048:
        raise ValueError("expected a root partition and 2048-byte CD sectors")
    part = 0xc0 + root * 0x40
    if label[part:part + 3] != b"\0\0\0":
        raise ValueError("expected a root partition immediately after the front porch")
    if ufs_bytes <= 0 or ufs_bytes % 2048 or ufs_bytes // 2048 >= 0xffffff:
        raise ValueError("User UFS size cannot be represented in the CD partition label")
    label[part + 3:part + 6] = (ufs_bytes // 2048).to_bytes(3, "big")
    # Relocate this label copy to block zero and the partition's front porch
    # to the new UFS extent. Only the root partition's size changes above;
    # unrelated geometry and partition data survive.
    struct.pack_into(">I", label, 4, 0)
    struct.pack_into(">H", label, 0x70, ufs_lba)
    checksum = sum(struct.unpack(">279H", label[:0x22e]))
    while checksum >> 16:
        checksum = (checksum & 0xffff) + (checksum >> 16)
    struct.pack_into(">H", label, 0x22e, checksum)
    with output.open("r+b") as target:
        target.write(label)


def _check_iso_ufs(iso: PathInput, layout: IsoLayout, prepared: ImageInfo,
                   binary: PathInput | None) -> None:
    embedded = cd_layout(iso, nextufs_binary=binary)
    if (embedded.slice_base != layout.ufs_lba * 2048 or
            not (layout.ufs_bytes == prepared.filesystem_bytes == embedded.slice_bytes ==
                 embedded.filesystem_bytes) or embedded.filesystem != prepared.filesystem):
        raise ValueError("ISO UFS extent, hybrid partition label, or filesystem differs from prepared UFS")


def check_payload(iso: PathInput, lba: int, source: PathInput) -> None:
    """Compare a file against an image extent addressed in 2048-byte sectors."""
    iso, source = map(Path, (iso, source))
    with iso.open("rb") as image, source.open("rb") as original:
        image.seek(lba * 2048)
        remaining = source.stat().st_size
        while remaining:
            count = min(1024 * 1024, remaining)
            expected = original.read(count)
            if len(expected) != count or image.read(count) != expected:
                raise ValueError(f"ISO payload differs from {source}")
            remaining -= count


def verify_boot_cd(boot_disk: PathInput, boot: PathInput, user_cd: PathInput,
                   ufs: PathInput, iso: PathInput, *, nextufs_binary: PathInput | None = None,
                   installation_drivers: bool = False, fix_pic_bug: bool = False,
                   package_hook: bytes = b"", packaged_drivers: Iterable[str] = (),
                   kernel_source: PathInput | None = None, removed_drivers: Iterable[str] = ()) -> None:
    """Check bootloader, El Torito payloads, User UFS, and installer access.

    Read-only verification, including fsck of the prepared floppy; this does not
    establish that the ISO boots successfully in a VM. With installation_drivers,
    verify the installer hook, driver archive and optional patched kernel instead
    of requiring the embedded User UFS to be byte-identical to the original CD.
    kernel_source identifies the independently prepared patched User filesystem;
    omit it to require the original CD and boot-floppy kernels.
    """
    boot_disk, boot, user_cd, ufs, iso = map(Path, (boot_disk, boot, user_cd, ufs, iso))
    packaged_drivers = tuple(packaged_drivers)
    _verify_prepared_boot(boot_disk, boot, nextufs_binary, fix_pic_bug, kernel_source)
    layout = iso_layout(iso)
    check_payload(iso, layout.boot_lba, boot)
    check_payload(iso, layout.ufs_lba, ufs)
    user_layout = cd_layout(user_cd, nextufs_binary=nextufs_binary)
    _check_iso_ufs(iso, layout, _raw_ufs_info(ufs, nextufs_binary), nextufs_binary)
    if not installation_drivers:
        check_payload(user_cd, user_layout.slice_base // 2048, ufs)
    _verify_installer(boot, user_cd, iso, nextufs_binary=nextufs_binary,
                      installation_drivers=installation_drivers, fix_pic_bug=fix_pic_bug,
                      package_hook=package_hook, packaged_drivers=packaged_drivers,
                      kernel_source=kernel_source, removed_drivers=removed_drivers)


def _verify_prepared_boot(boot_disk: Path, boot: Path, nextufs_binary: PathInput | None,
                          fix_pic_bug: bool, kernel_source: PathInput | None) -> None:
    with boot_disk.open("rb") as source, boot.open("rb") as prepared:
        original, grown = source.read(65536), prepared.read(65536)
    if len(original) != 65536 or boot.stat().st_size != MAX_BOOT_FLOPPY_KIB * 1024:
        raise ValueError(f"expected an original boot floppy and a {MAX_BOOT_FLOPPY_KIB} KiB boot image")
    # Resizing may update the three disk labels, but not the bootloader between them.
    for offset, (old, new) in enumerate(zip(original, grown)):
        if old != new and not any(label <= offset < label + 0x300 for label in (7680, 15360, 23040)):
            raise ValueError("bootloader bytes changed")
    expected_boot = nextufs("browse", "--raw", kernel_source or boot_disk, "/mach_kernel", binary=nextufs_binary)
    expected_boot = _i386_kernel(expected_boot)
    if fix_pic_bug:
        expected_boot = patch_pic_kernel(expected_boot)
    if nextufs("browse", "--raw", boot, "/mach_kernel", binary=nextufs_binary) != expected_boot:
        raise ValueError("boot-floppy kernel differs from the expected Intel kernel")
    check_image(boot, nextufs_binary=nextufs_binary)


def verify_boot_usb(boot_disk: PathInput, boot: PathInput, user_cd: PathInput,
                    ufs: PathInput, usb: PathInput, *, nextufs_binary: PathInput | None = None,
                    fix_pic_bug: bool = False, package_hook: bytes = b"",
                    packaged_drivers: Iterable[str] = (), kernel_source: PathInput | None = None,
                    removed_drivers: Iterable[str] = ()) -> None:
    """Verify native boot stages, prepared boot files, and installer UFS b."""
    _verify_prepared_boot(Path(boot_disk), Path(boot), nextufs_binary, fix_pic_bug, kernel_source)
    layout = usb_layout(usb)
    if layout.installer_bytes != _raw_ufs_info(ufs, nextufs_binary).filesystem_bytes:
        raise ValueError("USB installer partition size differs from prepared UFS")
    _check_extent(usb, layout.installer_offset, ufs, patches=_usb_ufs_patches(ufs))
    loaders = _usb_bootloaders(ufs, nextufs_binary)
    with Path(usb).open("rb") as image:
        if image.read(446) != loaders["boot0"][:446]:
            raise ValueError("USB MBR boot code differs")
        image.seek(_USB_PARTITION_OFFSET)
        if image.read(512) != loaders["boot1"]:
            raise ValueError("USB partition boot code differs")
        for offset in _USB_LOADER_OFFSETS:
            image.seek(_USB_PARTITION_OFFSET + offset)
            if image.read(len(loaders["boot"])) != loaders["boot"]:
                raise ValueError("USB second-stage boot code differs")
    _check_usb_boot_files(boot, usb, nextufs_binary)
    check_image(usb, nextufs_binary=nextufs_binary)
    _verify_installer(boot, user_cd, ufs, nextufs_binary=nextufs_binary,
                      installation_drivers=True, fix_pic_bug=fix_pic_bug, package_hook=package_hook,
                      packaged_drivers=tuple(packaged_drivers), kernel_source=kernel_source, usb=True,
                      removed_drivers=removed_drivers)


def _verify_installer(boot: PathInput, user_cd: PathInput, iso: PathInput, *,
                      nextufs_binary: PathInput | None, installation_drivers: bool,
                      fix_pic_bug: bool, package_hook: bytes, packaged_drivers: Iterable[str],
                      kernel_source: PathInput | None, usb: bool = False,
                      removed_drivers: Iterable[str] = ()) -> None:
    """Check installer contents through either the CD view or the raw USB UFS."""
    original_script = nextufs("browse", "--raw", user_cd, "/etc/rc.cdrom", binary=nextufs_binary)
    if installation_drivers:
        _, system = system_table(Image(executable(nextufs_binary), boot))
        boot_names, active_names = driver_lists(system)
        removed_drivers = tuple(removed_drivers)
        if removed_drivers:
            present = set(DriverImage(boot, nextufs=nextufs_binary).list_drivers())
            if set(removed_drivers) & (present | set(boot_names + active_names)):
                raise ValueError("excluded drivers remain on the boot floppy")
        expected_script = _patch_cd_installer(original_script, boot_names,
                                              fix_pic_bug=fix_pic_bug, package_hook=package_hook,
                                              active_drivers=(name for name in active_names if name in packaged_drivers),
                                              removed_drivers=removed_drivers)
        archive = nextufs("browse", "--raw", iso, _INSTALL_DRIVER_ARCHIVE, binary=nextufs_binary)
        if archive != _boot_driver_archive(boot, nextufs_binary=nextufs_binary, packaged_drivers=packaged_drivers):
            raise ValueError("installation drivers differ from the boot floppy")
        original_kernel = nextufs("browse", "--raw", kernel_source or user_cd, "/mach_kernel", binary=nextufs_binary)
        if nextufs("browse", "--raw", iso, "/mach_kernel", binary=nextufs_binary) != original_kernel:
            raise ValueError("User CD kernel differs from the expected kernel")
        if fix_pic_bug:
            expected_kernel = patch_pic_kernel(_i386_kernel(original_kernel))
            if nextufs("browse", "--raw", iso, _INSTALL_KERNEL, binary=nextufs_binary) != expected_kernel:
                raise ValueError("installation kernel differs from the PIC-patched User CD Intel kernel")
            image = Image(executable(nextufs_binary), iso)
            helper = image.inspect(_INSTALL_PIC_SCRIPT)[0]
            if ((helper.mode, helper.uid, helper.gid) != (stat.S_IFREG | 0o755, 0, 0) or
                    image.read(_INSTALL_PIC_SCRIPT) != _pic_fix_script()):
                raise ValueError("PIC patch helper differs or has incorrect permissions/ownership")
        else:
            image = Image(executable(nextufs_binary), iso)
            for payload in (_INSTALL_KERNEL, _INSTALL_PIC_SCRIPT):
                if image.child("/NextCD", payload.rsplit("/", 1)[1]) is not None:
                    raise ValueError(f"unexpected {payload} without fix_pic_bug")
    else:
        expected_script = original_script
    if usb:
        expected_script = _patch_usb_installer(expected_script)
    installer = nextufs("browse", "--raw", iso, "/etc/rc.cdrom", binary=nextufs_binary)
    if installer != expected_script:
        raise ValueError("installer script differs from the prepared installation hook")
