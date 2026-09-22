# OPENSTEP 4.2 bootable CD builder

Build an El Torito bootable OPENSTEP 4.2 Intel CD from the original installation media.

## Requirements

- Python 3.11 or newer; no pip packages are needed.
- [nextufs-offline](https://github.com/turbolent/nextufs-offline).
- One ISO tool on `PATH`: `mkisofs`, `genisoimage`, or `xorriso`.
- OPENSTEP 4.2 installation media:
  - Boot floppy
  - Driver floppy
  - User CD
- IDE driver: [BusMasterIDE](https://github.com/turbolent/BusMasterIDE); or OPENSTEP 4.2 Beta driver floppy

### Optional

- OPENSTEP 4.2 installation media:
  - Developer CD
  - User Patch 4
  - Developer Patch 4


## Build the ISO

Run from the workspace root, substituting the paths to your images:

```sh
python3 build_cd.py \
  --boot-disk "4.2_Install_Disk.img" \
  --driver-disk "4.2_Driver_Disk.img" \
  --beta-disk "4.2_Beta_Drivers_1.img" \
  --user-cd "Openstep-4.2-Intel-User.iso" \
  --output openstep.iso
```

### Optional flags

- **Recommended** `--bus-master-ide /path/to/BusMasterIDE.pkg`: install BusMasterIDE instead of EIDE; `--beta-disk` is then unnecessary.
  Accepts a `.pkg` directory or a single-package tar archive, not an extracted `.config` bundle.
- `--installation-driver /path/to/Driver.pkg`: install a driver on the boot floppy, CD filesystem, and startup disk automatically.
  Repeat the flag for additional drivers; single-package tar archives are also accepted.
  Each package must be script-free and install one `.config` bundle directly in `/private/Devices`.
  Driver activation follows `Instance0.table` (created from `Default.table` if missing), including its `Boot Driver` setting.
  Added drivers retain their command-line order within each activation list.
  The installed system receives each complete package and receipt, plus the configured instance from the boot floppy.
  This also applies to `--bus-master-ide`; its only special behavior is replacing EIDE.
  These drivers are installed during system installation, not offered as optional Setup choices, and must fit on the boot floppy.
  Debug symbols are stripped automatically from boot-floppy driver binaries;
  the original packages and the payloads installed on the CD and startup disk remain unchanged.
- `--developer-cd /path/to/Developer.iso`: include the Developer CD packages.
- `--user-patch /path/to/OS42MachUserPatch4.tar`: apply User Patch 4 to the CD filesystem and boot-floppy kernel, and install it automatically on the startup disk.
  The startup disk receives the complete patch, its receipt, and the VBE-enabled bootloader.
  The CD's existing bootloader is retained; the original package is also included for use on other systems.
- `--developer-patch /path/to/OS42MachDevPatch4.tar`: include Developer Patch 4.
- `--profile-libs-patch /path/to/OS42MachPLibPatch4.tar`: include Profiling Libraries Patch 4.
- `--setup-app /path/to/Setup.app`: include the native post-installation package selector (see [building Setup.app](setup/README.md)).
- `--optional-driver-package /path/to/Driver.pkg`: copy an optional driver package to `/NextCD/Packages` and list it under **Drivers** when Setup.app is included.
  Repeat the flag for additional packages; single-package tar archives are also accepted.
  These choices are unchecked by default and do not change the boot-floppy drivers.
- `--framebuffer-wc ../FramebufferWC/FramebufferWC-0.27.pkg.tar.gz`: include FramebufferWC as an optional **Drivers** choice in Setup.
  Use with `--setup-app` and `--user-patch` (or an already-installed User Patch 4).
  Selecting it installs User Patch 4 first if needed, then FramebufferWC, patches VBE, and activates both drivers in load order.
  Missing driver instances are copied from their default tables; existing instance settings are preserved.
  Reboot afterward to use the drivers.
- `--remove-languages`: remove non-English boot-floppy translations and additional language packages and their receipt entries from the generated CD.
  English and existing localized files in the base system are retained.
- `--fix-pic-bug`: opt in to the PIC interrupt fix for both the boot-floppy kernel and the kernel installed on the hard disk, and install `/usr/bin/fix-pic-bug` for later use.
- `--nextufs /path/to/nextufs`: select the nextufs executable explicitly.
- `--iso-tool /path/to/xorriso`: select an ISO tool explicitly (`mkisofs`, `genisoimage`, or `xorriso`).

Developer packages, Developer Patch 4, and Profiling Libraries Patch 4 are copied to `/NextCD/Packages` for manual installation with Installer.app after setup.
Only User Patch 4 is applied automatically when supplied.
With `--fix-pic-bug`, the PIC fix is applied after Patch 4 to both the boot and installed kernels.
Manually reinstalling User Patch 4 replaces the installed kernel.
If you built with `--fix-pic-bug`, run `/usr/bin/fix-pic-bug` as root after installing the patch and before rebooting.
The helper supports stock OPENSTEP 4.2 and Patch 4 kernels, leaves already-patched kernels unchanged, and refuses unknown kernels.
It saves the original as `/mach_kernel.pre-pic-fix` without overwriting an existing backup.
The package itself is not modified.

### Setup.app

When included, open Setup.app from the mounted CD after booting and configuring the installed system, logged in as root.
Setup recognizes the automatically installed User Patch 4 receipt and skips reinstalling it.
FramebufferWC is unchecked by default and is not installed or activated by building or booting the CD.
If its post-install action fails, Setup offers Retry without reinstalling the package.
Selecting it again in a later Setup session safely reapplies the patch and activation, including after reinstalling User Patch 4.
