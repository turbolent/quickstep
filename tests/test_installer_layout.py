"""Execute the shipped Perl planner and decode its on-disk artifacts independently."""
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

import installer


def shell_path(path):
    value = path.resolve().as_posix()
    return '/' + value[0].lower() + value[2:] if os.name == 'nt' else value

try:
    import unicorn
    from unicorn import x86_const as x86
except ImportError:
    unicorn = None


class LayoutHelperTests(unittest.TestCase):
    def setUp(self):
        self.perl = 'C:/msys64/usr/bin/perl.exe' if os.name == 'nt' else shutil.which('perl')
        if not self.perl or not Path(self.perl).is_file():
            self.skipTest('Perl required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.helper = self.work / 'layout.pl'
        self.helper.write_bytes(installer.layout_helper())

    def run_perl(self, code, *args, prelude=''):
        wrapper = self.work / 'test.pl'
        wrapper.write_text(prelude + '\nrequire "' + shell_path(self.helper) + '";\n' + code)
        return subprocess.run([self.perl, shell_path(wrapper), *map(str, args)],
                              capture_output=True, timeout=10)

    def test_boundaries_and_volume_accounting(self):
        limit = 4 * 1024**3 // 512 - 256
        for sectors, expected in ((153922, [153600]), (153923, [153600]),
                                  (2**21, [2**21 - 322]), (2**21 + 1, [2**21 - 322]),
                                  (2**23, [2**23 - 322]), (2**24, [limit, limit]),
                                  (10 * 2**21, [limit, limit, 10 * 2**21 - 322 - 2 * limit]),
                                  (limit + 322 + 153599, [limit]),
                                  (limit + 322 + 153600, [limit, 153600]),
                                  (28 * 2**21, [limit] * 7), (500118192, [limit] * 7),
                                  (2147483647, [limit] * 7)):
            with self.subTest(sectors=sectors):
                r = self.run_perl('print join(",", layout($ARGV[0]));', sectors)
                self.assertEqual(r.returncode, 0, r.stderr)
                sizes = list(map(int, r.stdout.split(b',')))
                self.assertEqual(sizes, expected)
                self.assertLessEqual(322 + sum(sizes), sectors)
        for bad in ('0', '-1', '153921', '2147483648', '4GB', '1;exit'):
            r = self.run_perl('print join(",", layout($ARGV[0]));', bad)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(r.stdout, b'')

    def test_mbr_disktab_and_readback_cover_exactly_the_same_extents(self):
        boot = bytes(range(256)) + bytes(range(190)) + bytes(64) + b'\x55\xaa'
        bootfile = self.work / 'boot1'
        bootfile.write_bytes(boot)
        disk = self.work / 'scratch'
        for sectors in (2**23, 2**24, 28 * 2**21, 500118192):
            disk.write_bytes(b'x' * (128 * 512))
            r = self.run_perl('''
                open(B, "<$ARGV[1]") or die; binmode(B); read(B,$boot,512) == 512 or die;
                @sizes=layout($ARGV[0]); $mbr=mbr($boot,@sizes);
                open(D,"+<$ARGV[2]") or die; binmode(D); write_layout(D,$mbr); close(D) or die;
                print disktab(@sizes);
            ''', sectors, shell_path(bootfile), shell_path(disk))
            self.assertEqual(r.returncode, 0, r.stderr)
            table = r.stdout.decode()
            self.assertLess(len(table), 1024)  # Native getdiskbyname's entry buffer.
            fields = dict(re.findall(r':([a-z][a-z0-9])#(-?\d+)', table))
            sector_size = int(fields['ss'])
            self.assertEqual(sector_size, 1024)
            front = int(fields['fp'])
            self.assertEqual(front * sector_size, 160 * 1024)
            self.assertEqual(int(fields['nt']) * int(fields['ns']) * sector_size, 1024**2)
            self.assertEqual(int(fields['z0']) * sector_size, 66 * 512)
            self.assertEqual(int(fields['z1']) * sector_size, 194 * 512)
            end = 1
            count = 0
            for letter in 'abcdefgh':
                base, size = int(fields.get('p' + letter, -1)), int(fields.get('s' + letter, -1))
                if size == -1:
                    self.assertEqual(base, -1)
                    continue
                self.assertEqual(base, end)
                self.assertEqual(int(fields['b' + letter]), 8192)
                self.assertEqual(int(fields['f' + letter]), 1024)
                self.assertEqual(int(fields['d' + letter]), 4096)
                self.assertIn(':i' + letter + ':', table)
                end += size
                count += 1
            self.assertLessEqual(count, 7)
            self.assertNotIn('sh', fields)
            data = disk.read_bytes()
            self.assertEqual(data[:446], boot[:446])
            self.assertEqual(data[446:454], bytes.fromhex('80000300a7feffff'))
            start, length = struct.unpack_from('<II', data, 454)
            self.assertEqual(start, 2)
            self.assertEqual((start + length) * 512, (front + end) * sector_size)
            self.assertLessEqual(start + length, sectors)
            self.assertEqual(data[462:510], bytes(48))
            self.assertEqual(data[510:512], b'\x55\xaa')
            self.assertEqual(data[512:66 * 512], bytes(65 * 512))
            self.assertEqual(data[66 * 512:], b'x' * (62 * 512))

    def test_bad_boot_template_and_read_only_target_fail(self):
        r = self.run_perl('print mbr("bad", layout(8388608));')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b'')
        target = self.work / 'readonly'
        target.write_bytes(b'untouched')
        r = self.run_perl('open(D,"<$ARGV[0]") or die; write_layout(D,"overwrite");', shell_path(target))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(target.read_bytes(), b'untouched')

    def test_post_format_verification_rejects_lost_mbr_and_changed_labels(self):
        bootfile = self.work / 'boot1'
        bootfile.write_bytes(bytes(510) + b'\x55\xaa')
        r = self.run_perl('''open(B,"<$ARGV[0]") or die; binmode(B); read(B,$b,512);
                            binmode(STDOUT); print mbr($b,layout(500118192));''', shell_path(bootfile))
        self.assertEqual(r.returncode, 0, r.stderr)
        data = bytearray(r.stdout)
        for block in (17, 32, 47):
            start = block * 512
            data[start:start+4] = b'dlV3'
            struct.pack_into('>I', data, start+0x5c, 1024)
            struct.pack_into('>H', data, start+0x70, 160)
            struct.pack_into('>II', data, start+0x7c, 33, 97)
            data[start+0xbc] = ord('a')
            for i in range(8):
                struct.pack_into('>ii', data, start+0xbe+46*i,
                                 1+4194176*i if i < 7 else -1, 4194176 if i < 7 else -1)
        disk = self.work / 'formatted'
        for offset in (None, 446, 17*512, 17*512+0x5c, 17*512+0x70,
                       17*512+0x7c, 17*512+0x80, 32*512+0xbe+46*4, 47*512+0xbe+46*7):
            damaged = bytearray(data)
            if offset is not None:
                damaged[offset] ^= 1
            disk.write_bytes(damaged)
            result = self.run_perl('''open(B,"<$ARGV[0]") or die; binmode(B); read(B,$b,512);
                open(D,"<$ARGV[1]") or die; binmode(D); verify_layout(D,$b,layout(500118192));''',
                shell_path(bootfile), shell_path(disk))
            self.assertEqual(result.returncode == 0, offset is None, result.stderr)
            self.assertEqual(disk.read_bytes(), damaged)

    def test_real_device_entrypoint_rejects_regular_files(self):
        target = self.work / 'scratch'
        target.write_bytes(b'untouched')
        r = subprocess.run([self.perl, shell_path(self.helper), 'prepare', shell_path(target), 'boot1'],
                           capture_output=True, timeout=10)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(target.read_bytes(), b'untouched')

    def test_prepare_works_when_file_creation_is_read_only(self):
        bootfile = self.work / 'boot1'
        bootfile.write_bytes(bytes(510) + b'\x55\xaa')
        target = self.work / 'disk'
        target.write_bytes(b'x' * (128 * 512))
        mounts = self.work / 'mounts'
        mounts.write_bytes(b'')
        self.helper.write_text(self.helper.read_text().replace(
            '"/usr/etc/mount |"', '"<' + shell_path(mounts) + '"'))
        r = self.run_perl('''
            *disk_sectors = sub { return 8388608; };
            main();
        ''', 'prepare', shell_path(target), shell_path(bootfile), prelude='''
            sub CORE::GLOBAL::open (*;$) {
                die "Read-only file system: $_[1]\\n" if $_[1] =~ /^>/;
                return CORE::open($_[0], $_[1]);
            }
        ''')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b'1\n')
        data = target.read_bytes()
        self.assertEqual(data[446:454], bytes.fromhex('80000300a7feffff'))
        self.assertEqual(struct.unpack_from('<II', data, 454), (2, 8388606))
        self.assertEqual(data[510:512], b'\x55\xaa')
        self.assertEqual(data[512:66*512], bytes(65*512))
        self.assertEqual(data[66*512:], b'x' * (62*512))

    def test_formatter_receives_pipe_and_reports_exec_or_child_failure(self):
        for mode in ('format', 'boot'):
            with self.subTest(mode=mode):
                self.check_disk_pipe(mode)

    def check_disk_pipe(self, mode):
        formatter = self.work / 'formatter'
        for status in (0, 7, None):
            with self.subTest(status=status):
                if status is None:
                    formatter.unlink()
                else:
                    formatter.write_text('#!/usr/bin/perl\n'
                        'print join(" ", @ARGV), "\\n";\n'
                        'print while <STDIN>;\nexit ' + str(status) + ';\n')
                    formatter.chmod(0o755)
                r = self.run_perl('''
                    *disk_sectors = sub { return 500118192; };
                    main(); print "DONE\\n";
                ''', mode, '/dev/rsd0h', shell_path(formatter))
                self.assertEqual(r.returncode == 0, status == 0, r.stderr)
                self.assertEqual(b'DONE\n' in r.stdout, status == 0)
                if status is not None:
                    expected = self.run_perl('print disktab(layout(500118192));')
                    action = '-i' if mode == 'format' else '-b'
                    self.assertEqual(r.stdout, f'-t quickstep -N {action} -u /dev/rsd0h\n'.encode()
                                     + expected.stdout + (b'DONE\n' if status == 0 else b''))
                else:
                    self.assertIn(b'Cannot execute disk formatter', r.stderr)


class NativeDiskCopyTests(unittest.TestCase):
    def load_disk(self):
        source = os.environ.get('INSTALLER_TEST_DISK')
        if not source or unicorn is None:
            self.skipTest('INSTALLER_TEST_DISK and Unicorn required')
        binary = installer.layout_disk(Path(source).read_bytes())
        uc = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_32)
        uc.mem_map(0x1000, 0x100000)
        uc.mem_map(0x1000000, 0x10000)
        pos = 28
        for _ in range(struct.unpack_from('<I', binary, 16)[0]):
            command, size = struct.unpack_from('<II', binary, pos)
            if command == 1:
                _, _, _, va, _, off, length, *_ = struct.unpack_from('<II16s8I', binary, pos)
                if length:
                    uc.mem_write(va, binary[off:off+length])
            elif command == 2:
                symoff, count, stroff, _ = struct.unpack_from('<4I', binary, pos+8)
            pos += size
        symbols = {}
        for i in range(count):
            index, kind, _, _, value = struct.unpack_from('<IBBHI', binary, symoff+12*i)
            if kind & 0xe0 or not value:
                continue
            name = binary[stroff+index:binary.index(b'\0', stroff+index)].decode('ascii')
            symbols[name] = value
        return uc, symbols

    def test_known_disk_copy_changes_only_three_instructions(self):
        path = os.environ.get('INSTALLER_TEST_DISK')
        if not path:
            self.skipTest('INSTALLER_TEST_DISK required')
        source = Path(path).read_bytes()
        thin = source
        if source[:4] == bytes.fromhex('cafebabe'):
            for i in range(struct.unpack_from('>I', source, 4)[0]):
                cpu, _, offset, size, _ = struct.unpack_from('>5I', source, 8 + 20*i)
                if cpu == 7:
                    thin = source[offset:offset+size]
        copied = installer.layout_disk(source)
        self.assertEqual(len(copied), len(thin))
        changed = [i for i, (old, new) in enumerate(zip(thin, copied)) if old != new]
        self.assertEqual(len(changed), 14)
        self.assertEqual(changed[-1] - changed[-5], 4)  # Five-byte open call.
        self.assertNotIn(b'/tmp/disktab', copied)
        self.assertNotIn(b'/tmp/qsboot1', copied)
        self.assertEqual(installer.layout_disk(thin), copied)
        with self.assertRaises(ValueError):
            installer.layout_disk(thin[:-1] + bytes([thin[-1] ^ 1]))

    def test_unknown_disk_copy_is_rejected(self):
        for data in (b'', b'/etc/disktab\0', bytes.fromhex('cafebabe'),
                     bytes.fromhex('cafebabe00000001')):
            with self.assertRaises(ValueError):
                installer.layout_disk(data)

    def test_native_disktab_parser_and_label_preserve_all_seven_volumes(self):
        source = os.environ.get('INSTALLER_TEST_DISK')
        perl = 'C:/msys64/usr/bin/perl.exe' if os.name == 'nt' else shutil.which('perl')
        if not source or unicorn is None or not perl:
            self.skipTest('INSTALLER_TEST_DISK, Unicorn and Perl required')
        # Run the real Intel disk utility's parser and label constructor. Only
        # its three file syscalls are intercepted; parsing and arithmetic run
        # unmodified, including the 1024-byte disktab entry buffer.
        for sectors in (2**23, 10 * 2**21, 500118192):
            with self.subTest(sectors=sectors):
                helper = shell_path(Path(installer.__file__).with_name('installer-layout.pl'))
                r = subprocess.run([perl, '-e', f'require "{helper}"; print disktab(layout($ARGV[0]));',
                                    str(sectors)], capture_output=True, timeout=10)
                self.assertEqual(r.returncode, 0, r.stderr)
                table = r.stdout
                uc, symbols = self.load_disk()
                def word(address):
                    return struct.unpack('<I', uc.mem_read(address, 4))[0]
                def put(address, value):
                    uc.mem_write(address, struct.pack('<I', value))
                cursor = 0
                closes = []
                def hook(machine, address, size, _):
                    nonlocal cursor
                    sp = machine.reg_read(x86.UC_X86_REG_ESP)
                    if address == symbols['_open']:
                        self.fail('disktab parser attempted to open a file')
                    elif address == symbols['_read']:
                        self.assertEqual(word(sp+4), 0)
                        # Pipes may return less than the requested buffer size.
                        data = table[cursor:cursor+min(word(sp+12), 17)]
                        machine.mem_write(word(sp+8), data)
                        cursor += len(data)
                        value = len(data)
                    elif address == symbols['_close']:
                        closes.append(word(sp+4))
                        value = 0
                    else:
                        return
                    machine.reg_write(x86.UC_X86_REG_EAX, value)
                    machine.reg_write(x86.UC_X86_REG_EIP, word(sp))
                    machine.reg_write(x86.UC_X86_REG_ESP, sp+4)
                uc.hook_add(unicorn.UC_HOOK_CODE, hook)
                def call(name, *args):
                    sp = 0x100f000
                    uc.mem_write(sp, struct.pack('<'+'I'*(len(args)+1), 0xf1000, *args))
                    uc.reg_write(x86.UC_X86_REG_ESP, sp)
                    uc.emu_start(symbols[name], 0xf1000, count=1000000)
                    self.assertEqual(uc.reg_read(x86.UC_X86_REG_EIP), 0xf1000)
                    return uc.reg_read(x86.UC_X86_REG_EAX)
                uc.mem_write(0xf0000, b'quickstep\0')
                dt = call('_getdiskbyname', 0xf0000)
                self.assertNotEqual(dt, 0)
                self.assertEqual(word(dt+0x30), 1024)
                self.assertEqual(closes, [0])
                put(symbols['_dt'], dt)
                call('_make_new_label')
                label = symbols['_disk_label']
                self.assertEqual(word(label+44+0x30), 1024)
                self.assertEqual(bytes(uc.mem_read(label+44, 0x70)), bytes(uc.mem_read(dt, 0x70)))
                # make_new_label supplies a default hostname at dt+0x70.
                self.assertEqual(bytes(uc.mem_read(label+44+0x90, 532-0x90)),
                                 bytes(uc.mem_read(dt+0x90, 532-0x90)))
                fields = dict(re.findall(rb':([a-z][a-z0-9])#(-?\d+)', table))
                for i, letter in enumerate(b'abcdefgh'):
                    p = dt + 0x94 + 48*i
                    key = bytes([letter])
                    expected_size = int(fields.get(b's'+key, -1))
                    self.assertEqual(word(p), int(fields.get(b'p'+key, -1)) & 0xffffffff)
                    self.assertEqual(word(p+4), expected_size & 0xffffffff)
                    self.assertEqual(uc.mem_read(p+19, 1)[0], int(expected_size > 0))

                # EOF from a failed producer must reject the layout.
                table = b''
                cursor = 0
                self.assertEqual(call('_getdiskbyname', 0xf0000), 0)

    def test_native_secondary_loaders_are_written_without_overwriting_mbr(self):
        self.check_native_boot_write(from_main=False)

    def test_native_boot_update_command_preserves_mbr_labels_and_filesystems(self):
        self.check_native_boot_write(from_main=True)

    def test_native_physical_dos_base_reproduces_label_overlap_error(self):
        with self.assertRaisesRegex(AssertionError, 'boot block overlays labels'):
            self.check_native_boot_write(from_main=False, dosbase=2)

    def check_native_boot_write(self, *, from_main, dosbase=0):
        uc, symbols = self.load_disk()
        boot = b'loader!!' * 1024
        disk = bytearray(b'x' * (256 * 1024))
        original = bytes(disk)
        cursor = 0
        writes = []
        opens = []

        def word(address):
            return struct.unpack('<I', uc.mem_read(address, 4))[0]

        def put(address, value):
            uc.mem_write(address, struct.pack('<I', value))

        def hook(machine, address, size, _):
            nonlocal cursor
            sp = machine.reg_read(x86.UC_X86_REG_ESP)
            if address == symbols['_malloc']:
                value = 0x80000
            elif address in (symbols['_free'], symbols['_printf'], symbols['_close'],
                             symbols['_init_arch_info'], symbols['_openlog'], symbols['_closelog']):
                value = 0
            elif address in (symbols['_init'], symbols['_Format'], symbols['_uses_fdisk'],
                             symbols['_sd_inferdisktab'], symbols['_hd_inferdisktab']):
                self.fail('boot update attempted formatting or DOS partition inference')
            elif address in (symbols['_bomb'], symbols['_dpanic']):
                message = bytes(machine.mem_read(word(sp+8), 128)).split(b'\0')[0]
                self.assertEqual(writes, [])
                self.fail('native boot writer rejected the prepared layout: ' + repr(message))
            elif address == symbols['_exit']:
                self.assertEqual(word(sp+4), 0)
                machine.emu_stop()
                return
            elif address == symbols['_getdiskbyname']:
                self.assertEqual(bytes(machine.mem_read(word(sp+4), 10)), b'quickstep\0')
                value = symbols['_disk_label'] + 44
            elif address == symbols['_ioctl']:
                request = word(sp+8)
                if request == 0x40306405:  # DKIOCINFO: physical sector size.
                    put(word(sp+12)+0x28, 512)
                else:
                    self.assertEqual(request, 0x20006400)  # DKIOCGLABEL
                    self.assertEqual(word(sp+12), symbols['_disk_label'])
                value = 0
            elif address == symbols['_open']:
                path = bytes(machine.mem_read(word(sp+4), 64)).split(b'\0')[0]
                opens.append(path)
                self.assertIn(path, (b'/secondary-loader', b'/dev/rsd0h', b'/dev/kmem'))
                value = {b'/secondary-loader': 10, b'/dev/kmem': 11, b'/dev/rsd0h': 12}[path]
            elif address == symbols['_read']:
                fd = word(sp+4)
                self.assertIn(fd, (10, 11))
                data = boot[cursor:cursor+word(sp+12)] if fd == 10 else bytes(word(sp+12))
                machine.mem_write(word(sp+8), data)
                if fd == 10:
                    cursor += len(data)
                value = len(data)
            elif address == symbols['_lseek']:
                self.assertIn((word(sp+4), word(sp+8), word(sp+12)), ((10, 0, 0), (11, 0x11000, 0)))
                cursor = value = 0
            elif address == 0xf2100:  # Driver's read-label callback.
                value = 0
            elif address == 0xf2200:  # Driver's read/write callback.
                self.assertEqual(word(sp+4), 2)  # CMD_WRITE
                block, pointer, length = (word(sp+i) for i in (8, 12, 16))
                writes.append(block)
                disk[block*512:block*512+length] = machine.mem_read(pointer, length)
                value = 0
            else:
                return
            machine.reg_write(x86.UC_X86_REG_EAX, value)
            machine.reg_write(x86.UC_X86_REG_EIP, word(sp))
            machine.reg_write(x86.UC_X86_REG_ESP, sp+4)

        uc.hook_add(unicorn.UC_HOOK_CODE, hook)
        uc.mem_write(0xf3000, b'/secondary-loader\0')
        put(symbols['_bootfile'], 0xf3000)
        if not from_main:
            put(symbols['_do_boot'], 1)
            put(symbols['_do_boot0'], 1)
            put(symbols['_do_boot1'], 1)
        put(symbols['_dosdisk'], 0)
        put(symbols['_dosbase'], dosbase)
        put(symbols['_interactive'], 0)
        put(symbols['_devblklen'], 512)
        put(symbols['_dsp'], 0xf2000)
        put(0xf2004, 0xffffffff)  # Stub driver supports the boot command.
        put(0xf2028, 0xf2100)
        put(0xf2010, 0xf2200)
        label = symbols['_disk_label']
        put(label+44+0x30, 1024)
        uc.mem_write(label+44+0x44, struct.pack('<H', 160))
        put(label+44+0x50, 33)
        put(label+44+0x54, 97)
        put(0x100f000, 0xf1000)
        if from_main:
            args = [b'layout-disk', b'-t', b'quickstep', b'-N', b'-b', b'-u', b'/dev/rsd0h']
            for i, arg in enumerate(args):
                uc.mem_write(0xf4000+128*i, arg+b'\0')
                put(0xf5000+4*i, 0xf4000+128*i)
            put(0x100f004, len(args))
            put(0x100f008, 0xf5000)
        uc.reg_write(x86.UC_X86_REG_ESP, 0x100f000)
        uc.emu_start(symbols['_main' if from_main else '_boot'], 0xf1000, count=1000000)
        self.assertEqual(uc.reg_read(x86.UC_X86_REG_EIP), symbols['_exit'] if from_main else 0xf1000)
        if not from_main:
            self.assertEqual(uc.reg_read(x86.UC_X86_REG_EAX), 0)
        self.assertEqual(opens, ([b'/dev/rsd0h', b'/dev/kmem', b'/dev/rsd0h'] if from_main else [])
                         + [b'/secondary-loader'])
        self.assertEqual(writes, [66, 194])
        expected = bytearray(original)
        for block in (66, 194):
            expected[block*512:block*512+len(boot)] = boot
        self.assertEqual(disk, expected)


if __name__ == '__main__':
    unittest.main()
