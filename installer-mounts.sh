# BEGIN quickstep installed mounts
if [ "${1-}" != singleuser -a "$iscdrom" -eq 0 -a -f /private/etc/fstab.quickstep-volumes ]; then
    # Keep unrelated entries (including the installation source) intact.
    # A matching device or mountpoint is replaced, never duplicated.
    if /bin/cp -p /private/etc/fstab /private/etc/fstab.quickstep &&
        /bin/awk '
            reading == 1 {
                entries[++count] = $0
                devices[count] = $1
                mounts[count] = $2
                next
            }
            /^#/ { print; next }
            {
                keep = 1
                for (i = 1; i <= count; i++) {
                    if ($1 == devices[i] || $2 == mounts[i]) keep = 0
                }
                if (keep) print
            }
            END { for (i = 1; i <= count; i++) print entries[i] }
        ' reading=1 /private/etc/fstab.quickstep-volumes reading=0 /private/etc/fstab \
            > /private/etc/fstab.quickstep &&
        /bin/mv /private/etc/fstab.quickstep /private/etc/fstab; then
        # These entries may have been absent from the first fsck pass. Check
        # each extra filesystem before rc mounts it; never recheck live root.
        while read volume_device volume_mount volume_rest; do
            case "$volume_device" in
                /dev/sd[0-9]*[b-g]|/dev/hd[0-9]*[b-g]) ;;
                *) echo "Invalid installation volume: $volume_device"; exit 1 ;;
            esac
            /usr/etc/fsck -p "$volume_device" || exit 1
        done < /private/etc/fstab.quickstep-volumes
        # Keep the recovery data across interrupted installation attempts.
        if [ ! -f /private/adm/CDIS.custom -a ! -f /private/adm/BuildDisk.custom ]; then
            /bin/rm -f /private/etc/fstab.quickstep-volumes || exit 1
        fi
    else
        echo "Cannot restore installation volume mounts; startup stopped."
        exit 1
    fi
fi
# END quickstep installed mounts
