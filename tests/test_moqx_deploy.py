from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def sh(call, **env):
    result = subprocess.run(['bash', '-c', f'set -euo pipefail; source {ROOT}/lib/moqx.sh; {call}'],
                            capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', **env})
    return result.returncode, result.stdout.split()


def elf(machine):
    # 64-bit little-endian ELF header: e_ident, e_type, e_machine.
    return b'\x7fELF\x02\x01\x01' + b'\0' * 9 + struct.pack('<HH', 2, machine) + b'\0' * 44


class MoqxDeploy(unittest.TestCase):
    def test_host_arch(self):
        self.assertEqual(sh('moqx_arch aarch64'), (0, ['arm64']))
        self.assertEqual(sh('moqx_arch x86_64'), (0, ['amd64']))
        self.assertEqual(sh('moqx_arch riscv64')[0], 1)

    def test_asset(self):
        self.assertEqual(sh('moqx_asset arm64'), (0, ['moqx-linux-arm64.tar.gz']))
        self.assertEqual(sh('moqx_asset arm64', MOQX_ASSET='custom.tar.gz'), (0, ['custom.tar.gz']))

    def test_script_refs_for_ci_build(self):
        self.assertEqual(sh('moqx_script_refs v0.3.7-arm64-pgo-0783f501'),
                         (0, ['v0.3.7-arm64-pgo-0783f501', '0783f501', 'v0.3.7']))

    def test_script_refs_for_release(self):
        self.assertEqual(sh('moqx_script_refs v0.3.7'), (0, ['v0.3.7']))

    def test_script_refs_override_first(self):
        self.assertEqual(sh('moqx_script_refs v0.3.7', MOQX_SCRIPTS_REF='main'), (0, ['main', 'v0.3.7']))

    def test_elf_machine(self):
        with tempfile.TemporaryDirectory() as d:
            for machine, name in [(0xB7, 'arm64'), (0x3E, 'amd64')]:
                p = Path(d) / name
                p.write_bytes(elf(machine))
                self.assertEqual(sh(f'moqx_elf_arch {p}'), (0, [name]))
            (Path(d) / 'riscv').write_bytes(elf(0xF3))
            self.assertEqual(sh(f'moqx_elf_arch {d}/riscv')[0], 1)
            (Path(d) / 'text').write_text('#!/bin/sh\n')
            self.assertEqual(sh(f'moqx_elf_arch {d}/text')[0], 1)


def relay_cmd(**env):
    with tempfile.TemporaryDirectory() as d:  # no relays.local.sh
        return subprocess.run(['bash', '-c', f'source {ROOT}/lib/run.sh; relay_cmd moqx 8'], cwd=d,
                              capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', **env}).stdout


class MoqxRelayCommand(unittest.TestCase):
    def test_default_starts_releases_without_newer_keys(self):
        cmd = relay_cmd()
        self.assertNotIn('write_limit_rtt_fraction', cmd)
        self.assertNotIn('pacing_tick_interval_us', cmd)
        self.assertIn('num_gro_buffers: 1', cmd)
        self.assertIn('MALLOC_CONF=thp:always ', cmd)
        self.assertIn(' -j ', cmd)

    def test_opt_outs_and_injected_keys(self):
        cmd = relay_cmd(MOQX_THP='0', MOQX_JEMALLOC='0', MOQX_GRO='', MOQX_WLRF='0', MOQX_PTI='1000')
        self.assertNotIn('MALLOC_CONF', cmd)
        self.assertNotIn(' -j ', cmd)
        self.assertNotIn('num_gro_buffers', cmd)
        self.assertIn('write_limit_rtt_fraction: 0', cmd)
        self.assertIn('pacing_tick_interval_us: 1000', cmd)


if __name__ == '__main__':
    unittest.main()
