#!/usr/bin/perl
# OPENSTEP 4.2's system Perl 5. Plans seven native UFS volumes, then lets
# the native disk/newfs utilities initialize them from an explicit disktab.
# plan is read-only; prepare is called only after rc.cdrom's erase confirmation.
# Capacity, layout() sizes and the MBR use 512-byte device sectors. The NeXT
# label uses 1024-byte logical sectors to avoid OPENSTEP's buffer-cache bug.

sub layout {
    local($sectors) = @_;
    $sectors =~ /^[0-9]+$/ && $sectors >= 153922 && $sectors <= 2147483647
        or die "Unsupported disk capacity (512-byte sectors): $sectors\n";
    # Leave 128 KiB below 4 GiB for OPENSTEP's filesystem arithmetic.
    # Sector 0 is the MBR; NeXT starts at LBA 2 with a 160 KiB front porch.
    local($start, $limit, $remaining, $size, @sizes) = (322, 8388352);
    $remaining = $sectors - $start;
    while ($remaining >= 153600 && @sizes < 7) {
        $size = $remaining < $limit ? $remaining : $limit;
        $size -= $size % 2;       # UFS fragments are at least 1 KiB.
        push(@sizes, $size);
        $remaining -= $size;
    }
    return @sizes;
}

sub disk_sectors {
    local($device) = @_;
    $device =~ m{^/dev/r(?:sd|hd)[0-9]+h$} && -c $device
        or die "Expected a whole raw SCSI or IDE disk: $device\n";
    open(CAPACITY, "<$device") or die "Cannot open $device: $!\n";
    local($word) = pack("L", 0);
    ioctl(CAPACITY, 0x40046418, $word) or die "Cannot read sector size: $!\n";
    unpack("L", $word) == 512 or die "Only 512-byte device sectors are supported\n";
    ioctl(CAPACITY, 0x40046419, $word) or die "Cannot read sector count: $!\n";
    local($sectors) = unpack("L", $word);
    close(CAPACITY) or die "Cannot close $device: $!\n";
    &layout($sectors);           # Validate even for a read-only preview.
    return $sectors;
}

sub disktab {
    local(@sizes) = @_;
    local($end, $base, $i, $letter, $text, $logical_size) = (322, 1, 0);
    foreach $size (@sizes) { $end += $size; }
    $text = "quickstep|Quickstep installation:\\\n";
    # Halve every sector-valued field, retaining the same byte geometry:
    # 1 MiB cylinders, 160 KiB front porch, loaders at 33 KiB and 97 KiB.
    $text .= "\t:ty=fixed_rw_scsi:ss#1024:nt#64:ns#16:nc#" . int(($end + 2047) / 2048) . ":rm#3600:\\\n";
    $text .= "\t:fp#160:bp#0:ng#0:gs#0:ga#0:ao#0:os=mach_kernel:z0#33:z1#97:ro=a:rw=a:\\\n";
    foreach $size (@sizes) {
        $logical_size = $size / 2;
        $letter = substr("abcdefg", $i++, 1);
        $text .= "\t:p$letter#$base:s$letter#$logical_size:b$letter#8192:f$letter#1024:c$letter#16:d$letter#4096:r$letter#10:o$letter=time:i$letter:t$letter=4.3BSD:\\\n";
        $base += $logical_size;
    }
    # Missing numeric capabilities become -1. The native parser does not
    # accept a minus sign in '#-1'; explicitly writing that would become zero.
    return $text . "\t:\n";
}

sub mbr {
    local($boot, @sizes) = @_;
    length($boot) == 512 && substr($boot, 510, 2) eq pack("H*", "55aa") &&
        substr($boot, 446, 64) eq ("\0" x 64)
        or die "Invalid OPENSTEP boot1 template\n";
    local($end) = 322;
    foreach $size (@sizes) { $end += $size; }
    substr($boot, 446, 16) = pack("H*", "80000300a7feffff") . pack("V2", 2, $end - 2);
    return $boot . ("\0" x (65 * 512));
}

sub write_layout {
    local($handle, $data) = @_;
    seek($handle, 0, 0) or die "Cannot seek to disk start: $!\n";
    syswrite($handle, $data, length($data)) == length($data)
        or die "Cannot write complete disk layout: $!\n";
    seek($handle, 0, 0) or die "Cannot seek for layout verification: $!\n";
    local($actual) = "";
    read($handle, $actual, length($data)) == length($data) && $actual eq $data
        or die "Disk layout readback failed\n";
}

sub verify_layout {
    local($handle, $boot, @sizes) = @_;
    local($expected) = &mbr($boot, @sizes);
    local($actual, $block, $i, $base, $offset, $size);
    seek($handle, 0, 0) && read($handle, $actual, 512) == 512 &&
        $actual eq substr($expected, 0, 512)
        or die "Formatter changed the prepared MBR\n";
    # Label copy locations remain in physical 512-byte sectors.
    foreach $block (17, 32, 47) {
        seek($handle, $block * 512, 0) && read($handle, $actual, 1024) == 1024
            or die "Cannot read formatted disk label\n";
        substr($actual, 0, 4) eq 'dlV3' &&
            unpack('N', substr($actual, 0x5c, 4)) == 1024 &&
            unpack('n', substr($actual, 0x70, 2)) == 160 &&
            unpack('N', substr($actual, 0x7c, 4)) == 33 &&
            unpack('N', substr($actual, 0x80, 4)) == 97 &&
            substr($actual, 0xbc, 1) eq 'a'
            or die "Unexpected formatted disk label\n";
        $base = 1;
        for ($i = 0; $i < 8; $i++) {
            ($offset, $size) = unpack('N2', substr($actual, 0xbe + 46 * $i, 8));
            if ($i < @sizes) {
                $offset == $base && $size * 2 == $sizes[$i]
                    or die "Formatter changed volume " . substr('abcdefgh', $i, 1) . "\n";
                $base += $size;
            } else {
                $offset == 4294967295 && $size == 4294967295
                    or die "Formatter created an unexpected volume\n";
            }
        }
    }
}

sub format_layout {
    local($device, $formatter, @sizes) = @_;
    # The CD/USB root, including /tmp, is read-only. Feed the native parser
    # through an anonymous pipe; no temporary files or /dev/fd are required.
    local($pid) = open(FORMAT, "|-");
    defined($pid) or die "Cannot start disk formatter: $!\n";
    if (!$pid) {
        exec($formatter, "-t", "quickstep", "-N", "-i", "-u", $device);
        die "Cannot execute disk formatter: $!\n";
    }
    local($SIG{'PIPE'}) = 'IGNORE';
    local($written) = print FORMAT &disktab(@sizes);
    local($finished) = close(FORMAT);
    $written && $finished or die "Disk formatter failed\n";
}

sub main {
    local($mode, $device, $bootpath) = @ARGV;
    defined($device) && (($mode eq "plan" && @ARGV == 2) ||
        (($mode eq "prepare" || $mode eq "verify" || $mode eq "format") && @ARGV == 3))
        or die "Usage: installer-layout plan raw-disk | prepare|verify raw-disk boot1 | format raw-disk formatter\n";
    local($sectors) = &disk_sectors($device);
    local(@sizes) = &layout($sectors);
    if ($mode eq "plan") {
        local($used, $i) = (322, 0);
        print "OPENSTEP will use ", scalar(@sizes), " volume(s):\n";
        foreach $size (@sizes) {
            printf "  %s: %.2f MiB\n", $i == 0 ? "/" : $i == 1 ? "/usr/local" : "/Data$i", $size / 2048;
            $used += $size;
            $i++;
        }
        printf "Space left unallocated: %.2f MiB\n", ($sectors - $used) / 2048;
        return;
    }
    if ($mode eq "format") {
        &format_layout($device, $bootpath, @sizes);
        return;
    }
    open(BOOT, "<$bootpath") or die "Cannot read boot1: $!\n";
    binmode(BOOT);
    local($boot);
    { local($/) = undef; $boot = <BOOT>; }
    close(BOOT) or die "Cannot close boot1: $!\n";
    local($data) = &mbr($boot, @sizes);
    if ($mode eq "verify") {
        open(VERIFY, "<$device") or die "Cannot open destination for verification: $!\n";
        binmode(VERIFY);
        &verify_layout(VERIFY, $boot, @sizes);
        close(VERIFY) or die "Cannot close destination: $!\n";
        return;
    }
    # Reject a mounted destination, including any of its data volumes.
    local($name) = $device;
    $name =~ s|^/dev/r||;
    $name =~ s/h$//;
    open(MOUNTS, "/usr/etc/mount |") or die "Cannot inspect mounted disks\n";
    local($mounted) = 0;
    while (<MOUNTS>) { $mounted = 1 if m|^/dev/${name}[a-g] on |; }
    close(MOUNTS) or die "Cannot inspect mounted disks\n";
    !$mounted or die "Refusing to erase mounted disk $device\n";
    # The copied formatter preserves this MBR while creating labels, secondary
    # loaders and filesystems. All preparation writes go to the confirmed disk.
    open(TARGET, "+<$device") or die "Cannot open destination: $!\n";
    binmode(TARGET);
    &write_layout(TARGET, $data);
    close(TARGET) or die "Cannot close destination: $!\n";
    print scalar(@sizes), "\n";
}

&main unless caller;
