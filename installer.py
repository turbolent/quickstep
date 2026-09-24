"""Script-based OPENSTEP CD/USB installer limits; native utilities stay unchanged."""

from pathlib import Path
import struct


MBR_PATH = '/NextCD/MBR4GiB'
LIMIT_SECTORS = 4 * 1024 * 1024 * 1024 // 512
LAYOUT_SECTORS = 66


def limited_layout(boot0: bytes) -> bytes:
    """Prepared MBR and cleared labels for one active NeXT partition below 4 GiB.

    Start at LBA 2 (CHS 0/0/3), the same geometry-independent location used for
    our USB source image. The entire NeXT extent ends at the 4 GiB boundary.
    """
    if len(boot0) != 512 or boot0[510:] != b'\x55\xaa':
        raise ValueError('expected a 512-byte OPENSTEP boot0 with MBR signature')
    mbr = bytearray(boot0)
    mbr[446:510] = b'\0' * 64
    mbr[446:454] = bytes.fromhex('80000300a7feffff')
    struct.pack_into('<II', mbr, 454, 2, LIMIT_SECTORS - 2)
    # OPENSTEP has no /dev/zero. Supply the reserved-area zeros in the asset.
    return bytes(mbr) + bytes((LAYOUT_SECTORS - 1) * 512)


def patch_script(script: bytes) -> bytes:
    """Offer a prepared erase layout on large disks without invoking fdisk."""
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
    if [ "$physicalsize" -gt 4096 ]; then
        reply=""
        while [ -z "$reply" ]; do
            clear
            echo "Type 1 to erase all partitions and use up to 4096 MB for OPENSTEP."
            echo "The rest of the disk will remain unallocated."
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
                        if [ "$physicalsize" -gt 4096 ]; then
                            QUICKSTEP_FIXED_LAYOUT=yes
                            FDISK_FLAGS=""
                            break
                        fi ;;''')
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
    return script
