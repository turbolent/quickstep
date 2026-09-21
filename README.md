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

- **Recommended** `--bus-master-ide /path/to/BusMasterIDE.config`: use an extracted BusMasterIDE bundle instead of EIDE; `--beta-disk` is then unnecessary.
- `--developer-cd /path/to/Developer.iso`: include the Developer CD packages.
- `--user-patch /path/to/OS42MachUserPatch4.tar`: include User Patch 4.
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
- `--remove-language-packages`: remove additional language packages and their receipt entries from the generated CD.
  English and existing localized files in the base system are retained.
- `--fix-pic-bug`: opt in to the PIC interrupt fix for both the boot-floppy kernel and the kernel installed on the hard disk, and install `/usr/bin/fix-pic-bug` for later use.
- `--nextufs /path/to/nextufs`: select the nextufs executable explicitly.
- `--iso-tool /path/to/xorriso`: select an ISO tool explicitly (`mkisofs`, `genisoimage`, or `xorriso`).

Developer and Patch 4 packages are copied to `/NextCD/Packages` on the ISO for manual installation with Installer.app after setup.
They are not installed automatically.
Manually installing User Patch 4 replaces the installed kernel.
If you built with `--fix-pic-bug`, run `/usr/bin/fix-pic-bug` as root after installing the patch and before rebooting.
The helper supports stock OPENSTEP 4.2 and Patch 4 kernels, leaves already-patched kernels unchanged, and refuses unknown kernels.
It saves the original as `/mach_kernel.pre-pic-fix` without overwriting an existing backup.
The package itself is not modified.

### Setup.app

When included, open Setup.app from the mounted CD after booting and configuring the installed system, logged in as root.
FramebufferWC is unchecked by default and is not installed or activated by building or booting the CD.
If its post-install action fails, Setup offers Retry without reinstalling the package.
Selecting it again in a later Setup session safely reapplies the patch and activation, including after reinstalling User Patch 4.
