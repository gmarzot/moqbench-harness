"""relay-restart: the recovers verdict per subscriber, and the scenarios."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from fabric import faults, report  # noqa: E402
from fabric.run import load_scenario  # noqa: E402


def write_restarts(folder, r1_until=70):
    """r1 - r2 - r3 at 10 objects/s, publisher on r1: r3 restarts, then r2, then r1."""
    folder.mkdir(parents=True)
    meta = dict(label='t', order=['r1', 'r2', 'r3'], links=[['r1', 'r2'], ['r2', 'r3']], namespace='fab-x',
                scenario={'publisher': 'ingest'}, test={'scenario': 'relay-restart', 'topology': 'line'},
                options={'rate': 10, 'group_size': 10, 'object_size': 100},
                addressing={'access': {'hosts': {}}, 'links': []})
    (folder / 'meta.json').write_text(json.dumps(meta))
    pub = [f'{100 + i * 0.1:.6f} x TRACE: MOQT send: object {i // 10}.{i % 10} alias=0 100 bytes' for i in range(200)]
    (folder / 'pub.log').write_text('\n'.join(pub) + '\n')
    for node, until in (('r1', r1_until), ('r2', 50), ('r3', 20)):
        rows = [f'{100 + i * 0.1 + 0.0005:.6f} x TRACE: MOQT stream(3): object {i // 10}.{i % 10} alias=0 113 bytes '
                'status=0' for i in range(until)]
        (folder / f'sub-{node}.log').write_text('\n'.join(rows) + '\n')
    events = [dict(verb=v, on=n, t0=t, t1=t + 0.05, ok=True, expect='recovers')
              for v, n, t in (('node.stop', 'r3', 102.05), ('node.start', 'r3', 103.05), ('node.stop', 'r2', 105.05),
                              ('node.start', 'r2', 106.05), ('node.stop', 'r1', 110.05))]
    (folder / 'faults.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n')


class Recovers(unittest.TestCase):
    def verdicts(self, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            write_restarts(Path(tmp) / 'run', **kw)
            return [(v['check'], v['status'], v.get('reason')) for v in report.analyze(Path(tmp) / 'run')['verdicts']
                    if 'recovers' in v['check']]

    def test_per_subscriber(self):
        got = {c: (s, r) for c, s, r in self.verdicts(r1_until=100)}
        self.assertEqual(got['sub-r1 node.stop r3: recovers without a reconnect'][0], 'PASS')
        self.assertEqual(got['sub-r2 node.start r3: recovers without a reconnect'][0], 'PASS')
        self.assertEqual(got['sub-r3 node.stop r3: recovers without a reconnect'],
                         ('SKIP', 'its own relay restarted: the subscriber reconnects'))
        self.assertEqual(got['sub-r2 node.stop r2: recovers without a reconnect'][0], 'SKIP')
        self.assertNotIn('sub-r3 node.stop r2: recovers without a reconnect', got)
        self.assertEqual(got['sub-r1 node.start r2: recovers without a reconnect'][0], 'PASS')
        self.assertTrue(all(got[f'sub-{n} node.stop r1: recovers without a reconnect'][1].startswith("the publisher's")
                            for n in ('r1', 'r2', 'r3')))

    def test_delivery_that_stops_for_good_does_not_recover(self):
        got = {c: s for c, s, _ in self.verdicts(r1_until=70)}
        self.assertEqual(got['sub-r1 node.stop r2: recovers without a reconnect'], 'PASS')
        self.assertEqual(got['sub-r1 node.start r2: recovers without a reconnect'], 'FAIL')

    def test_scenarios(self):
        for name, verb in (('relay-restart', 'node.stop'), ('relay-restart-kill', 'node.kill')):
            scen = load_scenario(name)
            faults.validate(scen['event'], ['a', 'b', 'c'])
            self.assertEqual([(e['verb'], e['on']) for e in scen['event']],
                             [(verb, 'transit'), ('node.start', 'transit'), (verb, 'egress'), ('node.start', 'egress'),
                              (verb, 'ingest'), ('node.start', 'ingest')])
            self.assertTrue(all(e['expect'] == 'recovers' for e in scen['event']))


if __name__ == '__main__':
    unittest.main()
