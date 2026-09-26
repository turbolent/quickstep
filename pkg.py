"""Offline OPENSTEP package installation, including native receipts.

Image access and transactional output are provided by media.py.
See docs/installer.md for supported package formats and offline limits.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
import hashlib
import io
from pathlib import Path, PureWindowsPath
import re
import shlex
import stat
import struct
import tarfile
import time

import bom
import media


__all__ = ["PackageInfo", "prepare_package", "InstalledPackage", "install_package", "installation_hook",
           "InstalledDriver", "install_driver_package", "PreparedUserPatch", "prepare_user_patch",
           "user_patch_installation_hook"]


@dataclass(frozen=True)
class PackageInfo:
    name: str
    title: str
    version: str
    relocatable: bool


@dataclass(frozen=True)
class InstalledPackage:
    """Published package identity and absolute payload paths in the image."""

    name: str
    location: str
    paths: tuple[str, ...]

    @property
    def receipt(self) -> str:
        return "/NextLibrary/Receipts/" + self.name + ".pkg"


@dataclass(frozen=True)
class InstalledDriver:
    name: str
    package: InstalledPackage


@dataclass(frozen=True)
class PreparedUserPatch:
    package: InstalledPackage
    receipt_source: str


_USER_PATCH = "OS42MachUserPatch4"
# The original hook only runs disk -b on the i386 root device. We implement
# that action against rc.cdrom's selected disk, never against the build host.
_USER_PATCH_POST_SHA256 = "ecac37f4f2799e006ffe7fb14f13dfd8c88f8f326164b06ec29461d8a4aea89e"


def prepare_user_patch(package: media.PathInput, target_ufs: media.PathInput,
                       output: media.PathInput, *,
                       nextufs_binary: media.PathInput | None = None) -> PreparedUserPatch:
    """Overlay User Patch 4 and stage its receipt outside BuildDisk's inventory.

    Only the known bootloader hook is accepted. The destination disk's boot
    area is updated later by user_patch_installation_hook(), before its receipt
    is published. The CD's embedded floppy bootloader is intentionally unchanged.
    """
    name, files = _package_bundle(package)
    script = files.get(name + ".post_install")
    info = files.get(name + ".info")
    hooks = {path for path in files if path.endswith((".pre_install", ".post_install"))}
    if (name != _USER_PATCH or info is None or not stat.S_ISREG(info.entry.mode) or
            script is None or not stat.S_ISREG(script.entry.mode) or
            hooks != {name + ".post_install"} or
            hashlib.sha256(script.data).hexdigest() != _USER_PATCH_POST_SHA256):
        raise ValueError("expected User Patch 4 with its original bootloader post-install script")
    fields = _package_fields(info.data)
    if (fields.get("version", "").strip() != "OPENSTEP Release 4.2 Patch 4" or
            fields.get("defaultlocation") != "/" or fields.get("relocatable", "NO").upper() != "NO"):
        raise ValueError("unexpected User Patch 4 version or installation location")
    del files[name + ".post_install"]
    receipt = "/NextCD/Receipts/" + name + ".pkg"
    with media.new_output(output) as staged:
        installed = _install_package(name, files, target_ufs, staged, location=None, languages=(),
                                     nextufs_binary=nextufs_binary, receipt_root="/NextCD/Receipts")
        image = media.Image(media.executable(nextufs_binary), staged)
        kernel = media._i386_kernel(image.read("/mach_kernel"))
        if b"mk-183.34.4" not in kernel or not image.read("/usr/standalone/i386/boot"):
            raise ValueError("User Patch 4 kernel or bootloader is missing or unrecognized")
    return PreparedUserPatch(installed, receipt)


def install_driver_package(package: media.PathInput, target_ufs: media.PathInput,
                           output: media.PathInput, *,
                           nextufs_binary: media.PathInput | None = None) -> InstalledDriver:
    """Install a package containing one driver bundle directly in /private/Devices.

    Preserve payload bytes and receipts, but give the installed bundle to root:
    driverLoader rejects bundles carrying the package builder's uid.
    Configuration and activation are separate operations.
    """
    with media.new_output(output) as staged:
        installed = install_package(package, target_ufs, staged, nextufs_binary=nextufs_binary)
        bundles = [path for path in installed.paths if path.endswith(".config")]
        if (installed.location != "/private/Devices" or len(bundles) != 1 or
                bundles[0].rsplit("/", 1)[0] != installed.location):
            raise ValueError(f"installation driver package must install exactly one .config bundle "
                             f"directly in /private/Devices: {package}")
        name = media.driver_name(bundles[0].rsplit("/", 1)[1])
        if name == "System" or re.fullmatch(r"[A-Za-z0-9_.+-]+", name) is None:
            raise ValueError(f"unsupported installation driver name: {name}")
        image = media.Image(media.executable(nextufs_binary), staged)
        if not image.inspect(bundles[0])[0].is_dir:
            raise ValueError(f"installation driver bundle is not a directory: {bundles[0]}")
        # Every bundle needs its default even if it already has an instance.
        media.Table(image.read(bundles[0] + "/Default.table"))
        for entry in image.tree(bundles[0]):
            path = bundles[0] if entry.name == "." else bundles[0] + "/" + entry.name
            if entry.uid != 0:
                expected = replace(entry, uid=0)
                image.metadata(path, expected)
                media._verify_metadata(expected, image.inspect(path)[0], path)
    return InstalledDriver(name, installed)


def installation_hook(packages: Iterable[InstalledPackage]) -> bytes:
    """Copy required, already-installed CD packages during stock rc.cdrom setup.

    Use its ROOT, HD, DITTO, MKDIRS, RM and MV variables. Retain all architectures
    and stage each receipt until both payload and receipt copying succeed.
    """
    return b"".join(_installation_hook(package, package.receipt) for package in packages)


def user_patch_installation_hook(patch: PreparedUserPatch) -> bytes:
    """Copy Patch 4, install its hard-disk booter, then publish its receipt."""
    if patch.package.name != _USER_PATCH or patch.package.location != "/":
        raise ValueError("expected a prepared User Patch 4 package")
    return _installation_hook(patch.package, patch.receipt_source, bootloader=True)


def _installation_hook(package: InstalledPackage, receipt_source: str, *, bootloader: bool = False) -> bytes:
    media.component(package.name)
    location = _package_path(package.location, absolute=True)
    receipt_source = _package_path(receipt_source, absolute=True)
    if not location.startswith("/") or not receipt_source.startswith("/") or any(
            path != location and not path.startswith(location.rstrip("/") + "/")
            for path in package.paths):
        raise ValueError("CD package payload must be relative to its installation location")
    receipt = package.receipt
    temporary = "/NextLibrary/Receipts/." + package.name + ".pkg.quickstep"
    # Explicit traversal preserves rc.cdrom's /private/Devices symlink.
    directory = location.rstrip("/") + "/."

    def source(path: str) -> str:
        return '"${ROOT}"' + shlex.quote(path)

    def target(path: str) -> str:
        return '"${HD}"' + shlex.quote(path)

    finalizer = ('        echo "Installing Patch 4 bootloader on ${rawdisk}..." &&\n'
                 '        ${DISK} -b "${rawdisk}" &&\n') if bootloader else ""
    return f'''    echo {shlex.quote("Installing required package " + package.name + "...")}
    if ${{MKDIRS}} {target(location)} &&
        ${{DITTO}} -bom {source(receipt_source + "/" + package.name + ".bom")} {source(directory)} {target(directory)} &&
{finalizer}        ${{MKDIRS}} {target("/NextLibrary/Receipts")} &&
        ${{RM}} -rf {target(temporary)} &&
        ${{DITTO}} {source(receipt_source)} {target(temporary)} &&
        ${{RM}} -rf {target(receipt)} &&
        ${{MV}} {target(temporary)} {target(receipt)}; then
        echo {shlex.quote("Installed package " + package.name + " and its receipt.")}
    else
        echo {shlex.quote("Cannot install required package " + package.name + "; installation stopped.")}
        exit 1
    fi
'''.encode("utf-8")


def _uncompress(data: bytes) -> bytes:
    """Decode UNIX compress (.Z), including width changes and block clears."""
    if len(data) < 3 or data[:2] != b"\x1f\x9d":
        raise ValueError("expected a UNIX compress (.Z) stream")
    max_bits, block_mode = data[2] & 31, bool(data[2] & 128)
    if data[2] & 96 or not 9 <= max_bits <= 16:
        raise ValueError("unsupported compress header")
    data = data[3:]
    table = [bytes([i]) for i in range(256)]
    if block_mode:
        table.append(b"")
    width, position, group_start = 9, 0, 0
    previous = b""
    result = bytearray()

    def align() -> int:
        group = width * 8
        return group_start + ((position - group_start + group - 1) // group) * group

    while True:
        if len(table) >= (1 << width) and width < max_bits:
            position = align()
            group_start = position
            width += 1
        if position + width > len(data) * 8:
            break
        offset, shift = divmod(position, 8)
        code = (int.from_bytes(data[offset:offset + 3], "little") >> shift) & ((1 << width) - 1)
        position += width
        if block_mode and code == 256:
            position = align()
            group_start = position
            width = 9
            del table[257:]
            previous = b""
            continue
        if code < len(table):
            value = table[code]
        elif code == len(table) and previous:
            value = previous + previous[:1]
        else:
            raise ValueError("invalid compress dictionary reference")
        if not value:
            raise ValueError("invalid compress code")
        result.extend(value)
        if previous and len(table) < (1 << max_bits):
            table.append(previous + value[:1])
        previous = value
    return bytes(result)


class _BigTarInfo(tarfile.TarInfo):
    """NeXT's -B archive has 225-byte name/link fields, not POSIX ustar."""

    @classmethod
    def fromtarfile(cls, archive: tarfile.TarFile) -> tarfile.TarInfo:
        # Newer tarfile versions bypass frombuf() in the default reader.
        # Route every header through our NeXT checksum and field conversion.
        item = cls.frombuf(archive.fileobj.read(512), archive.encoding, archive.errors)
        item.offset = archive.fileobj.tell() - 512
        return item._proc_member(archive)

    @classmethod
    def frombuf(cls, buf: bytes, encoding: str, errors: str) -> tarfile.TarInfo:
        if len(buf) != 512 or not any(buf):
            return super().frombuf(buf, encoding, errors)
        try:
            stored = int(buf[273:281].strip(b"\0 "), 8)
        except ValueError as exc:
            raise tarfile.InvalidHeaderError("invalid bigtar checksum") from exc
        check = buf[:273] + b" " * 8 + buf[281:]
        if stored not in (sum(check), sum(v if v < 128 else v - 256 for v in check)):
            raise tarfile.InvalidHeaderError("bad bigtar checksum")
        # Reuse tarfile's numeric/type handling after validating the original.
        ordinary = bytearray(512)
        ordinary[:100] = buf[:100]
        ordinary[100:257] = buf[225:382]
        ordinary[148:156] = b" " * 8
        ordinary[148:156] = f"{sum(ordinary):06o}\0 ".encode("ascii")
        item = super().frombuf(bytes(ordinary), encoding, errors)
        item.name = buf[:225].split(b"\0", 1)[0].decode(encoding, errors)
        item.linkname = buf[282:507].split(b"\0", 1)[0].decode(encoding, errors)
        if item.type == tarfile.AREGTYPE and item.name.endswith("/"):
            item.type = tarfile.DIRTYPE
        if item.isdir():
            item.name = item.name.rstrip("/")
        return item


def _package_path(path: str, *, absolute: bool = False) -> str:
    """Normalize ./ prefixes only; never accept traversal or host paths."""
    if "\\" in path or PureWindowsPath(path).drive:
        raise ValueError(f"invalid package path: {path!r}")
    if path.startswith("/"):
        if not absolute:
            raise ValueError(f"absolute package path: {path!r}")
        prefix, path = "/", path[1:]
    else:
        prefix = ""
    while path.startswith("./"):
        path = path[2:]
    if path in ("", "."):
        return prefix or "."
    for part in path.split("/"):
        media.component(part)
        if len(part.encode("utf-8")) > 255:
            raise ValueError(f"package filename exceeds UFS limit: {path!r}")
    return prefix + path


@dataclass(frozen=True)
class _PackageFile:
    entry: media.Entry
    data: bytes = b""
    link: str | None = None
    hard_link: bool = False


def _package_bundle(path: media.PathInput) -> tuple[str, dict[str, _PackageFile]]:
    """Read a .pkg directory or a tar containing exactly one .pkg directory."""
    path = Path(path)
    files: dict[str, _PackageFile] = {}
    mode = media.local_stat(path).st_mode
    if stat.S_ISDIR(mode):
        name = path.name
        for entry in media.local_tree(path):
            files[entry.name] = _PackageFile(entry, b"" if entry.is_dir else (path / entry.name).read_bytes())
    else:
        if not stat.S_ISREG(mode):
            raise ValueError(f"package archive must be a regular file: {path}")
        data = path.read_bytes()
        if data.startswith(b"\x1f\x9d"):
            data = _uncompress(data)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            entries = media._tar_entries(archive)
            roots = {item.entry.name.split("/", 1)[0] for item in entries}
            if len(roots) != 1:
                raise ValueError("package archive must contain exactly one .pkg directory")
            name = roots.pop()
            for item in entries:
                relative = item.entry.name[len(name):].lstrip("/") or "."
                files[relative] = _PackageFile(replace(item.entry, name=relative),
                                              b"" if item.entry.is_dir else media._tar_read(archive, item))
    if (not name.endswith(".pkg") or name == ".pkg" or
            "." not in files or not files["."].entry.is_dir):
        raise ValueError("expected an OPENSTEP .pkg directory")
    media.component(name)
    for relative in files:
        _package_path(relative)
    return name[:-4], files


def _package_fields(data: bytes, *, numeric: bool = False) -> dict[str, str]:
    """Read .info values verbatim (up to 1023 bytes), or .sizes tokens."""
    result = {}
    for line in data.decode("latin-1").split("\n"):
        line = line.lstrip(" \t\r\v\f")
        if not line or line.startswith("#"):
            continue
        fields = re.split(r"[ \t\r\v\f]+", line, maxsplit=1)
        if len(fields) != 2:
            raise ValueError(f"invalid package metadata line: {line!r}")
        value = fields[1][:1023]
        if numeric:
            value = re.split(r"[ \t\r\v\f]+", value, maxsplit=1)[0]
        result[fields[0].lower()] = value
    return result


def prepare_package(package: media.PathInput, output: media.PathInput) -> PackageInfo:
    """Stage a .pkg directory or single-package tar for copying onto a CD.

    Preserve the bundle, including scripts and its compressed payload, without
    installing it or executing anything. Return metadata for a Setup catalog.
    """
    name, files = _package_bundle(package)

    def resource(extension: str, *, localized: bool = False) -> _PackageFile:
        leaf = name + "." + extension
        for path in (["English.lproj/" + leaf] if localized else []) + [leaf]:
            item = files.get(path)
            if item is not None and stat.S_ISREG(item.entry.mode):
                return item
        raise ValueError(f"missing package resource: {name}.pkg/{leaf}")

    fields = _package_fields(resource("info", localized=True).data)
    for field in ("title", "version", "description", "defaultlocation", "diskname"):
        if not fields.get(field, "").strip():
            raise ValueError(f"missing package info field {field}: {name}.pkg")
    relocatable = fields.get("relocatable", "NO").strip().upper()
    if relocatable not in ("YES", "NO"):
        raise ValueError(f"invalid package Relocatable value: {name}.pkg")
    location = fields["defaultlocation"].strip()
    if not location.startswith("/") and relocatable != "YES":
        raise ValueError(f"non-relocatable package requires an absolute DefaultLocation: {name}.pkg")
    resource("sizes", localized=True)
    resource("bom")
    resource("tar.Z")
    info = PackageInfo(name, fields["title"].strip(), fields["version"].strip(), relocatable == "YES")
    # Validate strings before staging; Setup's generated plist is ASCII.
    media.setup_plist(media.SetupCatalog(
        (media.SetupPackage(info.name, info.version, relocatable=info.relocatable),),
        (media.SetupChoice("Drivers", info.title, (info.name,)),)))
    with media.new_output(output) as staged:
        with tarfile.open(staged, "w") as archive:
            for relative, item in files.items():
                entry = item.entry
                path = name + ".pkg" + ("/" + relative if relative != "." else "")
                member = tarfile.TarInfo(path)
                member.type = tarfile.DIRTYPE if entry.is_dir else tarfile.REGTYPE
                member.mode, member.uid, member.gid = stat.S_IMODE(entry.mode), entry.uid, entry.gid
                member.mtime, member.size = entry.mtime, len(item.data)
                archive.addfile(member, None if entry.is_dir else io.BytesIO(item.data))
    return info


def _package_checksum(data: bytes) -> int:
    """OPENSTEP UnixFSTree sum: big-endian words, overlapping last word."""
    if len(data) < 4:
        return sum((v if v < 128 else v - 256) << (8 * i) for i, v in enumerate(data)) & 0xffffffff
    size = (len(data) - 1) // 4 * 4
    return (sum(value[0] for value in struct.iter_unpack(">I", data[:size])) +
            int.from_bytes(data[-4:], "big")) & 0xffffffff


def _package_payload(data: bytes, *, big: bool) -> dict[str, _PackageFile]:
    if data.startswith(b"\x1f\x9d"):
        data = _uncompress(data)
    files: dict[str, _PackageFile] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*",
                      tarinfo=_BigTarInfo if big else tarfile.TarInfo) as archive:
        for item in archive:
            name = _package_path(item.name, absolute=True)
            if name in files:
                raise ValueError(f"duplicate package payload path: {name}")
            if item.issparse() or not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                raise ValueError(f"unsupported package payload type: {name}")
            kind = stat.S_IFDIR if item.isdir() else stat.S_IFLNK if item.issym() else stat.S_IFREG
            entry = media.Entry(0, kind | stat.S_IMODE(item.mode), item.uid, item.gid,
                          item.size, int(item.mtime), int(item.mtime), name)
            content = b""
            if item.isfile():
                with archive.extractfile(item) as stream:
                    content = stream.read()
                if len(content) != item.size:
                    raise ValueError(f"truncated package payload: {name}")
            link = item.linkname if item.issym() or item.islnk() else None
            if link is not None and (not link or "\0" in link):
                raise ValueError(f"invalid package link: {name}")
            if item.islnk():
                link = _package_path(link, absolute=True)
            files[name] = _PackageFile(entry, content, link, item.islnk())
    return files


def _validate_package_bom(data: bytes, payload: dict[str, _PackageFile]) -> None:
    entries = bom.parse(data)
    inventory = {_package_path(entry.path, absolute=True): entry for entry in entries}
    if len(inventory) != len(entries):
        raise ValueError("duplicate normalized package BOM paths")
    if inventory.keys() != payload.keys():
        difference = sorted(inventory.keys() ^ payload.keys())
        raise ValueError(f"package BOM/archive inventory differs: {difference[:8]}")
    bom_links: dict[int, set[str]] = {}
    tar_links: dict[str, set[str]] = {}
    for name, item in payload.items():
        expected, actual = inventory[name], item.entry
        if item.hard_link:
            seen = {name}
            source = item
            while source.hard_link:
                if source.link in seen or source.link not in payload:
                    raise ValueError(f"invalid package hard link: {name}")
                seen.add(source.link)
                source = payload[source.link]
            if not stat.S_ISREG(source.entry.mode):
                raise ValueError(f"hard link does not name a regular file: {name}")
            actual = source.entry
        else:
            source = item
        if (expected.mode, expected.uid, expected.gid, expected.mtime) != (
                actual.mode, actual.uid, actual.gid, actual.mtime):
            raise ValueError(f"package BOM/archive metadata differs: {name}")
        if stat.S_ISREG(actual.mode) and (expected.size != len(source.data) or
                                         expected.checksum != _package_checksum(source.data)):
            raise ValueError(f"package BOM/archive size or checksum differs: {name}")
        if stat.S_ISLNK(actual.mode) and expected.link_target != item.link:
            raise ValueError(f"package BOM/archive symlink differs: {name}")
        if stat.S_ISREG(actual.mode):
            bom_links.setdefault(expected.inode, set()).add(name)
            tar_links.setdefault(source.entry.name, set()).add(name)
    # mkbom and tar can encounter the members of a hard-link group in different
    # orders. Compare the groups, not which member each format chose as primary.
    if {frozenset(group) for group in bom_links.values()} != {
            frozenset(group) for group in tar_links.values()}:
        raise ValueError("package BOM/archive hard link groups differ")


def install_package(package: media.PathInput, target_ufs: media.PathInput, output: media.PathInput, *,
                    location: str | None = None, languages: Iterable[str] = ("English",),
                    nextufs_binary: media.PathInput | None = None) -> InstalledPackage:
    """Install a script-free OPENSTEP .pkg into a new raw UFS image.

    Accept a local .pkg directory or a tar containing one. Validate the native
    binary BOM, preserve all architectures/languages and payload metadata, then
    publish /NextLibrary/Receipts/NAME.pkg with installed status and location.
    Existing regular files are replaced; unrelated files survive a reinstall.
    Refuse install hooks (including localized hooks), SourceLocation packages,
    remote/split archives, text BOMs, UseUserUmask/UseUserMask, devices, and symlink parents.
    No package code is executed on the host. Inputs are never modified and no
    output is published on failure. Driver activation is a separate operation.
    Return the installed package's identity, location and payload paths.
    """
    name, files = _package_bundle(package)
    for relative in files:
        if relative.endswith((".pre_install", ".post_install")):
            raise ValueError(f"offline installation rejects install scripts: {name}.pkg/{relative}")
    return _install_package(name, files, target_ufs, output, location=location, languages=languages,
                            nextufs_binary=nextufs_binary)


def _install_package(name: str, files: dict[str, _PackageFile], target_ufs: media.PathInput,
                     output: media.PathInput, *, location: str | None, languages: Iterable[str],
                     nextufs_binary: media.PathInput | None,
                     receipt_root: str = "/NextLibrary/Receipts") -> InstalledPackage:
    language_dirs = [media.component(language) + ".lproj" for language in languages]

    def resource(extension: str, *, localized: bool = False) -> _PackageFile:
        candidates = ([folder + "/" + name + "." + extension for folder in language_dirs]
                      if localized else []) + [name + "." + extension]
        for candidate in candidates:
            item = files.get(candidate)
            if item is not None and stat.S_ISREG(item.entry.mode):
                return item
        raise ValueError(f"missing package resource: {name}.{extension}")

    info = _package_fields(resource("info", localized=True).data)
    for field in ("title", "version", "description", "defaultlocation", "diskname"):
        if not info.get(field):
            raise ValueError(f"missing package info field: {field}")
    for field in ("relocatable", "application", "useusermask", "useuserumask", "longfilenames"):
        if info.get(field, "NO").upper() not in ("YES", "NO"):
            raise ValueError(f"invalid package boolean: {field}")
    if info.get("sourcelocation") or any(info.get(field, "NO").upper() == "YES"
                                       for field in ("useusermask", "useuserumask")):
        raise ValueError("offline installation does not support SourceLocation or UseUserUmask/UseUserMask")
    default = info["defaultlocation"]
    if any(char.isspace() or char in "!$^&*(){}[]\\|;<>?'\"`" for char in default):
        raise ValueError("invalid package DefaultLocation: whitespace or shell metacharacters")
    if location is None:
        location = default
    elif location != default and info.get("relocatable", "NO").upper() != "YES":
        raise ValueError("package is not relocatable")
    if not location.startswith("/"):
        raise ValueError("package installation location must be absolute (no ~ expansion offline)")
    if any(char in location for char in "\r\n\0"):
        raise ValueError("invalid package installation location")
    location = _package_path(location.rstrip("/") or "/", absolute=True)
    sizes = _package_fields(resource("sizes", localized=True).data, numeric=True)
    if any(not re.fullmatch(r"[0-9]+", sizes.get(key, ""))
           for key in ("numfiles", "installedsize", "compressedsize")):
        raise ValueError("invalid package sizes")
    payload = _package_payload(resource("tar.Z").data, big=info.get("longfilenames", "NO").upper() == "YES")
    _validate_package_bom(resource("bom").data, payload)
    if int(sizes["numfiles"]) != len(payload):
        raise ValueError("package NumFiles differs from BOM/archive inventory")

    receipt = receipt_root + "/" + name + ".pkg"
    planned: dict[str, _PackageFile] = {}
    now = int(time.time())

    def destination(path: str) -> str:
        return path if path.startswith("/") else location if path == "." else location.rstrip("/") + "/" + path

    for path, item in payload.items():
        target = destination(path)
        if any(target == root or target.startswith(root + "/")
               for root in ("/NextLibrary/Receipts", receipt_root)):
            raise ValueError(f"package payload overlaps receipt storage: {path}")
        if target in planned:
            raise ValueError(f"package paths resolve to the same destination: {target}")
        entry = replace(item.entry, name=target, atime=now)
        # installer_tar recreates symlinks without applying their archived
        # metadata. Model a privileged system install (root:wheel, umask 022).
        if stat.S_ISLNK(entry.mode):
            entry = replace(entry, mode=stat.S_IFLNK | 0o777, uid=0, gid=0, mtime=now)
        planned[target] = replace(item, entry=entry,
                                  link=destination(item.link) if item.hard_link else item.link)
    payload_targets = set(planned)

    # Installer's receipt whitelist: copy metadata, localized resources and
    # delete hooks, but not the compressed archive or unrelated bundle files.
    suffixes = (".info", ".sizes", ".tiff", ".bom", ".pre_delete", ".post_delete", ".lproj")
    receipt_files = {path: item for path, item in files.items() if not path.startswith(".") and
                     (path.split("/", 1)[0].endswith(suffixes) or
                      path.split("/", 1)[0].startswith("software_version"))}
    generated = media.Entry(0, stat.S_IFREG | 0o644, 0, 0, 0, now, now, ".")
    receipt_files[name + ".location"] = _PackageFile(generated, (location + "\n").encode("utf-8"))
    receipt_files[name + ".status"] = _PackageFile(generated, b"installed\n")
    for path, item in receipt_files.items():
        target = receipt + "/" + path
        planned[target] = replace(item, entry=replace(item.entry, name=target))

    # Complete and validate the destination tree before editing even the copy.
    implicit = replace(generated, mode=stat.S_IFDIR | 0o755)
    for path in list(planned):
        parent = path.rpartition("/")[0]
        while parent:
            if parent in planned and not planned[parent].entry.is_dir:
                raise ValueError(f"package parent is not a directory: {parent}")
            planned.setdefault(parent, _PackageFile(replace(implicit, name=parent)))
            parent = parent.rpartition("/")[0]
    binary = media.executable(nextufs_binary)
    media._raw_ufs_info(target_ufs, binary)
    source = media.Image(binary, target_ufs)
    existing: dict[str, media.Entry] = {"/": source.inspect("/")[0]}
    ordered = sorted(planned, key=lambda path: (path.count("/"), path))
    for path in ordered:
        if path == "/":
            continue
        parent, _, basename = path.rpartition("/")
        if (parent or "/") in existing:
            found = source.child(parent or "/", basename)
            if found is not None:
                if (found.is_dir != planned[path].entry.is_dir or
                        stat.S_IFMT(found.mode) not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK)):
                    raise ValueError(f"incompatible existing file or symlink at {path}")
                existing[path] = found
    old_receipt = source.tree(receipt) if receipt in existing else []
    directory_metadata = {}
    for path, item in planned.items():
        if item.entry.is_dir:
            original = existing.get(path) if not (path == receipt or path.startswith(receipt + "/")) else None
            expected = original or item.entry
            if original is not None and path in payload_targets:
                expected = replace(original, mtime=item.entry.mtime)
            directory_metadata[path] = expected
    status_path = receipt + "/" + name + ".status"
    with media.new_output(output, source=target_ufs) as staged:
        media._directory_capacity(staged, [item.entry for item in planned.values()], binary)
        target = media.Image(binary, staged)
        for entry in reversed(old_receipt):
            path = receipt if entry.name == "." else receipt + "/" + entry.name
            target.mutate("rmdir" if entry.is_dir else "unlink", path)
        for path in ordered:
            item = planned[path]
            was_there = path in existing and not (path == receipt or path.startswith(receipt + "/"))
            if item.entry.is_dir:
                if not was_there:
                    target.mutate("mkdir", path)
            elif item.hard_link or path == status_path:
                continue
            elif item.link is not None:
                if was_there:
                    target.mutate("unlink", path)
                target.nextufs("mkfile", "--symlink", staged, item.link, path)
                target.metadata(path, item.entry)
            else:
                target.write(path, item.data, item.entry, exists=was_there)
        pending = {path: item for path, item in planned.items() if item.hard_link}
        while pending:
            ready = [path for path, item in pending.items() if item.link not in pending]
            if not ready:
                raise ValueError("cyclic package hard links")
            for path in ready:
                item = pending.pop(path)
                if path in existing:
                    target.mutate("unlink", path)
                target.nextufs("mkfile", "--link", staged, item.link, path)
        status = planned[status_path]
        target.write(status_path, status.data, status.entry, exists=False)
        # Existing payload directories retain ownership/mode/atime, but native
        # xpfT restores their BOM/archive mtime after writing their children.
        for path in reversed(ordered):
            if planned[path].entry.is_dir:
                target.metadata(path, directory_metadata[path])
        if "/" not in directory_metadata:
            target.metadata("/", existing["/"])
        for path in ordered:
            item = planned[path]
            actual = target.inspect(path)[0]
            if item.hard_link:
                if actual.inode != target.inspect(item.link)[0].inode:
                    raise ValueError(f"installed hard link differs: {path}")
            elif item.entry.is_dir:
                media._verify_metadata(directory_metadata[path], actual, path)
            else:
                media._verify_metadata(item.entry, actual, path)
                if item.link is None and target.read(path, actual) != item.data:
                    raise ValueError(f"installed package contents differ: {path}")
        media.check_image(staged, nextufs_binary=binary)
    return InstalledPackage(name, location, tuple(sorted(payload_targets)))
