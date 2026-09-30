"""Explicit seven-volume OPENSTEP CD/USB erase layout and checked selection."""

import hashlib
from pathlib import Path
import struct


MBR_PATH = '/NextCD/LayoutBoot1'
HELPER_PATH = '/NextCD/installer-layout'
DISK_PATH = '/NextCD/layout-disk'

# File offsets for the two supported Intel executables (stock and Patch 4).
# _dgetent: replace open("/etc/disktab", 0) with fd 0; it still reads/closes it.
# _boot: skip the boot1 write, which would overwrite the already prepared MBR
# when -t bypasses partition inference. The secondary loaders remain enabled.
_DISK_STREAM_PATCHES = {
    'aec5fa7501d2942cced00ad9c7fae46a36ffbcf1c840adbd0cf3fb9b83dee777': (
        (0x5ad4, bytes.fromhex('e8039b0000'), bytes.fromhex('31c0909090')),
        (0x3985, bytes.fromhex('0f84a1000000'), bytes.fromhex('e9a200000090')),
    ),
    'ceec81bb3b8f3fc9c52a587ab191f5d09eae1cd879bad879d3ca7e301f53e5e9': (
        (0x58dc, bytes.fromhex('e8fb9c0000'), bytes.fromhex('31c0909090')),
        (0x378d, bytes.fromhex('0f84a1000000'), bytes.fromhex('e9a200000090')),
    ),
}


def limited_layout(boot1: bytes) -> bytes:
    """Validate the boot template; runtime capacity determines the MBR extent."""
    if len(boot1) != 512 or boot1[510:] != b'\x55\xaa' or any(boot1[446:510]):
        raise ValueError('expected a 512-byte OPENSTEP boot1 with an empty partition table and MBR signature')
    return boot1


def layout_helper() -> bytes:
    return Path(__file__).with_name('installer-layout.pl').read_text(encoding='ascii').encode('ascii')


def layout_disk(binary: bytes) -> bytes:
    """Copy the known Intel disk utility for initialization from read-only media.

    Read the explicit disktab from stdin and retain the MBR written by prepare.
    Only the installer copy is patched; /usr/etc/disk stays unchanged. This copy
    is used exclusively with -t quickstep -N -i -u, without console input.
    """
    if binary[:4] == bytes.fromhex('cafebabe'):
        if len(binary) < 8:
            raise ValueError('truncated disk executable')
        count = struct.unpack_from('>I', binary, 4)[0]
        end = 8 + count * 20
        if end > len(binary):
            raise ValueError('truncated disk architecture table')
        intel = [struct.unpack_from('>5I', binary, p) for p in range(8, end, 20)
                 if struct.unpack_from('>I', binary, p)[0] == 7]
        if len(intel) != 1:
            raise ValueError('expected one Intel disk executable')
        _, _, offset, size, _ = intel[0]
        if offset < end or offset + size > len(binary):
            raise ValueError('invalid disk architecture extent')
        binary = binary[offset:offset + size]
    changes = _DISK_STREAM_PATCHES.get(hashlib.sha256(binary).hexdigest())
    if changes is None:
        raise ValueError('unsupported OPENSTEP disk executable')
    result = bytearray(binary)
    for offset, before, after in changes:
        if binary[offset:offset + len(before)] != before:
            raise ValueError('unsupported OPENSTEP disk instructions')
        result[offset:offset + len(before)] = after
    return bytes(result)


def patch_script(script: bytes) -> bytes:
    """Offer explicit 4 GiB volumes on every Intel destination, without fdisk."""
    if b'# BEGIN quickstep disk limits' in script:
        raise ValueError('installer already contains disk limits')

    def edit(before: bytes, after: bytes, count: int = 1) -> None:
        nonlocal script
        if script.count(before) != count:
            raise ValueError('unsupported rc.cdrom disk selection: ' + repr(before))
        script = script.replace(before, after)

    helper = Path(__file__).with_name('installer-disk.sh').read_bytes().replace(b'\r\n', b'\n')
    edit(b'# Clean up output\n', helper + b'\n# Clean up output\n')
    edit(b'diskie=`${PICKDISK} ${disknum}`\n', b'diskie=`${PICKDISK} ${disknum}` || exit 1\n')
    edit(b'livedisk=`echo $rawdisk | ${SED} s/a/h/`\n', b'''livedisk=`echo $rawdisk | ${SED} s/a/h/`

if [ "${ARCH}" = "i386" ]; then
    physicalsize=`quickstep_physical_size` || exit 1
    reply=""
    while [ -z "$reply" ]; do
        clear
        quickstep_preview_layout || exit 1
        echo "Type 1 to erase all partitions and create the volumes shown above."
        echo "Type 2 for advanced partitioning using the original fdisk."
        echo "Type 3 to quit without changing the disk."
        echo -n "---> "
        read reply
        case "$reply" in
            1) QUICKSTEP_FIXED_LAYOUT=yes ;;
            2) ;;
            3) exit 1 ;;
            *) reply="" ;;
        esac
    done
fi
''')
    edit(b'if [ "${ARCH}" = "i386" ]; then\n   reply=""',
         b'if [ "${ARCH}" = "i386" -a "$QUICKSTEP_FIXED_LAYOUT" = "no" ]; then\n   reply=""')
    edit(b'ispartitioned=`${FDISK} $livedisk -isDiskPartitioned`', b'''ispartitioned=`${FDISK} $livedisk -isDiskPartitioned` || exit 1
      case "$ispartitioned" in
          Yes|No) ;;
          *) echo "Cannot read partition information; installation stopped."; exit 1 ;;
      esac''')
    for variable, query, count in ((b'disksize', b'-diskSize', 1), (b'currentsize', b'-installSize', 2),
                                   (b'freesize', b'-freeSpace', 1), (b'esize', b'-sizeofExtended', 1),
                                   (b'resp2', b'-sizeofExtended', 1)):
        edit(variable + b'=`${FDISK} $livedisk ' + query + b'`',
             variable + b'=`quickstep_size ' + query + b'` || exit 1', count)
    edit(b'if [ $ispartitioned = "Yes" ]', b'if [ "$ispartitioned" = "Yes" ]')
    # fdisk rounds to nearest MiB. Leave a margin for manually chosen layouts.
    edit(b'      choices=2\n', b'''      if [ "$currentsize" -gt 4095 ]; then currentsize=0; fi
      if [ "$freesize" -gt 4095 ]; then freesize=0; fi
      if [ "$esize" -gt 4095 ]; then esize=0; fi
      choices=2
''')
    edit(b'FDISK_FLAGS="-removePartitioning" ;;', b'''FDISK_FLAGS="-removePartitioning"
                        QUICKSTEP_FIXED_LAYOUT=yes
                        FDISK_FLAGS=""
                        quickstep_preview_layout || exit 1
                        break ;;''')
    edit(b'newsize=`${EXPR} $disksize - $resp2`', b'''case "$resp2" in
                                  ''|*[!0-9]*) continue ;;
                              esac
                              newsize=`${EXPR} $disksize - $resp2`''')
    edit(b'if [ $newsize -gt $MINSIZE ]; then', b'''if [ "$newsize" -gt 4095 ]; then
                                  echo "The OPENSTEP partition is too large. Use the prepared erase layout or advanced options."
                                  continue
                              fi
                              if [ $newsize -gt $MINSIZE ]; then''')
    edit(b'${FDISK} $livedisk ${FDISK_FLAGS}\n', b'${FDISK} $livedisk ${FDISK_FLAGS} || exit 1\n')
    edit(b'${FDISK} $livedisk\n', b'${FDISK} $livedisk || exit 1\n')
    edit(b'if [ -z "$currentsize" -o ${currentsize} -lt $MINSIZE ]; then',
         b'if [ "$currentsize" -lt "$MINSIZE" ]; then')
    edit(b'# Get off the disk before we initialize it!\n', b'''if [ "${ARCH}" = "i386" ]; then
    if [ "$QUICKSTEP_FIXED_LAYOUT" = "yes" ]; then
        quickstep_write_layout || exit 1
    else
        currentsize=`quickstep_size -installSize` || exit 1
        if [ "$currentsize" -lt "$MINSIZE" ]; then exit 1; fi
        if [ "$physicalsize" -gt 4096 -a "$currentsize" -gt 4095 ]; then
            echo "The OPENSTEP partition is too large; installation stopped."
            exit 1
        fi
    fi
fi

# Get off the disk before we initialize it!
''')
    edit(b'${DISK} -i $livedisk', b'${DISK} -i -u $livedisk')
    # Mount before copying any packages, so /usr/local files land on volume b.
    edit(b'${MOUNT} -n /dev/$diskie ${HD} >> /dev/null\n',
         b'${MOUNT} -n /dev/$diskie ${HD} >> /dev/null || exit 1\n'
         b'quickstep_mount_local || exit 1\n')
    edit(b'echo "/dev/${diskie} / 4.3 rw,noquota,noauto 0 1" > ${HD}/private/etc/fstab\n',
         b'echo "/dev/${diskie} / 4.3 rw,noquota,noauto 0 1" > ${HD}/private/etc/fstab\n'
         b'quickstep_data_volumes || exit 1\n')
    return script
