"""Editable OPENSTEP 4.2 CD recipe; generic media operations live in media.py."""
import argparse
from collections.abc import Sequence
from pathlib import Path
import subprocess
import sys
import tarfile

import media
from media import PathInput

# Recipe choices: edit these and install_drivers() to build a different CD.
VOLUME_ID = "OPENSTEP_4_2"
BOOT_DRIVERS = ("PS2Keyboard", "EISABus", "PCIBus", "Intel824X0", "EIDE")


def install_drivers(image: PathInput, driver_disk: PathInput, beta_disk: PathInput | None,
                    stage: Path, *, nextufs_binary: PathInput | None = None,
                    bus_master_ide: PathInput | None = None) -> None:
    """Prepare the boot drivers through the root media.py library only."""
    if bus_master_ide is not None and not Path(bus_master_ide).is_dir():
        raise NotADirectoryError(f"BusMasterIDE bundle must be a directory: {bus_master_ide}")
    target = media.DriverImage(image, nextufs=nextufs_binary)
    boot_drivers = tuple("BusMasterIDE" if name == "EIDE" and bus_master_ide is not None else name
                         for name in BOOT_DRIVERS)
    if bus_master_ide is not None:
        print(f"Using BusMasterIDE from {bus_master_ide} as a boot driver instead of EIDE...", flush=True)
        print("Deactivating EIDE...", flush=True)
        target.deactivate("EIDE")
        print("Checking for an existing EIDE bundle...", flush=True)
        if "EIDE" in target.list_drivers():
            print("Removing the existing EIDE bundle...", flush=True)
            target.remove("EIDE")
    else:
        print(f"Using EIDE with the PIIX configuration from {beta_disk}...", flush=True)
    for name in boot_drivers:
        if name == "BusMasterIDE" and bus_master_ide is not None:
            local = Path(bus_master_ide)
        else:
            source_disk = beta_disk if name == "EIDE" else driver_disk
            if source_disk is None:
                raise ValueError("beta_disk is required when using EIDE")
            source = media.DriverImage(source_disk, nextufs=nextufs_binary)
            local = stage / (name + ".config")
            print(f"Extracting {name} from {source_disk}...", flush=True)
            source.extract(name, local)
        print(f"Storing {name} on the boot floppy...", flush=True)
        target.store(name, local)
        print(f"Configuring {name}...", flush=True)
        if name == "EIDE":
            target.configure(name, source_table="EIDE_PIIX.table", overwrite=True)
        elif name == "BusMasterIDE":
            target.configure(name, settings={"Boot Driver": "Yes"})
        else:
            target.configure(name)
    # The stock instance lists these drivers, but this boot image does not use them.
    for name in ("PCMCIABus", "PCIC"):
        print(f"Deactivating {name}...", flush=True)
        target.deactivate(name)
    for index, name in enumerate(boot_drivers):
        # Reproduce the tested load order, not a hardware dependency graph.
        print(f"Activating {name}...", flush=True)
        target.activate(name, dependencies=(boot_drivers[index - 1],) if index else ())
    print("Configuring boot-floppy System settings...", flush=True)
    target.configure("System", settings={
        "Ask For Drivers": "No",
        "Language": "English",
        "Prompt For Driver Disk": "No",
        "Driver Disk Prompts": "0",
        "Installation Driver Families": "Disk",
    })


def drivers(boot_disk: PathInput, driver_disk: PathInput, beta_disk: PathInput | None,
            output: PathInput, *, nextufs_binary: PathInput | None = None,
            bus_master_ide: PathInput | None = None) -> None:
    """Make a grown, driver-equipped copy; publish only after every step succeeds."""
    print(f"Copying boot floppy from {boot_disk}...", flush=True)
    with media.new_output(output, source=boot_disk) as image:
        print(f"Growing boot-floppy UFS to {media.MAX_GROWN_FLOPPY_KIB} KiB...", flush=True)
        media.grow_image(image, media.MAX_GROWN_FLOPPY_KIB, nextufs_binary=nextufs_binary)
        install_drivers(image, driver_disk, beta_disk, image.parent, nextufs_binary=nextufs_binary,
                        bus_master_ide=bus_master_ide)
        print("Disabling boot-floppy language selection...", flush=True)
        media.skip_boot_language_selection(image, nextufs_binary=nextufs_binary)
        print("Checking boot-floppy filesystem (fsck)...", flush=True)
        media.check_image(image, nextufs_binary=nextufs_binary)
        print(f"Padding boot floppy to {media.MAX_BOOT_FLOPPY_KIB} KiB...", flush=True)
        media.pad_image(image, media.MAX_BOOT_FLOPPY_KIB)


def build(boot_disk: PathInput, driver_disk: PathInput, beta_disk: PathInput | None,
          user_cd: PathInput, output: PathInput, *, iso_tool: PathInput | None = None,
          nextufs_binary: PathInput | None = None, bus_master_ide: PathInput | None = None,
          fix_pic_bug: bool = False, developer_cd: PathInput | None = None,
          user_patch: PathInput | None = None, developer_patch: PathInput | None = None,
          remove_language_packages: bool = False) -> None:
    """Build and check the complete ISO; keep intermediates only until publication."""
    if beta_disk is None and bus_master_ide is None:
        raise ValueError("beta_disk is required unless bus_master_ide is provided")
    print("Checking input paths and build tools...", flush=True)
    for label, path in (("boot disk", boot_disk), ("driver disk", driver_disk),
                        ("beta disk", beta_disk), ("User CD", user_cd),
                        ("Developer CD", developer_cd), ("User patch", user_patch),
                        ("Developer patch", developer_patch)):
        if path is not None and not Path(path).is_file():
            raise ValueError(f"{label} must be a regular file: {path}")
    if bus_master_ide is not None and not Path(bus_master_ide).is_dir():
        raise NotADirectoryError(f"BusMasterIDE bundle must be a directory: {bus_master_ide}")
    nextufs_binary = media.executable(nextufs_binary)
    iso_tool = media.resolve_iso_tool(iso_tool, nextufs_binary=nextufs_binary)
    patches = ((user_patch, "OS42MachUserPatch4.pkg"),
               (developer_patch, "OS42MachDeveloperPatch4.pkg"))
    for archive, package in patches:
        if archive is not None:
            print(f"Checking patch archive {archive}...", flush=True)
            entries = media.tar_entries(archive)
            if (not entries or {entry.name.split("/", 1)[0] for entry in entries} != {package} or
                    not any(entry.name == package and entry.is_dir for entry in entries)):
                raise ValueError(f"patch archive must contain only the directory {package}: {archive}")
    with media.new_output(output) as iso:
        boot = iso.parent / "boot.img"
        ufs = iso.parent / "user.ufs"
        installer = iso.parent / "installer.ufs"
        print(f"Preparing boot floppy and drivers from {boot_disk}...", flush=True)
        drivers(boot_disk, driver_disk, beta_disk, boot, nextufs_binary=nextufs_binary,
                bus_master_ide=bus_master_ide)
        print(f"Extracting User CD filesystem from {user_cd}...", flush=True)
        media.extract_ufs(user_cd, ufs, nextufs_binary=nextufs_binary)
        if remove_language_packages:
            print("Removing optional non-English language packages and receipts...", flush=True)
            pruned = iso.parent / "english.ufs"
            media.remove_language_packages(ufs, pruned, nextufs_binary=nextufs_binary)
            ufs = pruned
        if fix_pic_bug:
            print("Applying kernel PIC fix if needed...", flush=True)
            patched = iso.parent / "boot-picfix.img"
            media.patch_kernel_pic_bug(boot, patched, nextufs_binary=nextufs_binary)
            boot = patched
        print("Preparing installed-system drivers and installer hook...", flush=True)
        if fix_pic_bug:
            print("Preparing PIC-patched kernel for the installed system...", flush=True)
        media.prepare_installation_drivers(boot, ufs, installer, nextufs_binary=nextufs_binary,
                                           fix_pic_bug=fix_pic_bug)
        if developer_cd is not None:
            print(f"Adding Developer CD packages from {developer_cd}...", flush=True)
            combined = iso.parent / "combined.ufs"
            media.copy_directory(developer_cd, "/NextCD/Packages", installer, "/NextCD/Packages",
                                 combined, nextufs_binary=nextufs_binary)
            installer = combined
        for archive, package in patches:
            if archive is not None:
                print(f"Adding {package} for manual installation...", flush=True)
                patched_ufs = iso.parent / (package + ".ufs")
                media.copy_tar(archive, installer, "/NextCD/Packages", patched_ufs,
                               nextufs_binary=nextufs_binary)
                installer = patched_ufs
        print(f"Building ISO: {output}...", flush=True)
        media.create_iso(boot, user_cd, installer, iso, volume_id=VOLUME_ID, iso_tool=iso_tool,
                         nextufs_binary=nextufs_binary)
        print("Verifying boot image and ISO...", flush=True)
        media.verify_boot_cd(boot_disk, boot, user_cd, installer, iso, nextufs_binary=nextufs_binary,
                             installation_drivers=True, fix_pic_bug=fix_pic_bug)
        if developer_cd is not None:
            media.verify_directory_copy(developer_cd, "/NextCD/Packages", iso, "/NextCD/Packages",
                                        nextufs_binary=nextufs_binary)
        for archive, _ in patches:
            if archive is not None:
                media.verify_tar_copy(archive, iso, "/NextCD/Packages", nextufs_binary=nextufs_binary)
    print(f"Created bootable ISO: {output}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, description in (
        ("boot_disk", "boot floppy"),
        ("driver_disk", "driver floppy"),
        ("user_cd", "User CD image"),
        ("output", "new bootable ISO to create"),
    ):
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True, help=description)
    parser.add_argument("--iso-tool", help="mkisofs, genisoimage, or xorriso executable")
    parser.add_argument("--nextufs", help="nextufs executable (default: automatic discovery)")
    parser.add_argument("--developer-cd", type=Path,
                        help="Developer CD image whose packages will be included for manual installation")
    parser.add_argument("--user-patch", type=Path, help="OS42MachUserPatch4.tar to include for manual installation")
    parser.add_argument("--developer-patch", type=Path, help="OS42MachDevPatch4.tar to include for manual installation")
    parser.add_argument("--fix-pic-bug", action="store_true",
                        help="apply the PIC interrupt fix to the boot and installed kernels")
    parser.add_argument("--remove-language-packages", action="store_true",
                        help="omit French, German, Italian, Spanish and Swedish Essentials packages and receipts")
    parser.add_argument("--beta-disk", type=Path,
                        help="beta-driver floppy (required unless --bus-master-ide is provided)")
    parser.add_argument("--bus-master-ide", type=Path, metavar="BusMasterIDE.config",
                        help="local driver bundle to use as a boot driver instead of EIDE")
    args = parser.parse_args(argv)
    if args.beta_disk is None and args.bus_master_ide is None:
        parser.error("--beta-disk is required unless --bus-master-ide is provided")
    try:
        build(boot_disk=args.boot_disk, driver_disk=args.driver_disk, beta_disk=args.beta_disk,
              user_cd=args.user_cd, output=args.output, iso_tool=args.iso_tool, nextufs_binary=args.nextufs,
              bus_master_ide=args.bus_master_ide, fix_pic_bug=args.fix_pic_bug, developer_cd=args.developer_cd,
              user_patch=args.user_patch, developer_patch=args.developer_patch,
              remove_language_packages=args.remove_language_packages)
    except (media.MediaError, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"build_cd.py: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
