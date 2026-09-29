"""Script-only CD/USB installer limits, error handling, and prepared MBR bounds."""

import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import installer
import media


def shell_path(path):
    value = path.resolve().as_posix()
    return '/' + value[0].lower() + value[2:] if os.name == 'nt' else value


class LayoutTests(unittest.TestCase):
    def test_layout_preserves_boot_code_and_bounds_the_partition(self):
        boot = bytes(range(256)) * 2
        boot = boot[:446] + bytes(64) + b'\x55\xaa'
        layout = installer.limited_layout(boot)
        self.assertEqual(layout, boot)
        for bad in (b'', boot[:-1], boot[:-2] + b'xx',
                    boot[:446] + b'\x80' + boot[447:]):
            with self.assertRaises(ValueError):
                installer.limited_layout(bad)

    def test_unknown_script_rejected(self):
        with self.assertRaises(ValueError):
            installer.patch_script(b'#!/bin/sh\n')


class VerificationTests(unittest.TestCase):
    def test_cd_verification_accepts_disk_preparation_without_driver_changes(self):
        with ExitStack() as stack:
            for name in ('_verify_prepared_boot', 'iso_layout', 'cd_layout', '_raw_ufs_info', '_check_iso_ufs'):
                stack.enter_context(patch.object(media, name))
            payload = stack.enter_context(patch.object(media, 'check_payload'))
            verify = stack.enter_context(patch.object(media, '_verify_installer'))
            media.verify_boot_cd('original-boot', 'boot', 'original-cd', 'ufs', 'iso', disk_limits=True)
            self.assertEqual(len(payload.call_args_list), 2)
            self.assertTrue(all(call.args[0] == Path('iso') for call in payload.call_args_list))
            self.assertTrue(verify.call_args.kwargs['disk_limits'])

    def test_cd_and_usb_verification_reject_modified_utilities_layout_or_script(self):
        original = b'original installer'
        checked = b'    ${QUICKSTEP_FDISK} "$@"\ndiskie=`${PICKDISK} ${disknum}` || exit 1\n'
        boot1 = bytes(510) + b'\x55\xaa'
        for usb in (False, True):
            expected = media._patch_usb_installer(checked) if usb else checked
            files = {'/usr/etc/fdisk': b'native fdisk', '/usr/standalone/i386/boot1': boot1,
                     installer.MBR_PATH: installer.limited_layout(boot1), '/etc/rc.cdrom': expected,
                     '/usr/etc/disk': b'native disk', installer.DISK_PATH: b'layout disk',
                     installer.HELPER_PATH: installer.layout_helper()}
            for fault in (None, '/usr/etc/fdisk', '/usr/etc/disk', installer.MBR_PATH,
                          installer.DISK_PATH, installer.HELPER_PATH, '/etc/rc.cdrom'):
                with self.subTest(usb=usb, fault=fault):
                    def read(command, raw, source, path, **kwargs):
                        if source == 'original':
                            return original if path == '/etc/rc.cdrom' else (b'native disk' if path == '/usr/etc/disk' else b'native fdisk')
                        return files[path] + (b'corruption' if path == fault else b'')
                    with patch.object(installer, 'patch_script', return_value=checked), patch.object(installer, 'layout_disk', return_value=b'layout disk'), patch.object(media, 'nextufs', side_effect=read):
                        def verify():
                            media._verify_installer('boot', 'original', 'prepared', nextufs_binary=None,
                                                    installation_drivers=False, fix_pic_bug=False, package_hook=b'',
                                                    packaged_drivers=(), kernel_source=None, usb=usb, disk_limits=True)
                        if fault is None:
                            verify()
                        else:
                            with self.assertRaises(ValueError):
                                verify()


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.shell = 'C:/msys64/usr/bin/sh.exe' if os.name == 'nt' else shutil.which('sh')
        if not self.shell or not Path(self.shell).is_file():
            self.skipTest('Bourne shell required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.helper = Path(installer.__file__).with_name('installer-disk.sh').read_text()

    def run_shell(self, prefix, commands, inputs=''):
        script = self.work / 'check.sh'
        script.write_bytes(('set -u\nFDISK=native_fdisk\nPICKDISK=native_pickdisk\nDISK=native_disk\nAWK=awk\nSED=sed\n'
                            + prefix + '\n' + self.helper + '\n' + commands).encode())
        env = os.environ.copy()
        env['PATH'] = str(Path(self.shell).parent) + os.pathsep + env.get('PATH', '')
        result = subprocess.run([self.shell, shell_path(script)], input=inputs.encode(),
                                capture_output=True, timeout=10, env=env)
        result.stdout, result.stderr = result.stdout.decode(), result.stderr.decode()
        return result

    def test_display_cap_retains_physical_sizes_and_exit_status(self):
        prefix = '''native_pickdisk() {
            echo '1. SCSI Disk at target 0 (NVMe Disk) - 244198 MB'
            echo '2. IDE Disk #0 (Type 1) - 2048 MB'
            return 2
        }'''
        result = self.run_shell(prefix, '''quickstep_pickdisk
            status=$?
            disknum=1
            quickstep_physical_size
            disknum=2
            quickstep_physical_size
            exit $status''')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('244198 MB', result.stdout)
        self.assertTrue(result.stdout.endswith('244198\n2048\n'))

    def test_pickdisk_device_selection_remains_unchanged(self):
        result = self.run_shell('native_pickdisk() { echo sd0a; }', 'quickstep_pickdisk 1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'sd0a\n')

    def test_native_boot_writes_keep_boot1_only_for_the_prepared_layout(self):
        native = self.work / 'layout-disk'
        native.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 7\n')
        native.chmod(0o755)
        for fixed in ('yes', 'no'):
            for action in ('-i', '-b', '-e'):
                with self.subTest(fixed=fixed, action=action):
                    result = self.run_shell('native_disk() { printf "%s\\n" "$@"; return 7; }',
                                            f'CDDIR="{shell_path(self.work)}"\nQUICKSTEP_FIXED_LAYOUT={fixed}\n'
                                            f'${{DISK}} {action} "/dev/disk name"')
                    self.assertEqual(result.returncode, 7, result.stderr)
                    expected = f'{action}\n/dev/disk name\n'
                    if fixed == 'yes' and action in ('-i', '-b'):
                        expected = '-B0\n/usr/standalone/i386/boot1\n' + expected
                    if fixed == 'yes' and action == '-i':
                        expected = '-t\nquickstep\n-N\n' + expected
                        expected = expected.replace('/usr/standalone/i386/boot1', '/tmp/qsboot1')
                    self.assertEqual(result.stdout, expected)

    def test_failed_empty_and_invalid_inquiries_never_reach_numeric_test(self):
        for body in ('return 1', 'echo ""', 'echo nope', 'echo -1', 'echo "4 096"'):
            result = self.run_shell('native_fdisk() { ' + body + '; }', '''livedisk=/dev/rsd0h
                size=`quickstep_size -diskSize` || exit 1
                test "$size" -le 4096
                echo UNEXPECTED''')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn('UNEXPECTED', result.stdout)
            self.assertNotIn('argument expected', result.stderr)

    def test_prepare_failure_and_invalid_volume_counts_stop_installation(self):
        self.helper = self.helper.replace('/usr/bin/perl', 'test_perl')
        for output, status in (('', 1), ('oops', 0), ('0', 0), ('8', 0), ('7', 0)):
            result = self.run_shell('test_perl() { echo "' + output + '"; return ' + str(status) + '; }',
                'CDDIR=/NextCD\nlivedisk=/dev/rsd0h\nquickstep_write_layout || exit 1\necho CONTINUE')
            self.assertEqual(result.returncode, 0 if output == '7' else 1, result.stderr)
            self.assertEqual('CONTINUE' in result.stdout, output == '7')

    def test_post_format_verification_failure_stops_installation(self):
        self.helper = self.helper.replace('/usr/bin/perl', 'test_perl')
        native = self.work / 'layout-disk'
        native.write_text('#!/bin/sh\nexit 0\n')
        native.chmod(0o755)
        for status in (0, 1):
            result = self.run_shell('test_perl() { echo "$2"; return ' + str(status) + '; }',
                f'CDDIR="{shell_path(self.work)}"\nlivedisk=/dev/rsd0h\n'
                'QUICKSTEP_FIXED_LAYOUT=yes\n${DISK} -i -u "$livedisk" || exit 1\necho CONTINUE')
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertIn('verify', result.stdout)
            self.assertEqual('CONTINUE' in result.stdout, status == 0)

    def test_data_volumes_get_mountpoints_and_fstab_entries(self):
        target = self.work / 'target'
        (target / 'private/etc').mkdir(parents=True)
        table = target / 'private/etc/fstab'
        for count in (0, 1, 2, 7):
            table.write_text('/dev/sd0a / 4.3 rw,noquota,noauto 0 1\n')
            result = self.run_shell('', f'QUICKSTEP_VOLUMES={count}\n'
                f'HD="{shell_path(target)}"\ndiskie=sd0a\nMKDIRS="mkdir -p"\nEXPR=expr\nquickstep_data_volumes')
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = table.read_text().splitlines()
            self.assertEqual(len(lines), max(count, 1))
            for i in range(1, count):
                mount = '/usr/local' if i == 1 else f'/Data{i}'
                self.assertEqual(lines[i], f'/dev/sd0{chr(97+i)} {mount} 4.3 rw,noquota 0 2')
                self.assertTrue((target / mount.lstrip('/')).is_dir())

    def test_local_volume_mount_and_failure_status(self):
        target = self.work / 'target'
        for device in ('sd0a', 'hd1a'):
            for count in (0, 1, 2, 7):
                for status in (0, 1):
                    result = self.run_shell('test_mount() { printf "%s\\n" "$@"; return ' + str(status) + '; }',
                        f'QUICKSTEP_VOLUMES={count}\nHD="{shell_path(target)}"\ndiskie={device}\n'
                        'MKDIRS="mkdir -p"\nMOUNT=test_mount\nquickstep_mount_local')
                    self.assertEqual(result.returncode, status if count > 1 else 0, result.stderr)
                    self.assertEqual(result.stdout, f'-n\n/dev/{device[:-1]}b\n{shell_path(target)}/usr/local\n' if count > 1 else '')

    def test_cd_large_disk_bypasses_fdisk_and_waits_for_confirmation(self):
        self.check_full_script(usb=False)

    def test_usb_large_disk_bypasses_fdisk_and_waits_for_confirmation(self):
        self.check_full_script(usb=True)

    def check_full_script(self, *, usb):
        source = os.environ.get('INSTALLER_TEST_SCRIPT')
        if not source:
            self.skipTest('INSTALLER_TEST_SCRIPT required')
        fixed = installer.patch_script(Path(source).read_bytes())
        if usb:
            fixed = media._patch_usb_installer(fixed)
        fixed = fixed.decode()
        mount_hook = fixed.index('quickstep_mount_local || exit 1')
        self.assertLess(fixed.index('${MOUNT} -n /dev/$diskie'), mount_hook)
        self.assertLess(mount_hook, fixed.index('${DITTO} -T'))
        self.assertEqual('-useAllSectors' in fixed, usb)
        self.assertEqual('USB installation source' in fixed, usb)
        self.helper = fixed[fixed.index('# BEGIN quickstep disk limits'):fixed.index('# END quickstep disk limits')]
        native = 'native_fdisk() { printf "%s\\n" "$@"; return 7; }'
        result = self.run_shell(native, 'quickstep_fdisk "/dev/disk name" -diskSize')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, '/dev/disk name\n-diskSize\n' + ('-useAllSectors\n' if usb else ''))
        body = fixed[fixed.index('reply=""\nwhile [ -z "$reply" ]\ndo\n   clear'):]
        body = body[:body.index('# Get off the disk before we initialize it!')]
        prefix = '''
clear() { :; }
native_pickdisk() {
    case "${1-}" in 1) echo sd0a ;; *) echo 'SCSI Disk - 244198 MB' ;; esac
}
findroot() { echo /dev/sd1a; }
localecho() { :; }
ARCH=i386
MINSIZE=75
LOCALECHO=localecho
FINDROOT=findroot
SED=sed
EXPR=expr
HALT=false
native_fdisk() { echo UNEXPECTED_FDISK >&2; return 1; }
'''
        body = 'quickstep_preview_layout() { echo PLAN; }\nquickstep_write_layout() { echo WRITE_LAYOUT; }\n' + body + '\necho INITIALIZE'
        result = self.run_shell(prefix, body, '1\n1\n1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WRITE_LAYOUT\nINITIALIZE', result.stdout)
        self.assertNotIn('UNEXPECTED_FDISK', result.stderr)
        for inputs in ('1\n3\n', '1\n1\n2\n', '1\n2\n'):
            result = self.run_shell(prefix, body, inputs)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('WRITE_LAYOUT', result.stdout)
            self.assertNotIn('INITIALIZE', result.stdout)
            self.assertNotIn('argument expected', result.stderr)
        if usb:
            source_disk = prefix.replace('1) echo sd0a', '1) echo sd1a')
            result = self.run_shell(source_disk, body, '1\n')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('USB installation source', result.stdout)
            self.assertNotIn('WRITE_LAYOUT', result.stdout)
            failed_root = prefix.replace('findroot() { echo /dev/sd1a; }', 'findroot() { return 1; }')
            result = self.run_shell(failed_root, body, '1\n')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn('WRITE_LAYOUT', result.stdout)
            self.assertNotIn('INITIALIZE', result.stdout)


if __name__ == '__main__':
    unittest.main()
