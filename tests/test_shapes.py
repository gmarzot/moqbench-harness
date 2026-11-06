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
                if 'aiomoqt_frames' in s:
                    i, p, b = map(int, s['aiomoqt_frames'].split())
                    mbps = 8 * (i + 19 * p + 40 * b) * 30 / n / 1e6  # ibp, 60 frames
                    self.assertLess(abs(mbps - s['nominal_mbps']) / s['nominal_mbps'], 0.01)
                else:
                    self.assertEqual(s['aiomoqt_profile'], name)

    def test_shape_field(self):
        sh = lambda call: subprocess.run(['bash', '-c', f'source {ROOT}/lib/shapes.sh; {call}'],  # noqa: E731
                                         capture_output=True, text=True)
        self.assertEqual(sh('shape_field 720p moqperf_other_object').stdout.split(), ['7931'])
        self.assertNotEqual(sh('shape_field 4k nominal_mbps').returncode, 0)

    def test_frames_wrapper_sets_the_profile_for_a_module_or_a_script(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'aiomoqt/tools').mkdir(parents=True)
            (root / 'aiomoqt/__init__.py').write_text('')
            (root / 'aiomoqt/tools/__init__.py').write_text('')
            (root / 'aiomoqt/track.py').write_text('class VideoTrack:\n    PROFILES = {}\n')
            bench = ('import sys\nfrom aiomoqt.track import VideoTrack\n'
                     'print(VideoTrack.PROFILES["4k"]["i_frame"], sys.argv[1:], __name__)\n')
            (root / 'aiomoqt/tools/adaptive_bench.py').write_text(bench)
            (root / 'patched.py').write_text(bench)
            for target in ('aiomoqt.tools.adaptive_bench', str(root / 'patched.py')):
                with self.subTest(target=target):
                    run = subprocess.run([sys.executable, str(ROOT / 'remote/adaptive-bench-frames.py'),
                                          '4k', '744000', '93000', '37200', target, '--role', 'sub'],
                                         env=dict(os.environ, PYTHONPATH=d), capture_output=True, text=True)
                    self.assertEqual(run.stdout.strip(), "744000 ['--role', 'sub'] __main__", run.stderr)

    def test_plan_points_name_known_tools_and_shapes(self):
        for plan in (ROOT / 'scenarios').glob('sweep-*.plan'):
            for line in plan.read_text().splitlines():
                line = line.split('#')[0].strip()
                if not line or re.fullmatch(r'[A-Z_][A-Z0-9_]*=\S*', line):
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
