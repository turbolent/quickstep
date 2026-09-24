"""Editable OPENSTEP 4.2 CD/USB recipe; generic media operations live in media.py."""
import argparse
from collections.abc import Sequence
from pathlib import Path
import subprocess
import sys
import tarfile

import media
import pkg
from media import PathInput

# Recipe choices: edit these and install_drivers() to customize the installation media.
VOLUME_ID = "OPENSTEP_4_2"
BOOT_DRIVERS = ("PS2Keyboard", "EISABus", "PCIBus", "Intel824X0", "EIDE")
PS2_DRIVERS = ("PS2Keyboard", "PS2Mouse")
SETUP_CATALOG = media.SetupCatalog(
    packages=(
        media.SetupPackage("OS42MachUserPatch4", "OPENSTEP Release 4.2 Patch 4",
                           restart_required=True, fix_pic_after=True),
        media.SetupPackage("DeveloperTools", "OPENSTEP 4.2 for Mach"),
        media.SetupPackage("DeveloperLibs", "OPENSTEP 4.2 for Mach", ("DeveloperTools",)),
        media.SetupPackage("OS42MachDeveloperPatch4", "OPENSTEP Release 4.2 Developer Patch 4",
                           ("OS42MachUserPatch4", "DeveloperTools", "DeveloperLibs"), restart_required=True),
        media.SetupPackage("DeveloperDoc", "OPENSTEP 4.2 for Mach"),
        media.SetupPackage("ProfileLibs", "OPENSTEP 4.2 for Mach"),
        media.SetupPackage("OS42MachProfileLibPatch4", "OPENSTEP Release 4.2 ProfileLib Patch 4",
                           ("OS42MachDeveloperPatch4", "ProfileLibs"), restart_required=True),
        media.SetupPackage("GNUSource", "OPENSTEP 4.2 for Mach", relocatable=True),
    ),
    choices=(
        media.SetupChoice("System Software", "User Patch 4", ("OS42MachUserPatch4",), default_selected=True),
        media.SetupChoice("Developer Software", "Developer Tools and Libraries", ("DeveloperTools", "DeveloperLibs")),
        media.SetupChoice("Developer Software", "Developer Tools and Libraries Patch 4", ("OS42MachDeveloperPatch4",)),
        media.SetupChoice("Developer Software", "Developer Documentation", ("DeveloperDoc",)),
        media.SetupChoice("Profiling", "Profiling Libraries", ("ProfileLibs",)),
        media.SetupChoice("Profiling", "Profiling Libraries Patch 4", ("OS42MachProfileLibPatch4",)),
        media.SetupChoice("Source Code", "GNU Source", ("GNUSource",)),
    ),
)


def install_drivers(image: PathInput, driver_disk: PathInput, beta_disk: PathInput | None,
                    stage: Path, *, nextufs_binary: PathInput | None = None,
                    installation_driver_bundles: Sequence[PathInput] = (),
                    use_bus_master_ide: bool = False, remove_ps2: bool = False) -> None:
    """Prepare the boot drivers through the root media.py library only."""
    bundles: dict[str, Path] = {}
    for bundle in map(Path, installation_driver_bundles):
        if not bundle.is_dir() or not bundle.name.endswith(".config"):
            raise ValueError(f"installation driver must be a .config directory: {bundle}")
        name = media.driver_name(bundle.name)
        if name in bundles or name == "System":
            raise ValueError(f"duplicate or reserved installation driver: {name}")
        if remove_ps2 and name in PS2_DRIVERS:
            raise ValueError(f"installation driver {name} conflicts with --remove-ps2")
        bundles[name] = bundle
    if use_bus_master_ide and "BusMasterIDE" not in bundles:
        raise ValueError("BusMasterIDE replacement bundle is missing")
    if use_bus_master_ide and "EIDE" in bundles:
        raise ValueError("an EIDE installation driver conflicts with --bus-master-ide")
    target = media.DriverImage(image, nextufs=nextufs_binary)
    boot_drivers = tuple("BusMasterIDE" if name == "EIDE" and use_bus_master_ide else name
                         for name in BOOT_DRIVERS if not (remove_ps2 and name in PS2_DRIVERS))
    if remove_ps2:
        existing = target.list_drivers()
        for name in PS2_DRIVERS:
            print(f"Removing {name} from the boot floppy...", flush=True)
            target.deactivate(name)
            if name in existing:
                target.remove(name)
    if use_bus_master_ide:
        print("Using packaged BusMasterIDE instead of EIDE...", flush=True)
        print("Deactivating EIDE...", flush=True)
        target.deactivate("EIDE")
        print("Checking for an existing EIDE bundle...", flush=True)
        if "EIDE" in target.list_drivers():
            print("Removing the existing EIDE bundle...", flush=True)
            target.remove("EIDE")
    elif "EIDE" in bundles:
        print("Using packaged EIDE...", flush=True)
    else:
        print(f"Using EIDE with the PIIX configuration from {beta_disk}...", flush=True)
    extra_drivers = tuple(name for name in bundles if name not in boot_drivers)
    for name in boot_drivers + extra_drivers:
        if name in bundles:
            local = bundles[name]
            print(f"Replacing any existing {name} bundle...", flush=True)
            target.deactivate(name)
            if name in target.list_drivers():
                target.remove(name)
        else:
            source_disk = beta_disk if name == "EIDE" else driver_disk
            if source_disk is None:
                raise ValueError("beta_disk is required when using EIDE")
            source = media.DriverImage(source_disk, nextufs=nextufs_binary)
            local = stage / (name + ".config")
            print(f"Extracting {name} from {source_disk}...", flush=True)
            source.extract(name, local)
        print(f"Stripping debug symbols and storing {name} on the boot floppy...", flush=True)
        target.store(name, local, strip_debug=True)
        print(f"Configuring {name}...", flush=True)
        if name == "EIDE" and name not in bundles:
            target.configure(name, source_table="EIDE_PIIX.table", overwrite=True)
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
    for name in extra_drivers:
        print(f"Activating {name} according to its instance table...", flush=True)
        target.activate(name)
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
            installation_driver_bundles: Sequence[PathInput] = (),
            use_bus_master_ide: bool = False, remove_languages: bool = False,
            remove_ps2: bool = False,
            kernel_source: PathInput | None = None) -> None:
    """Make a grown, driver-equipped copy; publish only after every step succeeds."""
    print(f"Copying boot floppy from {boot_disk}...", flush=True)
    with media.new_output(output, source=boot_disk) as image:
        print(f"Growing boot-floppy UFS to {media.MAX_GROWN_FLOPPY_KIB} KiB...", flush=True)
        media.grow_image(image, media.MAX_GROWN_FLOPPY_KIB, nextufs_binary=nextufs_binary)
        print("Disabling boot-floppy language selection...", flush=True)
        media.skip_boot_language_selection(image, nextufs_binary=nextufs_binary)
        if remove_languages:
            print("Removing non-English boot-floppy translations...", flush=True)
            media.remove_boot_languages(image, nextufs_binary=nextufs_binary)
        if kernel_source is not None:
            print("Copying Patch 4 Intel kernel to the boot floppy...", flush=True)
            with_kernel = image.parent / "boot-kernel.img"
            media.copy_kernel(kernel_source, image, with_kernel, nextufs_binary=nextufs_binary)
            with_kernel.replace(image)
        install_drivers(image, driver_disk, beta_disk, image.parent, nextufs_binary=nextufs_binary,
                        installation_driver_bundles=installation_driver_bundles,
                        use_bus_master_ide=use_bus_master_ide, remove_ps2=remove_ps2)
        print("Checking boot-floppy filesystem (fsck)...", flush=True)
        media.check_image(image, nextufs_binary=nextufs_binary)
        print(f"Padding boot floppy to {media.MAX_BOOT_FLOPPY_KIB} KiB...", flush=True)
        media.pad_image(image, media.MAX_BOOT_FLOPPY_KIB)


def build(boot_disk: PathInput, driver_disk: PathInput, beta_disk: PathInput | None,
          user_cd: PathInput, output: PathInput, *, iso_tool: PathInput | None = None,
          nextufs_binary: PathInput | None = None, bus_master_ide: PathInput | None = None,
          fix_pic_bug: bool = False, developer_cd: PathInput | None = None,
          user_patch: PathInput | None = None, developer_patch: PathInput | None = None,
          remove_languages: bool = False, remove_ps2: bool = False, setup_app: PathInput | None = None,
          profile_libs_patch: PathInput | None = None,
          driver_packages: Sequence[PathInput] = (), framebuffer_wc: PathInput | None = None,
          installation_drivers: Sequence[PathInput] = (), usb: bool = False) -> None:
    """Build and check installation media; publish only after all checks pass."""
    if usb and iso_tool is not None:
        raise ValueError("--iso-tool cannot be used with --usb")
    if beta_disk is None and bus_master_ide is None:
        raise ValueError("beta_disk is required unless bus_master_ide is provided")
    print("Checking input paths and build tools...", flush=True)
    for label, path in (("boot disk", boot_disk), ("driver disk", driver_disk),
                        ("beta disk", beta_disk), ("User CD", user_cd),
                        ("Developer CD", developer_cd), ("User patch", user_patch),
                        ("Developer patch", developer_patch), ("Profiling libraries patch", profile_libs_patch)):
        if path is not None and not Path(path).is_file():
            raise ValueError(f"{label} must be a regular file: {path}")
    required_drivers = ([bus_master_ide] if bus_master_ide is not None else []) + list(installation_drivers)
    for path in required_drivers:
        package_path = Path(path)
        if not (package_path.is_file() or (package_path.is_dir() and package_path.name.endswith(".pkg"))):
            raise ValueError(f"installation driver must be a .pkg directory or package archive: {path}")
    if setup_app is not None:
        media.validate_setup_app(setup_app)
        media.setup_plist(SETUP_CATALOG, fix_pic_bug=fix_pic_bug)
        if framebuffer_wc is not None and not (Path(setup_app) / "setup-framebuffer-wc.sh").is_file():
            raise ValueError("--framebuffer-wc requires a rebuilt Setup.app containing setup-framebuffer-wc.sh")
    nextufs_binary = media.executable(nextufs_binary)
    if not usb:
        iso_tool = media.resolve_iso_tool(iso_tool, nextufs_binary=nextufs_binary)
    patches = ((user_patch, "OS42MachUserPatch4.pkg"),
               (developer_patch, "OS42MachDeveloperPatch4.pkg"),
               (profile_libs_patch, "OS42MachProfileLibPatch4.pkg"))
    for archive, package in patches:
        if archive is not None:
            print(f"Checking patch archive {archive}...", flush=True)
            entries = media.tar_entries(archive)
            if (not entries or {entry.name.split("/", 1)[0] for entry in entries} != {package} or
                    not any(entry.name == package and entry.is_dir for entry in entries)):
                raise ValueError(f"patch archive must contain only the directory {package}: {archive}")
    with media.new_output(output) as iso:
        catalog = SETUP_CATALOG
        driver_archives: list[Path] = []
        optional_drivers = [(path, False) for path in driver_packages]
        if framebuffer_wc is not None:
            optional_drivers.append((framebuffer_wc, True))
        for index, (path, framebuffer) in enumerate(optional_drivers):
            print(f"Checking driver package {path}...", flush=True)
            archive = iso.parent / f"driver-package-{index}.tar"
            info = pkg.prepare_package(path, archive)
            if framebuffer and (info.name != "FramebufferWC" or info.relocatable):
                raise ValueError("--framebuffer-wc requires the non-relocatable FramebufferWC package")
            if any(package.name == info.name for package in catalog.packages):
                raise ValueError(f"duplicate Setup package name: {info.name}")
            catalog = media.SetupCatalog(
                catalog.packages + (media.SetupPackage(
                    info.name, info.version, dependencies=("OS42MachUserPatch4",) if framebuffer else (),
                    relocatable=info.relocatable, restart_required=True,
                    post_install=("/bin/sh", "Setup.app/setup-framebuffer-wc.sh")
                    if framebuffer else ()),),
                catalog.choices + (media.SetupChoice("Drivers", info.title, (info.name,)),))
            driver_archives.append(archive)
        media.setup_plist(catalog, fix_pic_bug=fix_pic_bug)
        boot = iso.parent / "boot.img"
        ufs = iso.parent / "user.ufs"
        installer = iso.parent / "installer.ufs"
        installed_drivers: list[pkg.InstalledDriver] = []
        driver_bundles: list[Path] = []
        print(f"Extracting User CD filesystem from {user_cd}...", flush=True)
        media.extract_ufs(user_cd, ufs, nextufs_binary=nextufs_binary)
        kernel_source: Path | None = None
        prepared_patch: pkg.PreparedUserPatch | None = None
        package_hook = b""
        if user_patch is not None:
            print("Applying User Patch 4 to the CD filesystem...", flush=True)
            patched_user = iso.parent / "user-patch4.ufs"
            prepared_patch = pkg.prepare_user_patch(user_patch, ufs, patched_user, nextufs_binary=nextufs_binary)
            ufs = kernel_source = patched_user
            package_hook = pkg.user_patch_installation_hook(prepared_patch)
        for index, path in enumerate(required_drivers):
            packaged = iso.parent / f"installation-driver-{index}.ufs"
            print(f"Installing driver package from {path} on the CD...", flush=True)
            installed = pkg.install_driver_package(path, ufs, packaged, nextufs_binary=nextufs_binary)
            if index == 0 and bus_master_ide is not None and installed.name != "BusMasterIDE":
                raise ValueError("--bus-master-ide requires a package containing BusMasterIDE.config")
            if any(item.name == installed.name or item.package.name == installed.package.name
                   for item in installed_drivers):
                raise ValueError(f"duplicate installation driver or package: {installed.name}")
            if bus_master_ide is not None and installed.name == "EIDE":
                raise ValueError("an EIDE installation driver conflicts with --bus-master-ide")
            if remove_ps2 and installed.name in PS2_DRIVERS:
                raise ValueError(f"installation driver {installed.name} conflicts with --remove-ps2")
            bundle = iso.parent / (installed.name + ".config")
            print(f"Extracting {installed.name} for the boot floppy...", flush=True)
            media.DriverImage(packaged, nextufs=nextufs_binary, driver_root=installed.package.location).extract(
                installed.name, bundle)
            installed_drivers.append(installed)
            driver_bundles.append(bundle)
            ufs = packaged
        package_hook += pkg.installation_hook(item.package for item in installed_drivers)
        packaged_drivers = tuple(item.name for item in installed_drivers)
        removed_drivers = PS2_DRIVERS if remove_ps2 else ()
        print(f"Preparing boot floppy and drivers from {boot_disk}...", flush=True)
        drivers(boot_disk, driver_disk, beta_disk, boot, nextufs_binary=nextufs_binary,
                installation_driver_bundles=driver_bundles, use_bus_master_ide=bus_master_ide is not None,
                remove_languages=remove_languages, remove_ps2=remove_ps2, kernel_source=kernel_source)
        if remove_languages:
            print("Removing optional non-English language packages and receipts...", flush=True)
            pruned = iso.parent / "english.ufs"
            media.remove_language_packages(ufs, pruned, nextufs_binary=nextufs_binary)
            ufs = pruned
        print("Fixing BuildDisk's 4 GiB capacity calculation...", flush=True)
        with_builddisk = iso.parent / "builddisk.ufs"
        media.fix_builddisk_capacity(ufs, with_builddisk, nextufs_binary=nextufs_binary)
        ufs = with_builddisk
        if fix_pic_bug:
            print("Applying kernel PIC fix if needed...", flush=True)
            patched = iso.parent / "boot-picfix.img"
            media.patch_kernel_pic_bug(boot, patched, nextufs_binary=nextufs_binary)
            boot = patched
        print("Preparing installed-system drivers and installer hook...", flush=True)
        if fix_pic_bug:
            print("Preparing PIC-patched kernel for the installed system...", flush=True)
        media.prepare_installation_drivers(boot, ufs, installer, nextufs_binary=nextufs_binary,
                                           fix_pic_bug=fix_pic_bug, package_hook=package_hook,
                                           packaged_drivers=packaged_drivers, removed_drivers=removed_drivers)
        if developer_cd is not None:
            print(f"Adding Developer CD packages from {developer_cd}...", flush=True)
            combined = iso.parent / "combined.ufs"
            media.copy_directory(developer_cd, "/NextCD/Packages", installer, "/NextCD/Packages",
                                 combined, nextufs_binary=nextufs_binary)
            installer = combined
        for archive, package in patches:
            if archive is not None:
                purpose = "for reuse on other systems" if archive == user_patch else "for manual installation"
                print(f"Adding {package} {purpose}...", flush=True)
                patched_ufs = iso.parent / (package + ".ufs")
                media.copy_tar(archive, installer, "/NextCD/Packages", patched_ufs,
                               nextufs_binary=nextufs_binary)
                installer = patched_ufs
        for index, archive in enumerate(driver_archives):
            print(f"Adding driver package {optional_drivers[index][0]} for manual installation...", flush=True)
            with_driver = iso.parent / f"driver-package-{index}.ufs"
            media.copy_tar(archive, installer, "/NextCD/Packages", with_driver,
                           nextufs_binary=nextufs_binary)
            installer = with_driver
        if setup_app is not None:
            print("Adding Setup.app for post-installation package setup...", flush=True)
            with_setup = iso.parent / "setup.ufs"
            media.prepare_setup_app(setup_app, installer, with_setup, catalog=catalog, fix_pic_bug=fix_pic_bug,
                                    nextufs_binary=nextufs_binary)
            installer = with_setup
        print("Preparing destination disk limits and checked selection...", flush=True)
        disk_installer = iso.parent / "disk-installer.ufs"
        media.prepare_installer_disks(installer, disk_installer, usb=usb, nextufs_binary=nextufs_binary)
        installer = disk_installer
        print(f"Building {'USB image' if usb else 'ISO'}: {output}...", flush=True)
        if usb:
            media.create_usb(boot, installer, iso, nextufs_binary=nextufs_binary)
            print("Verifying USB boot files and installer...", flush=True)
            media.verify_boot_usb(boot_disk, boot, user_cd, installer, iso,
                                  nextufs_binary=nextufs_binary, fix_pic_bug=fix_pic_bug,
                                  package_hook=package_hook, packaged_drivers=packaged_drivers,
                                  kernel_source=kernel_source, removed_drivers=removed_drivers)
            # verify_boot_usb checks partition b against this UFS, allowing only
            # the superblock conversion to the USB label's logical block size.
            # nextufs's default whole-disk view selects the boot partition a.
            contents = installer
        else:
            media.create_iso(boot, user_cd, installer, iso, volume_id=VOLUME_ID, iso_tool=iso_tool,
                             nextufs_binary=nextufs_binary)
            print("Verifying boot image and ISO...", flush=True)
            media.verify_boot_cd(boot_disk, boot, user_cd, installer, iso, nextufs_binary=nextufs_binary,
                                 installation_drivers=True, fix_pic_bug=fix_pic_bug,
                                 package_hook=package_hook, packaged_drivers=packaged_drivers,
                                 kernel_source=kernel_source, removed_drivers=removed_drivers, disk_limits=True)
            contents = iso
        if prepared_patch is not None:
            assert kernel_source is not None
            media.verify_directory_copy(kernel_source, prepared_patch.receipt_source, contents,
                                        prepared_patch.receipt_source, nextufs_binary=nextufs_binary)
        if developer_cd is not None:
            media.verify_directory_copy(developer_cd, "/NextCD/Packages", contents, "/NextCD/Packages",
                                        nextufs_binary=nextufs_binary)
        for archive, _ in patches:
            if archive is not None:
                media.verify_tar_copy(archive, contents, "/NextCD/Packages", nextufs_binary=nextufs_binary)
        for archive in driver_archives:
            media.verify_tar_copy(archive, contents, "/NextCD/Packages", nextufs_binary=nextufs_binary)
        if setup_app is not None:
            media.verify_setup_app(setup_app, contents, catalog=catalog, fix_pic_bug=fix_pic_bug,
                                   nextufs_binary=nextufs_binary)
    print(f"Created bootable {'USB image' if usb else 'ISO'}: {output}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, description in (
        ("boot_disk", "boot floppy"),
        ("driver_disk", "driver floppy"),
        ("user_cd", "User CD image"),
        ("output", "new ISO or USB disk image to create"),
    ):
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True, help=description)
    parser.add_argument("--iso-tool", help="mkisofs, genisoimage, or xorriso executable")
    parser.add_argument("--usb", action="store_true",
                        help="create a raw BIOS-bootable USB disk image instead of a CD ISO")
    parser.add_argument("--nextufs", help="nextufs executable (default: automatic discovery)")
    parser.add_argument("--developer-cd", type=Path,
                        help="Developer CD image whose packages will be included for manual installation")
    parser.add_argument("--user-patch", type=Path,
                        help="apply OS42MachUserPatch4.tar to the boot kernel, CD and installed system; also retain its package")
    parser.add_argument("--developer-patch", type=Path, help="OS42MachDevPatch4.tar to include for manual installation")
    parser.add_argument("--profile-libs-patch", type=Path,
                        help="OS42MachPLibPatch4.tar to include for manual installation")
    parser.add_argument("--setup-app", type=Path, metavar="Setup.app",
                        help="native Setup.app release bundle to include for post-installation package setup")
    parser.add_argument("--optional-driver-package", type=Path, action="append", default=[], metavar="PACKAGE",
                        help=".pkg directory or single-package tar to include in Setup (repeatable)")
    parser.add_argument("--framebuffer-wc", type=Path, metavar="PACKAGE",
                        help="FramebufferWC package to include in Setup; requires User Patch 4 and patches VBE after installation")
    parser.add_argument("--fix-pic-bug", action="store_true",
                        help="apply the PIC interrupt fix to the boot and installed kernels")
    parser.add_argument("--remove-languages", action="store_true",
                        help="omit non-English boot translations and French, German, Italian, Spanish and Swedish Essentials packages and receipts")
    parser.add_argument("--remove-ps2", action="store_true",
                        help="remove PS/2 keyboard and mouse drivers from the boot image and installed system")
    parser.add_argument("--beta-disk", type=Path,
                        help="beta-driver floppy (required unless --bus-master-ide is provided)")
    parser.add_argument("--bus-master-ide", type=Path, metavar="PACKAGE",
                        help="BusMasterIDE .pkg directory or package archive to install instead of EIDE")
    parser.add_argument("--installation-driver", type=Path, action="append", default=[], metavar="PACKAGE",
                        help="driver .pkg directory or package archive to install on the boot floppy, CD and startup disk (repeatable)")
    args = parser.parse_args(argv)
    if args.beta_disk is None and args.bus_master_ide is None:
        parser.error("--beta-disk is required unless --bus-master-ide is provided")
    try:
        build(boot_disk=args.boot_disk, driver_disk=args.driver_disk, beta_disk=args.beta_disk,
              user_cd=args.user_cd, output=args.output, iso_tool=args.iso_tool, nextufs_binary=args.nextufs,
              bus_master_ide=args.bus_master_ide, fix_pic_bug=args.fix_pic_bug, developer_cd=args.developer_cd,
              user_patch=args.user_patch, developer_patch=args.developer_patch,
              remove_languages=args.remove_languages, remove_ps2=args.remove_ps2, setup_app=args.setup_app,
              profile_libs_patch=args.profile_libs_patch, driver_packages=args.optional_driver_package,
              framebuffer_wc=args.framebuffer_wc, installation_drivers=args.installation_driver,
              usb=args.usb)
    except (media.MediaError, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"build.py: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
