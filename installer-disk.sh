# BEGIN quickstep disk limits
QUICKSTEP_FDISK=${FDISK}
QUICKSTEP_PICKDISK=${PICKDISK}
QUICKSTEP_DISK=${DISK}
FDISK=quickstep_fdisk
PICKDISK=quickstep_pickdisk
DISK=quickstep_disk
FDISK_FLAGS=
QUICKSTEP_DISK_LIST=
QUICKSTEP_FIXED_LAYOUT=no
QUICKSTEP_VOLUMES=0

quickstep_pickdisk() {
    disk_list=`${QUICKSTEP_PICKDISK} "$@"`
    disk_status=$?
    # Display calls retain capacities; device-name selection is in a subshell.
    case "${1-}" in
        ''|0) QUICKSTEP_DISK_LIST="${disk_list}" ;;
    esac
    echo "${disk_list}"
    return ${disk_status}
}

quickstep_fdisk() {
    ${QUICKSTEP_FDISK} "$@"
}

quickstep_disk() {
    case "$QUICKSTEP_FIXED_LAYOUT:${1-}" in
        yes:-i)
            /usr/bin/perl "${CDDIR}/installer-layout" format "${livedisk}" "${CDDIR}/layout-disk" || return $?
            /usr/bin/perl "${CDDIR}/installer-layout" verify "${livedisk}" "${CDDIR}/LayoutBoot1" ;;
        yes:-b)
            # Stock disk mixes the physical DOS base with logical label sectors.
            # Use the prepared layout and update only the secondary loaders.
            /usr/bin/perl "${CDDIR}/installer-layout" verify "${livedisk}" "${CDDIR}/LayoutBoot1" || return $?
            /usr/bin/perl "${CDDIR}/installer-layout" boot "${livedisk}" "${CDDIR}/layout-disk" || return $?
            /usr/bin/perl "${CDDIR}/installer-layout" verify "${livedisk}" "${CDDIR}/LayoutBoot1" ;;
        *) ${QUICKSTEP_DISK} "$@" ;;
    esac
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
    QUICKSTEP_VOLUMES=`/usr/bin/perl "${CDDIR}/installer-layout" prepare "${livedisk}" "${CDDIR}/LayoutBoot1"` || return 1
    case "$QUICKSTEP_VOLUMES" in [1-7]) ;; *) return 1 ;; esac
}

quickstep_preview_layout() {
    /usr/bin/perl "${CDDIR}/installer-layout" plan "${livedisk}"
}

quickstep_mount_local() {
    case "$QUICKSTEP_VOLUMES" in 0|1) return 0 ;; [2-7]) ;; *) return 1 ;; esac
    local_disk=`echo "$diskie" | ${SED} 's/a$//'`
    ${MKDIRS} "${HD}/usr/local" || return 1
    ${MOUNT} -n "/dev/${local_disk}b" "${HD}/usr/local"
}

quickstep_data_volumes() {
    case "$QUICKSTEP_VOLUMES" in 0|1) return 0 ;; [2-7]) ;; *) return 1 ;; esac
    data_disk=`echo "$diskie" | ${SED} 's/a$//'`
    data_index=1
    for data_letter in b c d e f g; do
        if [ "$data_index" -ge "$QUICKSTEP_VOLUMES" ]; then break; fi
        data_mount=/Data${data_index}
        if [ "$data_index" -eq 1 ]; then data_mount=/usr/local; fi
        ${MKDIRS} "${HD}${data_mount}" || return 1
        echo "/dev/${data_disk}${data_letter} ${data_mount} 4.3 rw,noquota 0 2" >> "${HD}/private/etc/fstab" || return 1
        data_index=`${EXPR} "$data_index" + 1` || return 1
    done
}
# END quickstep disk limits
