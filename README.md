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
- `--fix-pic-bug`: opt in to the PIC interrupt fix for both the boot-floppy kernel and the kernel installed on the hard disk.
- `--nextufs /path/to/nextufs`: select the nextufs executable explicitly.
- `--iso-tool /path/to/xorriso`: select an ISO tool explicitly (`mkisofs`, `genisoimage`, or `xorriso`).

Developer and Patch 4 packages are copied to `/NextCD/Packages` on the ISO for manual installation with Installer.app after setup.
They are not installed automatically.
Manually installing User Patch 4 replaces the installed kernel and may undo the PIC fix; `--fix-pic-bug` does not patch the package's replacement kernel.
