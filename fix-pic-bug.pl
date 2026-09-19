#!/usr/bin/perl
# Fix OPENSTEP's spurious IRQ15 handler: acknowledge cascade IRQ2 on the
# master PIC, while leaving spurious IRQ7 unacknowledged. Run as root after
# installing User Patch 4, before rebooting. Uses the system's Perl 5.
# Usage: fix-pic-bug [/absolute/path/to/kernel]

@ARGV <= 1 or die "Usage: fix-pic-bug [/absolute/path/to/kernel]\n";
$kernel = @ARGV ? $ARGV[0] : "/mach_kernel";
$kernel =~ m|^/| or die "fix-pic-bug: kernel path must be absolute\n";
@original = lstat($kernel);
@original && -f _ && ! -l _ && $original[3] == 1
    or die "fix-pic-bug: expected a regular kernel without symlinks or hard-link aliases: $kernel\n";
open(INPUT, "<$kernel") or die "fix-pic-bug: cannot read $kernel: $!\n";
binmode(INPUT);
undef $/;
$data = <INPUT>;
close(INPUT) or die "fix-pic-bug: cannot close $kernel: $!\n";
length($data) == $original[7] or die "fix-pic-bug: incomplete kernel read\n";

# A package may contain a fat kernel. Patch only its Intel slice and leave
# every other architecture and the fat header byte-for-byte unchanged.
$base = 0;
$size = length($data);
if (substr($data, 0, 4) eq pack("H*", "cafebabe")) {
    length($data) >= 8 or die "fix-pic-bug: truncated fat header\n";
    $count = unpack("N", substr($data, 4, 4));
    $end = 8 + 20 * $count;
    $count > 0 && $end <= length($data)
        or die "fix-pic-bug: invalid fat header\n";
    $found = 0;
    for ($i = 0; $i < $count; $i++) {
        ($cpu, $subtype, $offset, $length, $align) =
            unpack("N5", substr($data, 8 + 20 * $i, 20));
        $offset >= $end && $length >= 28 && $offset + $length <= length($data)
            or die "fix-pic-bug: invalid architecture extent\n";
        if ($cpu == 7) {
            $base = $offset;
            $size = $length;
            $found++;
        }
    }
    $found == 1 or die "fix-pic-bug: expected exactly one Intel kernel\n";
    for ($i = 0; $i < $count; $i++) {
        ($cpu, $subtype, $offset, $length) = unpack("N4", substr($data, 8 + 20 * $i, 16));
        if ($cpu != 7 && $offset < $base + $size && $base < $offset + $length) {
            die "fix-pic-bug: overlapping architecture extents\n";
        }
    }
}
$size >= 28 && substr($data, $base, 8) eq pack("H*", "cefaedfe07000000")
    or die "fix-pic-bug: expected an i386 Mach-O kernel\n";
# Match media.py's signatures; regression tests compare both patchers.
@profiles = (
    ["OPENSTEP 4.2", 0x8c70a,
     "7d0f83fb0f7517baa0000000ec84c07c0dff05d8691f00e9260100009090",
     "7d1383fb0f7517baa0000000ec84c07c0db062e620e92801000090909090"],
    ["OPENSTEP 4.2 Patch 4", 0x8c88e,
     "7d0f83fb0f7517baa0000000ec84c07c0dff05187a1f00e9260100009090",
     "7d1383fb0f7517baa0000000ec84c07c0db062e620e92801000090909090"],
);
$matched = 0;
foreach $profile (@profiles) {
    ($name, $offset, $oldhex, $newhex) = @$profile;
    $old = pack("H*", $oldhex);
    $new = pack("H*", $newhex);
    next if $offset + length($old) > $size;
    $actual = substr($data, $base + $offset, length($old));
    if ($actual eq $new) {
        print "fix-pic-bug: $name kernel already patched; no changes.\n";
        exit 0;
    }
    if ($actual eq $old) {
        substr($data, $base + $offset, length($old)) = $new;
        $matched = 1;
        last;
    }
}
$matched or die "fix-pic-bug: unknown or partially patched kernel; no changes.\n";

# Stage beside the kernel for an atomic rename. An exclusive directory avoids
# following a pre-existing temporary-file symlink. Never overwrite a backup.
$backup = "$kernel.pre-pic-fix";
! lstat($backup) or die "fix-pic-bug: backup already exists: $backup; move it aside first\n";
$stage = "$kernel.picfix.$$";
mkdir($stage, 0700) or die "fix-pic-bug: cannot create staging directory: $!\n";
END {
    if ($stage_created) {
        unlink("$stage/kernel");
        rmdir($stage);
    }
}
$stage_created = 1;
system("/bin/cp", "-p", $kernel, "$stage/kernel") == 0
    or die "fix-pic-bug: cannot copy kernel; original unchanged\n";
open(OUTPUT, ">$stage/kernel") or die "fix-pic-bug: cannot open staged kernel: $!\n";
binmode(OUTPUT);
print OUTPUT $data or die "fix-pic-bug: cannot write staged kernel: $!\n";
close(OUTPUT) or die "fix-pic-bug: cannot close staged kernel: $!\n";
open(CHECK, "<$stage/kernel") or die "fix-pic-bug: cannot verify staged kernel: $!\n";
binmode(CHECK);
$check = <CHECK>;
close(CHECK);
$check eq $data or die "fix-pic-bug: staged kernel verification failed\n";
utime($original[8], $original[9], "$stage/kernel") == 1
    or die "fix-pic-bug: cannot preserve kernel timestamps: $!\n";
@current = lstat($kernel);
join(":", @current[0,1,2,3,4,5,7,9,10]) eq join(":", @original[0,1,2,3,4,5,7,9,10])
    or die "fix-pic-bug: kernel changed during patching; original not replaced\n";
link($kernel, $backup) or die "fix-pic-bug: cannot create backup $backup: $!\n";
rename("$stage/kernel", $kernel)
    or die "fix-pic-bug: cannot replace kernel: $!; backup is $backup\n";
print "fix-pic-bug: patched $name kernel; backup: $backup\n";
print "Reboot to use the patched kernel.\n";
