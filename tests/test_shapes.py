import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHAPES = json.loads((ROOT / 'scenarios/shapes.json').read_text())


class Shapes(unittest.TestCase):
    def test_moqperf_sizes_match_nominal_bitrate(self):
        self.assertEqual(sorted(SHAPES), ['1080p', '480p', '720p'])
        for name, s in SHAPES.items():
            with self.subTest(shape=name):
                n = s['objects_per_group']
                group_bits = 8 * (s['moqperf_first_object'] + (n - 1) * s['moqperf_other_object'])
                mbps = group_bits * 30 / n / 1e6
                self.assertLess(abs(mbps - s['nominal_mbps']) / s['nominal_mbps'], 0.01)
                self.assertEqual(s['aiomoqt_profile'], name)

    def test_shape_field(self):
        sh = lambda call: subprocess.run(['bash', '-c', f'source {ROOT}/lib/shapes.sh; {call}'],  # noqa: E731
                                         capture_output=True, text=True)
        self.assertEqual(sh('shape_field 720p moqperf_other_object').stdout.split(), ['7931'])
        self.assertNotEqual(sh('shape_field 4k nominal_mbps').returncode, 0)


if __name__ == '__main__':
    unittest.main()
