"""Contribution mode: profiles, configuration, topology, lanes and the emulator, without Docker."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
import argparse  # noqa: E402
from contrib import calibrate, cli, emu, lanes, profiles, sources, spec, topology  # noqa: E402

# Mean forward loss in percent each profile is built for.
TARGET_LOSS = {'P0': 0.0, 'P1': 0.02, 'P2': 0.1, 'P3': 0.5, 'P4': 0.3, 'P5': 1.0, 'P6': 3.0, 'P7': 2.0, 'P8': 0.4,
               'P9': 0.5, 'REF': 0.5}


def make_test(**over):
    data = dict(type='contribution', lanes=['srt', 'rist', 'moqt-moq2ts', 'moqt-aiomoqt-loc'], profiles=['P3'])
    data.update(over)
    return spec.Test(data, 'test', {})


def ctx(lane_variant, placement='last-mile', rtt=80, features=None, playout=(300, 200)):
    return lanes.Ctx(paths=topology.Paths('10.79.0.0/16'), placement=placement, tier=sources.TIERS['mpeg2-20m'],
                     rtt_ms=rtt, variant=lane_variant, playout_ms=list(playout), slot_max_ms=16,
                     features=features or {})


class Profiles(unittest.TestCase):
    def test_every_profile_loads_and_meets_its_loss_target(self):
        names = profiles.names()
        self.assertEqual(set(TARGET_LOSS), set(names))
        for name in names:
            with self.subTest(profile=name):
                p = profiles.load(name)
                self.assertAlmostEqual(p.forward.mean_loss_pct(), TARGET_LOSS[name], delta=0.06 * max(1, TARGET_LOSS[name]))
                self.assertNotEqual(p.seeds(0)[0], p.seeds(0)[1])

    def test_ge_helpers(self):
        self.assertAlmostEqual(profiles.ge_mean(0.17, 33, 100, 0), 0.5126, places=3)
        p, r, bad, good = profiles.ge_for(0.5, 3)
        self.assertAlmostEqual(profiles.ge_mean(p, r, bad, good), 0.5, places=3)
        self.assertAlmostEqual(100 / r, 3, places=3)

    def test_tree_keeps_order_and_restates_everything(self):
        p = profiles.load('P3')
        cmds = p.forward.build('vb0', 20e6, seed=7)
        self.assertTrue(cmds[0].startswith('tc qdisc del dev vb0 root'))
        root = cmds[1]
        for part in ('handle 1: netem', 'limit 100000', 'delay 40ms', 'slot 0ms 16ms', 'rate 1gbit',
                     'loss gemodel 0.17% 33% 100% 0%', 'seed 7'):
            self.assertIn(part, root)
        self.assertIn('parent 1:1 handle 10: netem', cmds[2])
        self.assertIn('reorder 0.01%', cmds[2])
        self.assertIn('parent 10:1 handle 20: tbf rate 30', cmds[3])
        change = p.forward.change_delay('vb0', 48, seed=7)
        self.assertTrue(change.startswith('tc qdisc change dev vb0 root handle 1: netem'))
        self.assertIn('delay 48ms', change)
        self.assertIn('loss gemodel', change)
        self.assertIn('seed 7', change)
        self.assertIn('tbf rate', p.forward.change_rate('vb0', 20e6, 0.5))

    def test_cap_is_a_multiple_of_the_wire_rate(self):
        p = profiles.load('P3')
        self.assertAlmostEqual(p.forward.cap_bps(20e6), 1.5 * 20e6 * 1358 / 1316)
        self.assertIsNone(profiles.load('REF').forward.cap_bps(20e6))

    def test_reference_profile(self):
        p = profiles.load('REF')
        self.assertEqual(p.forward.netem_args(), 'limit 100000 delay 50ms rate 1gbit loss random 0.5% seed 2001')
        self.assertEqual(p.reverse.mean_loss_pct(), 0.0)

    def test_timeline_expands_repeats_and_ladders(self):
        p6 = profiles.load('P6')
        acts = p6.timeline(300, start_s=10)
        outages = [a for a in acts if a[1] == 'outage_on']
        self.assertEqual(sum(1 for a in outages if a[2]['name'].startswith('outage-1600ms')), 2)
        self.assertTrue(all(10 <= t < 310 + 2 for t, _, _ in acts))
        self.assertEqual(acts, sorted(acts, key=lambda a: a[0]))
        p9 = profiles.load('P9')
        self.assertEqual(p9.rtt_ms, 80)
        self.assertEqual(p9.forward.loss, profiles.load('P3').forward.loss)
        ladder = [a for a in p9.timeline(p9.run_s) if a[1] == 'outage_on']
        self.assertEqual([a[2]['direction'] for a in ladder], ['fwd'] * 4 + ['both'] * 4)
        off = {args['name']: t for t, kind, args in p9.timeline(p9.run_s) if kind == 'outage_off'}
        self.assertAlmostEqual(off['fwd-30s'] - 160, 30)

    def test_rejects(self):
        bad = [dict(id='X', forward=dict(loss={'gemodel': [120]})), dict(id='X', forward=dict(slot_ms=[5, 1])),
               dict(id='X', event=[dict(kind='outage', at_s=1)]), dict(id='X', event=[dict(kind='rate', at_s=1, factor=0.5, for_s=1)]),
               dict(id='X', forward=dict(jitter=3))]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(config.ConfigError):
                profiles.parse(data, 'test')


class Spec(unittest.TestCase):
    def test_example_deployment_and_suite(self):
        dep = spec.load_deployment('docker-contrib-example')
        self.assertEqual(dep.provider, 'docker')
        self.assertEqual(dep.cpus('wan'), '6-7')
        self.assertEqual(dep.images['moqx'], 'ghcr.io/openmoq/moqx:v0.3.7-amd64')
        suite = spec.load_suite('example-contrib')
        self.assertEqual(suite.tests[0].placement, 'last-mile')
        self.assertEqual(suite.tests[1].placement, 'first-mile')
        for test in suite.tests:
            for pid in test.profiles:
                profiles.load(pid)

    def test_budgets_are_rtt_multiples_with_a_floor_and_the_fixed_budgets(self):
        t = make_test()
        self.assertEqual(t.budgets(80), [160, 200, 240, 300, 320, 480])
        self.assertEqual(t.budgets(1), [40, 60, 80, 120, 200, 300])

    def test_variant_filters_apply_only_to_their_own_keys(self):
        a = argparse.Namespace(only=[1], lane=['srt', 'moqt-moq2ts', 'moqt-aiomoqt-msfts'], profile=['P3'], repeats=1,
                               variant=['D=4xRTT', 'relay_cc=bbr2', 'pub_cc=bbr'])
        got = [(lane.name, v) for _, lane, _, _, v in cli.cells(spec.load_suite('example-contrib'), a)]
        self.assertEqual(got, [('srt', {'D': 320}), ('moqt-moq2ts', {'relay_cc': 'bbr2'}),
                               ('moqt-aiomoqt-msfts', {'pub_cc': 'bbr', 'relay_cc': 'bbr2'})])

    def test_rejects(self):
        for over in (dict(lanes=['carrier-pigeon']), dict(lanes=[]), dict(source='h265'), dict(placement='middle'),
                     dict(type='fabric'), dict(colour='red')):
            with self.subTest(over=over), self.assertRaises(config.ConfigError):
                make_test(**over)
        with self.assertRaises(config.ConfigError):
            spec.Deployment('x', dict(provider='docker', host='local'), 'x')
        with self.assertRaises(config.ConfigError):
            spec.Deployment('x', dict(provider='docker', host='local', contribution=dict(subnet='10.79.0.0/24')), 'x')

    def test_scenarios(self):
        for name in ('standard', 'dev', 'soak'):
            s = spec.load_scenario(name)
            self.assertEqual(s['break_ms'], 200)
        self.assertEqual(spec.load_scenario('soak')['source_mode'], 'live')


class Topology(unittest.TestCase):
    def test_addresses_routes_and_offloads(self):
        p = topology.Paths('10.79.0.0/16')
        self.assertEqual(p.src_ip(), '10.79.1.10')
        self.assertEqual(p.dst_ip(), '10.79.2.10')
        self.assertEqual(p.fwd_dev, ['vb0'])
        self.assertEqual(p.rev_dev, ['va0'])
        cmds = p.ns_cmds()
        self.assertIn('ip route add 10.79.2.0/24 via 10.79.1.1', cmds['src'])
        self.assertIn('ip route add 10.79.1.0/24 via 10.79.2.1', cmds['dst'])
        self.assertTrue(any('gso off gro off tso off tx-udp-segmentation off' in c for c in cmds['wan']))
        self.assertTrue(any('239.0.0.0/8 dev mc0' in c for c in cmds['dst']))
        self.assertEqual(p.rx_devs()['dst'], ['wb0', 'mc0'])
        self.assertEqual(p.veth_cmds({'src': 1, 'wan': 2, 'dst': 3})[0],
                         'ip link add va0 netns 2 type veth peer name wa0 netns 1')

    def test_dual_path_and_placement(self):
        p = topology.Paths('10.79.0.0/16', dual=True)
        self.assertEqual(p.fwd_dev, ['vb0', 'vb1'])
        self.assertEqual(p.dst_ip(1), '10.79.4.10')
        self.assertEqual(p.relay_addresses('last-mile'), ('src', '127.0.0.1', '10.79.1.10'))
        self.assertEqual(p.relay_addresses('first-mile'), ('dst', '10.79.2.10', '127.0.0.1'))

    def test_vm_render(self):
        out = topology.vm_render({'src': 'ens3', 'dst': 'ens4'})
        self.assertTrue(any('mirred egress redirect dev ifb0' in c for c in out['dst']))
        self.assertTrue(any('ens4' in c for c in out['dst']))


class Lanes(unittest.TestCase):
    def test_srt_follows_the_traps(self):
        procs = {p.name: p for p in lanes.LANES['srt'].procs(ctx(dict(D=320)))}
        tx, rx = procs['srt-snd'].cmd, procs['srt-rcv'].cmd
        for cmd in (tx, rx):
            self.assertIn('-chunk:1316', cmd)
            self.assertIn('lossmaxttl=32', cmd)
            self.assertIn('latency=320', cmd)
        self.assertIn('udp://239.79.0.1:5000', tx)
        self.assertIn('srt://10.79.2.10:9000?mode=caller', tx)
        fc, buf = lanes.srt_buffers(320, 80, 20e6)
        self.assertGreaterEqual(buf, (0.320 + 0.080) * 20e6 / 8)
        self.assertGreaterEqual(fc * 1316, (0.320 + 0.080) * 20e6 / 8)

    def test_rist(self):
        procs = {p.name: p for p in lanes.LANES['rist'].procs(ctx(dict(D=320)))}
        self.assertIn('-p 1', procs['rist-snd'].cmd)
        self.assertIn('buffer=320', procs['rist-rcv'].cmd)
        self.assertIn('udp://@239.79.0.1:5000', procs['rist-snd'].cmd)

    def test_variants(self):
        t = make_test(relay_cc=['bbr2', 'bbr', 'cubic'], pub_cc=['bbr', 'bbr1'])
        p = profiles.load('P3')
        self.assertEqual([v['D'] for v in lanes.LANES['srt'].variants(t, p)], t.budgets(80))
        self.assertEqual(len(lanes.LANES['moqt-moq2ts'].variants(t, p)), 3)
        self.assertEqual(len(lanes.LANES['moqt-aiomoqt-loc'].variants(t, p)), 6)

    def test_moq2ts_matches_the_setup(self):
        procs = {p.name: p for p in lanes.LANES['moqt-moq2ts'].procs(ctx(dict(relay_cc='bbr2')))}
        pub = procs['moq2ts-pub'].cmd
        for part in ('--transparent', '--draft 18', '--quic-idle-timeout 3000', '--srt-config /out/srt.json',
                     'moqt://127.0.0.1:4433/moq-relay', '--namespace test/spts'):
            self.assertIn(part, pub)
        self.assertIn('--latency 100', procs['srt-ingest'].cmd)
        rx = procs['moq2ts-rcv'].cmd
        self.assertIn('moqt://10.79.1.10:4433/moq-relay', rx)
        self.assertIn('--draft 18', rx)
        self.assertIn('--out 300:239.79.0.2:6000', rx)
        self.assertIn('--out 200:239.79.0.2:6001', rx)
        self.assertIn('reconnecting', rx)
        relay = procs['relay']
        self.assertEqual(relay.ns, 'src')
        self.assertEqual(relay.env['MOQX_MOQT_VERSIONS'], '[18, 16]')
        self.assertEqual(relay.env['MOQX_CACHE'], 'true')
        self.assertEqual(relay.env['MALLOC_CONF'], 'thp:always')
        first = {p.name: p for p in lanes.LANES['moqt-moq2ts'].procs(ctx(dict(relay_cc='bbr'), placement='first-mile'))}
        self.assertEqual(first['relay'].ns, 'dst')
        self.assertEqual(first['relay'].env['MOQX_CC'], 'bbr')
        self.assertIn('moqt://10.79.2.10:4433', first['moq2ts-pub'].cmd)
        c = ctx(dict(relay_cc='bbr2'))
        c.ingest = 'stdin'
        self.assertIn('--video /dev/stdin', {p.name: p for p in lanes.Moq2ts().procs(c)}['moq2ts-pub'].cmd)

    def test_aiomoqt_lanes_follow_the_probed_flags(self):
        loc = lanes.LANES['moqt-aiomoqt-loc']
        old_ctx = ctx(dict(pub_cc='bbr1', relay_cc='bbr2'))
        old = {p.name: p for p in loc.procs(old_ctx)}
        self.assertIn('udp2pipe.py 239.79.0.1:5000 | python -m aiomoqt.tools.pub_media', old['aiomoqt-pub'].cmd)
        self.assertIn('--cc-algo bbr1', old['aiomoqt-pub'].cmd)
        self.assertIn('--annexb', old['aiomoqt-sub'].cmd)
        self.assertEqual(loc.in_tap_hash(old_ctx), 'vcl')
        self.assertEqual(old['aiomoqt-pub'].env['AIOPQUIC_GSO'], '0')
        self.assertEqual(loc.taps(old_ctx), {})
        new_ctx = ctx(dict(pub_cc='bbr', relay_cc='bbr2'), features=dict(ts_udp=True, ts_out=True, frame_log=True, msfts=True))
        new = {p.name: p for p in loc.procs(new_ctx)}
        self.assertIn('--ts udp://@239.79.0.1:5000', new['aiomoqt-pub'].cmd)
        self.assertIn('--frame-log /out/frames-$(date +%s%N).jsonl', new['aiomoqt-sub'].cmd)
        self.assertIn('--draft 18', new['aiomoqt-sub'].cmd)
        self.assertEqual(loc.in_tap_hash(new_ctx), 'loc')
        msfts = lanes.LANES['moqt-aiomoqt-msfts']
        self.assertIn('--msfts', msfts.pending({}))
        self.assertIsNone(msfts.pending(dict(ts_udp=True, msfts=True, ts_out=True)))
        procs = {p.name: p for p in msfts.procs(new_ctx)}
        self.assertIn('--ts udp://@239.79.0.1:5000 --msfts --msfts-packets 7', procs['aiomoqt-pub'].cmd)
        self.assertIn('--ts-out udp://127.0.0.1:6010', procs['aiomoqt-sub'].cmd)
        self.assertIn('--udp 127.0.0.1:6010', procs['playout'].cmd)
        self.assertIn('po300', msfts.taps(new_ctx))
        self.assertIsNone(msfts.in_tap_hash(new_ctx))


class FakeWan:
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if cmd.startswith('t0='):
            return 0, '{"t0": 1.0, "t1": 1.1, "rc": 0}\n'
        if cmd.startswith('nft -j'):
            return 0, '{"nftables": [{"rule": {"comment": "outage", "expr": [{"counter": {"packets": 9, "bytes": 99}}]}}]}'
        return 0, ''


class Emulator(unittest.TestCase):
    def test_build_events_and_clear(self):
        wan = FakeWan()
        e = emu.Emulator(topology.Paths('10.79.0.0/16'), wan, 20e6)
        p = profiles.load('P7')
        e.build(p, repeat=1)
        text = '\n'.join(wan.cmds)
        self.assertIn('tc qdisc add dev vb0 root handle 1: netem', text)
        self.assertIn(f'seed {p.forward.seed + 10}', text)
        self.assertIn('tc qdisc add dev va0 root handle 1: netem', text)
        self.assertIn('nft -f -', text)
        on = e.command('outage_on', dict(direction='both'))
        self.assertEqual(on, 'nft add element inet mbc blocked { "va0", "vb0" }')
        self.assertEqual(e.command('outage_off', dict(direction='fwd')), 'nft delete element inet mbc blocked { "va0" }')
        step = e.command('delay', dict(direction='both', delta_ms=60))
        self.assertIn('dev vb0 root handle 1: netem limit 100000 delay 90ms', step)
        self.assertIn('dev va0 root handle 1: netem limit 100000 delay 90ms', step)
        self.assertIn('tbf rate', e.command('rate', dict(factor=0.533)))
        rec = e.apply('outage_off', dict(name='x', direction='fwd'))
        self.assertEqual(rec['drops'], 9)
        self.assertEqual(rec['t0'], 1.0)
        e.clear()
        self.assertIn('nft delete table inet mbc', wan.cmds[-1])

    def test_nft_script_counts_before_dropping(self):
        script = emu.nft_script(topology.Paths('10.79.0.0/16'))
        self.assertLess(script.index('comment "fwd0"'), script.index('@blocked counter drop'))
        self.assertIn('set blocked { type ifname; }', script)


class Calibration(unittest.TestCase):
    def test_spread_grows_with_burstiness(self):
        bursty = profiles.load('P6').forward
        random = profiles.parse(dict(id='R', forward=dict(loss={'random': 2.952})), 't').forward
        self.assertGreater(calibrate.ge_sigma_pct(bursty, 40000), 3 * calibrate.ge_sigma_pct(random, 40000))
        self.assertEqual(calibrate.ge_sigma_pct(profiles.load('P0').forward, 1000), 0.0)

    def test_analyse(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            sent = 1000
            lost = {10, 11, 500}
            with open(run / 'probe-echo.csv', 'w') as f:
                f.write('seq,t_send_ns,t_recv_ns,t_user_ns\n')
                order = [s for s in range(sent) if s not in lost]
                order[100], order[101] = order[101], order[100]
                for s in order:
                    f.write(f'{s},{s * 1000000},{s * 1000000 + 40_500_000},0\n')
            with open(run / 'probe-send.csv', 'w') as f:
                f.write('seq,t_send_ns,t_echo_ns\n')
                for s in range(sent):
                    if s not in lost:
                        f.write(f'{s},{s * 1000000},{s * 1000000 + 81_000_000}\n')
                f.write(f'# sent {sent}\n')
            r = calibrate.analyse(run, profiles.load('P3'), 1900, 1)
        self.assertAlmostEqual(r['fwd_loss_pct'], 0.3)
        self.assertEqual(r['rev_loss_pct'], 0.0)
        self.assertEqual(r['reorder_events'], 1)
        self.assertEqual(r['owd_ms']['p50'], 40.5)
        self.assertTrue(r['ok_delay'])


if __name__ == '__main__':
    unittest.main()
