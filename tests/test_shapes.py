import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402

os.environ.pop('MOQBENCH_CONFIG', None)
SHAPES = json.loads((ROOT / 'scenarios/shapes.json').read_text())


class Shapes(unittest.TestCase):
    def test_moqperf_sizes_match_nominal_bitrate(self):
        self.assertEqual(sorted(SHAPES), ['1080p', '2160p', '480p', '720p'])
        for name, s in SHAPES.items():
            with self.subTest(shape=name):
                n = s['objects_per_group']
                group_bits = 8 * (s['moqperf_first_object'] + (n - 1) * s['moqperf_other_object'])
                mbps = group_bits * 30 / n / 1e6
                self.assertLess(abs(mbps - s['nominal_mbps']) / s['nominal_mbps'], 0.01)
                self.assertEqual(s['aiomoqt_profile'], {'2160p': '4kuhd'}.get(name, name))

    def test_shape_field(self):
        sh = lambda call: subprocess.run(['bash', '-c', f'source {ROOT}/lib/shapes.sh; {call}'],  # noqa: E731
                                         capture_output=True, text=True)
        self.assertEqual(sh('shape_field 720p moqperf_other_object').stdout.split(), ['7931'])
        self.assertNotEqual(sh('shape_field 4k nominal_mbps').returncode, 0)

    def test_plan_points_name_known_tools_and_shapes(self):
        plans = [config.load_plan(path.stem) for path in sorted((ROOT / 'plans').glob('*.toml'))]
        self.assertTrue(plans)
        for plan in plans:
            for line in (line for suite in plan.suites for line in plan.sweep_text(suite).splitlines()):
                line = line.split('#')[0].strip()
                if not line or re.match(r'[A-Z_][A-Z0-9_]*=', line):
                    continue
                with self.subTest(plan=plan.name, line=line):
                    tool, _build, shape, per_box, seconds, _run = line.split()
                    self.assertIn(tool, ('aio', 'mp'))
                    self.assertIn(shape, SHAPES)
                    self.assertGreater(int(per_box), 0)
                    self.assertGreater(int(seconds), 0)

    def test_sweep_keeps_the_relay_name_apart_from_its_address(self):
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / 'sweep-x.plan'
            plan.write_text('aio pgo 1080p 10 1 x\n')
            env = dict(os.environ, RELAY_HOST='relay.invalid', CLIENT_HOSTS='a.invalid',
                       MOQX_PGO_TARBALL=str(Path(d) / 'none.tar.gz'))
            env.pop('RELAY', None)
            run = subprocess.run([str(ROOT / 'bin/sweep.sh'), str(plan), f'test-{os.getpid()}'], env=env, cwd=d,
                                 capture_output=True, text=True, timeout=60)
            shutil.rmtree(ROOT / f'results/sweep-test-{os.getpid()}', ignore_errors=True)
            self.assertNotIn('takes src', run.stdout + run.stderr)

    def test_sub_hosts_refuses_a_load_host_that_does_not_exist(self):
        env = dict(os.environ, RELAY_HOST='relay.invalid', CLIENT_HOSTS='a.invalid b.invalid', SUB_HOSTS='client:3')
        run = subprocess.run([str(ROOT / 'bin/relay-load.sh'), 'adaptive', 'moqx', '1'], env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertIn('there are 2 load hosts', run.stderr)


if __name__ == '__main__':
    unittest.main()
