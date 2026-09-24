# BEGIN quickstep disk limits
QUICKSTEP_FDISK=${FDISK}
QUICKSTEP_PICKDISK=${PICKDISK}
FDISK=quickstep_fdisk
PICKDISK=quickstep_pickdisk
FDISK_FLAGS=
QUICKSTEP_DISK_LIST=
QUICKSTEP_FIXED_LAYOUT=no

quickstep_pickdisk() {
    disk_list=`${QUICKSTEP_PICKDISK} "$@"`
    disk_status=$?
    # Display calls retain capacities; device-name selection is in a subshell.
    case "${1-}" in
        ''|0) QUICKSTEP_DISK_LIST="${disk_list}" ;;
    esac
    echo "${disk_list}" | ${AWK} '
        / - [0-9]+ MB$/ {
            if ($(NF-1) > 4096) {
                for (i = 1; i < NF-1; i++) printf "%s ", $i
                print "4096 MB usable for OPENSTEP"
                next
            }
        }
        { print }
    ' || return 255
    return ${disk_status}
}

quickstep_fdisk() {
    ${QUICKSTEP_FDISK} "$@"
}

quickstep_number() {
    case "$1" in
        ''|*[!0-9]*)
            echo "Cannot read disk size; installation stopped." >&2
            return 1 ;;
    esac
    echo "$1"
}

quickstep_size() {
    disk_size=`${FDISK} "$livedisk" "$1"` || return 1
    quickstep_number "${disk_size}"
}

quickstep_physical_size() {
    disk_size=`echo "${QUICKSTEP_DISK_LIST}" | ${AWK} '
        / - [0-9]+ MB$/ { print $(NF-1) }
    ' | ${SED} -n "${disknum}p"` || return 1
    quickstep_number "${disk_size}"
}

quickstep_write_layout() {
    echo "Creating a 4 GiB OPENSTEP partition; remaining space will be unallocated."
    # The asset includes zeros covering old labels in the reserved area.
    /bin/dd if="${CDDIR}/MBR4GiB" of="${livedisk}" bs=512 count=66 || return 1
    /bin/dd if="${livedisk}" bs=512 count=66 |
        /bin/cmp - "${CDDIR}/MBR4GiB" || return 1
}
# END quickstep disk limits
