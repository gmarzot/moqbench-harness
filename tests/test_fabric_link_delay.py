"""Static per-link delay: config, the tc commands it renders, and leftover checks that pass it."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
from fabric import faults  # noqa: E402
from fabric.docker import DockerFabric, apply_link_delays, link_delays  # noqa: E402
from fabric.vm import VMFabric  # noqa: E402

sys.path.insert(0, str(ROOT / 'tests'))
from test_fabric_vm import INV, Recorder  # noqa: E402
from test_fabric_vm import deployment as vm_deployment  # noqa: E402


def docker_deployment(**fabric):
    nodes = [dict(name=n, roles=['relay']) for n in ('r1', 'r2', 'r3')]
    nodes.append(dict(name='client', roles=['subscriber', 'publisher']))
    return config.Deployment('fab', dict(provider='docker', host='local', nodes=nodes,
                                         fabric={'order': ['r1', 'r2', 'r3'], **fabric}))


class Host:
    local = True

    def __init__(self, out='{"t0": 1.0, "t1": 1.5, "rc": 0}'):
        self.calls, self.out = [], out

    def sh(self, script, check=True, timeout=None):
        self.calls.append(script)
        return type('R', (), {'stdout': self.out, 'returncode': 0})()


def vm_fabric(**fabric):
    fab = VMFabric(vm_deployment(**fabric), inventory=INV)
    log = []
    fab.hostfor = lambda t: Recorder(log, t)
    return fab, log


class Config(unittest.TestCase):
    def test_default_is_none(self):
        spec = docker_deployment().fabric
        self.assertEqual(spec.link_delay('r1', 'r2'), 0)

    def test_every_link_and_overrides(self):
        spec = docker_deployment(link_delay_ms=10, link_delays=[['r3', 'r1', 2.5], ['r2', 'r3', 0]]).fabric
        self.assertEqual(spec.link_delay('r1', 'r2'), 10)
        self.assertEqual(spec.link_delay('r1', 'r3'), 2.5)
        self.assertEqual(spec.link_delay('r3', 'r2'), 0)

    def test_validation(self):
        for bad in (dict(link_delay_ms=0), dict(link_delay_ms=-1), dict(link_delay_ms='10ms'),
                    dict(link_delay_ms=True), dict(link_delay_ms=20000),
                    dict(link_delays=[['r1', 'r2']]), dict(link_delays=[['r1', 'r1', 5]]),
                    dict(link_delays=[['r1', 'r9', 5]]), dict(link_delays=[['r1', 'r2', -5]]),
                    dict(link_delays=[['r1', 'r2', 5], ['r2', 'r1', 6]])):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, 'link_delay'):
                docker_deployment(**bad)


class Commands(unittest.TestCase):
    def test_spec(self):
        self.assertEqual(faults.delay_spec(10), 'delay 10ms')
        self.assertEqual(faults.delay_spec(2.5), 'delay 2.5ms')
        self.assertEqual(faults.with_delay('loss 1%', 10), 'delay 10ms loss 1%')
        self.assertEqual(faults.with_delay('delay 30ms 5ms', 10), 'delay 30ms 5ms')
        self.assertEqual(faults.with_delay('loss 1%', 0), 'loss 1%')

    def test_docker_both_ends_of_every_link(self):
        fab = DockerFabric(docker_deployment(link_delay_ms=10, link_delays=[['r1', 'r3', 0]]))
        fab.host = Host()
        self.assertEqual(link_delays(fab), {('r1', 'r2'): 10, ('r2', 'r3'): 10})
        apply_link_delays(fab)
        self.assertEqual(len(fab.host.calls), 4)
        self.assertTrue(all('tc qdisc replace dev "$dev" root netem delay 10ms limit 200000' in c
                            for c in fab.host.calls))
        self.assertIn('mb-fab-r1 ', fab.host.calls[0])
        self.assertIn('10.77.1.11', fab.host.calls[0])
        self.assertIn('mb-fab-r2 ', fab.host.calls[1])
        self.assertIn('10.77.1.12', fab.host.calls[1])

    def test_docker_events_keep_the_delay(self):
        fab = DockerFabric(docker_deployment(link_delay_ms=10))
        fab.host = Host()
        faults.apply(fab, dict(verb='link.impair', on='r1', peer='r2', netem='loss 1%', direction='on'))
        self.assertIn('root netem delay 10ms loss 1% limit', fab.host.calls[-1])
        faults.apply(fab, dict(verb='link.clear', on='r1', peer='r2', direction='on'))
        self.assertIn('root netem delay 10ms limit', fab.host.calls[-1])
        self.assertNotIn('qdisc del', fab.host.calls[-1])

    def test_vm_sending_side_of_every_pair(self):
        fab, log = vm_fabric(link_delay_ms=10)
        apply_link_delays(fab)
        self.assertEqual(len(log), 6)
        target, script = log[0]
        self.assertEqual(target, 'r1')
        self.assertIn('tc qdisc replace dev mbo2 root netem delay 10ms limit 200000', script)
        self.assertIn('egress pref 102 protocol ip flower dst_ip 10.42.1.12 action mirred egress redirect dev mbo2',
                      script)
        self.assertEqual(sorted(t for t, _ in log), ['r1', 'r1', 'r2', 'r2', 'r3', 'r3'])

    def test_vm_events_keep_the_delay(self):
        fab, log = vm_fabric(link_delay_ms=10)
        faults.apply(fab, dict(verb='link.impair', on='r1', peer='r2', netem='loss 1%', direction='on'))
        self.assertIn('root netem delay 10ms loss 1% limit', log[-1][1])
        faults.apply(fab, dict(verb='link.clear', on='r1', peer='r2', direction='on'))
        self.assertIn('root netem delay 10ms limit', log[-1][1])
        faults.apply(fab, dict(verb='link.impair', on='r1', peer='r2', netem='loss 1%', direction='on',
                               side='receive'))
        self.assertIn('dev mbi1 root netem loss 1% limit', log[-1][1])

    def test_vm_down_clears_the_delay(self):
        fab, log = vm_fabric(link_delay_ms=10)
        fab.clear_impairments(keep_delays=False)
        self.assertFalse([s for _, s in log if 'netem' in s and 'replace' in s])
        log.clear()
        fab.clear_impairments()
        self.assertEqual(len([s for _, s in log if 'root netem delay 10ms' in s]), 6)


class Leftovers(unittest.TestCase):
    DOCKER = ['qdisc netem 8001: dev eth1 root refcnt 2 limit 200000 delay 10ms',
              'qdisc netem 8002: dev eth2 root refcnt 2 limit 200000 delay 10.0ms']

    def test_docker_delay_is_not_left_over(self):
        self.assertEqual(faults.residual(self.DOCKER, {10}), [])
        self.assertEqual(faults.residual(self.DOCKER, set()), self.DOCKER)
        lossy = ['qdisc netem 8001: dev eth1 root refcnt 2 limit 200000 delay 10ms loss 1%']
        self.assertEqual(faults.residual(lossy, {10}), lossy)

    def test_vm_delay_is_not_left_over(self):
        lines = ['ifb mbo2', 'ifb mbo3', 'egress filter on ens3',
                 'qdisc netem 8001: dev mbo2 root refcnt 2 limit 200000 delay 10ms seed 123',
                 'qdisc netem 8002: dev mbo3 root refcnt 2 limit 200000 delay 10ms loss 100%']
        self.assertEqual(faults.residual(lines, (), {'mbo2': 10, 'mbo3': 10}), [lines[4]])
        self.assertEqual(faults.residual(lines, (), {}), lines)
        self.assertEqual(faults.residual(['ifb mbi1', 'ingress filter on ens3'], (), {'mbo2': 10}),
                         ['ifb mbi1', 'ingress filter on ens3'])

    def test_vm_ifbs_per_node(self):
        fab, _ = vm_fabric(link_delay_ms=10, link_delays=[['r1', 'r3', 0]])
        self.assertEqual(fab.delay_ifbs('r1'), {'mbo2': 10})
        self.assertEqual(fab.delay_ifbs('r2'), {'mbo1': 10, 'mbo3': 10})


if __name__ == '__main__':
    unittest.main()
