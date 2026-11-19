"""Contribution mode: profiles, configuration, topology, lanes and the emulator, without Docker."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import config  # noqa: E402
import argparse  # noqa: E402
from contrib import calibrate, cli, emu, lanes, profiles, run, sources, spec, topology  # noqa: E402
from contrib import docker as contrib_docker  # noqa: E402
from types import SimpleNamespace  # noqa: E402

# Mean forward loss in percent each profile is built for.
TARGET_LOSS = {'lan': 0.0, 'regional-clean': 0.02, 'national-managed': 0.1, 'transcontinental': 0.5, 'intercontinental-good': 0.3, 'longhaul-lossy': 1.0, 'intercontinental-bad': 3.0, 'cellular-handover': 2.0, 'leo-satellite': 0.4,
               'outage-ladder': 0.5, 'random-loss': 0.5, 'bloat-cable': 0.0, 'bloat-cellular': 0.0, 'cable-fqcodel': 0.0}


def make_test(**over):
    data = dict(type='contribution', lanes=['srt', 'rist', 'moqt-moq2ts', 'moqt-aiomoqt-loc'], profiles=['transcontinental'])
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
        p = profiles.load('transcontinental')
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
        p = profiles.load('transcontinental')
        self.assertAlmostEqual(p.forward.cap_bps(20e6), 1.5 * 20e6 * 1358 / 1316)
        self.assertIsNone(profiles.load('random-loss').forward.cap_bps(20e6))

    def test_reference_profile(self):
        p = profiles.load('random-loss')
        self.assertEqual(p.forward.netem_args(), 'limit 100000 delay 50ms rate 1gbit loss random 0.5% seed 2001')
        self.assertEqual(p.reverse.mean_loss_pct(), 0.0)

    def test_timeline_expands_repeats_and_ladders(self):
        p6 = profiles.load('intercontinental-bad')
        acts = p6.timeline(300, start_s=10)
        outages = [a for a in acts if a[1] == 'outage_on']
        self.assertEqual(sum(1 for a in outages if a[2]['name'].startswith('outage-1600ms')), 2)
        self.assertTrue(all(10 <= t < 310 + 2 for t, _, _ in acts))
        self.assertEqual(acts, sorted(acts, key=lambda a: a[0]))
        p9 = profiles.load('outage-ladder')
        self.assertEqual(p9.rtt_ms, 80)
        self.assertEqual(p9.forward.loss, profiles.load('transcontinental').forward.loss)
        ladder = [a for a in p9.timeline(p9.run_s) if a[1] == 'outage_on']
        self.assertEqual([a[2]['direction'] for a in ladder], ['fwd'] * 4 + ['both'] * 4)
        off = {args['name']: t for t, kind, args in p9.timeline(p9.run_s) if kind == 'outage_off'}
        self.assertAlmostEqual(off['fwd-30s'] - 160, 30)

    def test_bufferbloat_profiles(self):
        b1, b2, b3 = (profiles.load(n) for n in ('bloat-cable', 'bloat-cellular', 'cable-fqcodel'))
        tree = b1.forward.build('vb0', 20e6, seed=7)
        self.assertIn('handle 20: tbf rate 51595744bit burst 64kb latency 500ms', tree[2])
        self.assertEqual(len(tree), 3)
        tree = b3.forward.build('vb0', 20e6, seed=7)
        self.assertEqual(tree[3], 'tc qdisc add dev vb0 parent 20:1 handle 30: fq_codel')
        self.assertEqual(b3.events, b1.events)
        self.assertIn('latency 1000ms', b2.forward.build('vb0', 20e6)[2])
        acts = b1.timeline(300, start_s=10)
        on = [a for a in acts if a[1] == 'cross_on']
        off = [a for a in acts if a[1] == 'cross_off']
        self.assertEqual(len(on), 7)
        self.assertEqual(on[0], (30, 'cross_on', dict(name='tcp-upload#1', port=5201, proto='tcp', load=None, streams=1,
                                                       cc=None, for_s=20)))
        self.assertEqual(off[0], (50, 'cross_off', dict(name='tcp-upload#1', port=5201)))
        self.assertEqual(b1.summary(20e6)['cross'], 1)
        self.assertEqual(b3.summary(20e6)['cap_aqm'], 'fq_codel')
        two = profiles.parse(dict(id='X', forward=dict(cap=2), event=[
            dict(kind='cross', at_s=1, for_s=2), dict(kind='cross', at_s=5, for_s=2, proto='udp', load=0.5)]), 't')
        self.assertEqual([a[2]['port'] for a in two.timeline(10) if a[1] == 'cross_on'], [5201, 5202])

    def test_rejects(self):
        bad = [dict(id='X', forward=dict(loss={'gemodel': [120]})), dict(id='X', forward=dict(slot_ms=[5, 1])),
               dict(id='X', forward=dict(cap_aqm='fq_codel')), dict(id='X', forward=dict(cap=2, cap_aqm='red')),
               dict(id='X', event=[dict(kind='cross', at_s=1, for_s=2, proto='udp')]),
               dict(id='X', event=[dict(kind='cross', at_s=1, for_s=2, load=0.5)]),
               dict(id='X', event=[dict(kind='cross', at_s=1, for_s=2, proto='sctp')]),
               dict(id='X', event=[dict(kind='cross', at_s=1)]),
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
        a = argparse.Namespace(only=[1], lane=['srt', 'moqt-moq2ts', 'moqt-aiomoqt-msfts'], profile=['transcontinental'], repeats=1,
                               variant=['D=4xRTT', 'relay_cc=bbr2', 'pub_cc=bbr'])
        got = [(lane.name, v) for _, lane, _, _, v in cli.cells(spec.load_suite('example-contrib'), a)]
        self.assertEqual(got, [('srt', {'D': 320}), ('moqt-moq2ts', {'relay_cc': 'bbr2'}),
                               ('moqt-aiomoqt-msfts', {'pub_cc': 'bbr', 'relay_cc': 'bbr2', 'timing': 'none'}),
                               ('moqt-aiomoqt-msfts', {'pub_cc': 'bbr', 'relay_cc': 'bbr2', 'timing': 'mapping'})])

    def test_rejects(self):
        for over in (dict(lanes=['carrier-pigeon']), dict(lanes=[]), dict(source='h265'), dict(placement='middle'),
                     dict(relay='quiche'),
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
        t = make_test(lanes=['srt', 'moqt-moq2ts', 'moqt-aiomoqt-loc', 'moqt-aiomoqt-dual'],
                      relay_cc=['bbr2', 'bbr', 'cubic'], pub_cc=['bbr', 'bbr1'])
        p = profiles.load('transcontinental')
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
        self.assertIn("trap 'kill -TERM $pid 2>/dev/null; wait $pid; exit 0' TERM INT", rx)
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

    def test_relay_choice(self):
        moqx = make_test()
        self.assertEqual(moqx.relay_cc, ['bbr2'])
        self.assertNotIn('relay', lanes.LANES['moqt-moq2ts'].variants(moqx, profiles.load('transcontinental'))[0])
        t = make_test(lanes=['moqt-moq2ts', 'moqt-aiomoqt-msfts'], relay='aiomoqt', relay_cc=['bbr', 'bbr1'])
        msfts = lanes.LANES['moqt-aiomoqt-msfts']
        self.assertEqual([v['relay'] for v in msfts.variants(t, profiles.load('transcontinental'))], ['aiomoqt', 'aiomoqt'])
        self.assertEqual(make_test(relay='aiomoqt').relay_cc, ['bbr'])
        c = ctx(dict(relay='aiomoqt', pub_cc='bbr', relay_cc='bbr1'))
        c.relay = 'aiomoqt'
        relay = {p.name: p for p in msfts.procs(c)}['relay']
        self.assertEqual((relay.ns, relay.image), ('src', 'aiomoqt'))
        for part in ('openssl req -x509', 'moq_interop_relay --quic --port 4433', '--cert /tmp/cert.pem',
                     '--key /tmp/priv.key', '--cc-algo bbr1'):
            self.assertIn(part, relay.cmd)
        self.assertEqual(relay.env['AIOPQUIC_GSO'], '0')
        self.assertEqual(msfts.relay_impl(c), 'aiomoqt')
        dual = lanes.LANES['moqt-aiomoqt-dual']
        self.assertEqual((dual.relay_ns(c), dual.relay_impl(ctx({}))), ('dst', 'aiomoqt'))

    def test_direct_lane(self):
        t = make_test(lanes=['srt', 'moqt-aiomoqt-msfts'], relay='none', placement='first-mile', pub_cc=['bbr', 'bbr1'])
        self.assertEqual(t.relay_cc, [None])
        msfts = lanes.LANES['moqt-aiomoqt-msfts']
        vs = msfts.variants(t, profiles.load('transcontinental'))
        self.assertEqual(vs, [dict(relay='none', pub_cc='bbr', timing='none'), dict(relay='none', pub_cc='bbr1', timing='none')])
        self.assertEqual(msfts.label(vs[0]), 'relaynone-pub_ccbbr-timingnone')
        c = ctx(vs[0], placement='first-mile', features=dict(ts_udp=True, msfts=True, ts_out=True))
        c.relay = 'none'
        procs = {p.name: p for p in msfts.procs(c)}
        self.assertNotIn('relay', procs)
        pub = procs['aiomoqt-pub']
        self.assertEqual((pub.ns, pub.image), ('src', 'aiomoqt'))
        for part in ('pub_media moqt://10.79.2.10:4433/ -k --pub-ns --draft 18', '--msfts --msfts-packets 7',
                     '--cc-algo bbr ', ' --reconnect '):
            self.assertIn(part, pub.cmd)
        sub = procs['aiomoqt-sub']
        self.assertEqual(sub.ns, 'dst')
        self.assertTrue(sub.cmd.startswith('openssl req -x509'))
        self.assertIn('sub_media -Q -p 4433 --cert /tmp/cert.pem --key /tmp/priv.key --draft 18', sub.cmd)
        self.assertNotIn(' -k ', sub.cmd)
        self.assertEqual(msfts.cc_on_wan('first-mile'), {'pub_cc'})
        relayed = {p.name: p for p in msfts.procs(ctx(dict(pub_cc='bbr', relay_cc='bbr2', timing='none'),
                                                      placement='first-mile', features=c.features))}
        self.assertIn('pub_media moqt://10.79.2.10:4433/moq-relay -k --draft 18', relayed['aiomoqt-pub'].cmd)
        self.assertIn(' --reconnect ', relayed['aiomoqt-pub'].cmd)
        self.assertIn('sub_media moqt://127.0.0.1:4433/moq-relay -k --draft 18', relayed['aiomoqt-sub'].cmd)
        self.assertNotIn('openssl', relayed['aiomoqt-sub'].cmd)

    def test_direct_lane_needs_first_mile_and_a_direct_lane(self):
        for over in (dict(placement='last-mile'), dict(placement='first-mile', relay_cc=['bbr']),
                     dict(placement='first-mile', lanes=['moqt-aiomoqt-msfts', 'moqt-moq2ts']),
                     dict(placement='first-mile', lanes=['moqt-aiomoqt-dual'])):
            data = dict(lanes=['moqt-aiomoqt-msfts'], relay='none')
            data.update(over)
            with self.subTest(over=over), self.assertRaises(config.ConfigError):
                make_test(**data)

    def test_controller_sweeps_must_reach_the_impaired_hop(self):
        for over in (dict(pub_cc=['bbr', 'bbr1']),
                     dict(placement='first-mile', relay_cc=['bbr2', 'cubic']),
                     dict(lanes=['srt', 'moqt-moq2ts'], placement='first-mile', pub_cc=['bbr', 'c4'])):
            with self.subTest(over=over), self.assertRaises(config.ConfigError):
                make_test(**over)
        make_test(placement='first-mile', pub_cc=['bbr', 'bbr1'])
        make_test(lanes=['moqt-aiomoqt-dual'], pub_cc=['bbr', 'bbr1'])
        make_test(relay_cc=['bbr2', 'cubic'])

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
        self.assertNotIn('--timestamp-mapping', procs['aiomoqt-pub'].cmd)
        for p in procs.values():
            self.assertNotIn(' -T ', p.cmd)
        mapped = ctx(dict(pub_cc='bbr', relay_cc='bbr2', timing='mapping'), features=new_ctx.features)
        self.assertIn('--msfts --msfts-packets 7 --timestamp-mapping',
                      {p.name: p for p in msfts.procs(mapped)}['aiomoqt-pub'].cmd)
        t = make_test(pub_timing=['none', 'mapping'])
        self.assertEqual([v['timing'] for v in msfts.variants(t, profiles.load('transcontinental'))], ['none', 'mapping'])
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
        p = profiles.load('cellular-handover')
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
        self.assertLess(script.index('comment "cross-rev0"'), script.index('@blocked counter drop'))
        self.assertIn('iifname "va0" oifname "vb0" meta l4proto { tcp, udp } th dport 5201-5208 counter', script)
        self.assertIn('set blocked { type ifname; }', script)

    def test_timeline_hands_cross_traffic_to_its_handlers(self):
        wan = FakeWan()
        e = emu.Emulator(topology.Paths('10.79.0.0/16'), wan, 20e6)
        e.build(profiles.load('bloat-cable'))
        seen = []
        handlers = {'cross_on': lambda a: seen.append(a) or dict(action='cross_on', t0=1.0)}
        acts = [(0, 'cross_on', dict(name='c#1', port=5201)), (0, 'rate', dict(name='r', factor=0.5))]
        with tempfile.TemporaryDirectory() as d:
            done = e.run_timeline(acts, 0, f'{d}/faults.jsonl', handlers=handlers)
        self.assertEqual(seen, [dict(name='c#1', port=5201)])
        self.assertEqual([r['action'] for r in done], ['cross_on', 'rate'])
        self.assertIn('tbf rate 25797872bit', wan.cmds[-1])


class RemoteWork(unittest.TestCase):
    def provider(self, stack_dir, home='/home/u'):
        dep = SimpleNamespace(host='remote', subnet='10.79.0.0/16', name='dep', stack_dir=stack_dir)
        prov = contrib_docker.DockerContrib(dep)
        calls = []

        def sh(script, check=True, timeout=300):
            calls.append(script)
            return SimpleNamespace(stdout=home, stderr='', returncode=0)
        prov.host = SimpleNamespace(local=False, sh=sh)
        return prov, calls

    def test_a_relative_stack_dir_sits_under_the_remote_home(self):
        prov, calls = self.provider('moqbench-contrib')
        self.assertEqual(prov.work, '/home/u/moqbench-contrib/dep')
        self.assertEqual(prov.work, '/home/u/moqbench-contrib/dep')
        self.assertEqual(len(calls), 1)

    def test_an_absolute_stack_dir_is_used_as_given(self):
        prov, calls = self.provider('/srv/mb')
        self.assertEqual(prov.work, '/srv/mb/dep')
        self.assertEqual(calls, [])

    def test_no_home_is_an_error(self):
        prov, _ = self.provider('moqbench-contrib', home='')
        with self.assertRaises(contrib_docker.FabricError):
            prov.work


class FakeProv:
    def __init__(self):
        self.paths = topology.Paths('10.79.0.0/16')
        self.started, self.stopped, self.shell = [], [], []

    def sh(self, script, check=True, timeout=300):
        self.shell.append(script)

    def start(self, proc, run_dir, bin_dir, cell, sources_dir=None):
        self.started.append(proc)
        return f'mbc-{cell}'

    def stop(self, names, signal='TERM', grace_s=5):
        self.stopped.append((names, signal))


class Cell(unittest.TestCase):
    def cell(self, profile, lane='srt', out='/nonexistent', variant=None, **over):
        test = make_test(lanes=[lane], profiles=[profile], **over)
        return run.Cell(FakeProv(), test, spec.load_scenario('standard'), lanes.LANES[lane], profiles.load(profile),
                        variant or {}, 0, f'r001-{lane}', out)

    def test_capture(self):
        self.assertFalse(any(p.name.startswith(('pcap', 'qdisc')) for p in self.cell('bloat-cable').helpers()))
        procs = {p.name: p for p in self.cell('bloat-cable', capture='headers').helpers()}
        self.assertEqual(procs['pcap-src0'].cmd, 'tcpdump -i wa0 -p -n -s 96 -U -w /out/src0.pcap not portrange 5201-5208')
        self.assertEqual((procs['pcap-dst0'].ns, procs['pcap-dst0'].root), ('dst', True))
        self.assertIn('--cmd fwd="tc -s -j qdisc show dev vb0" --cmd rev="tc -s -j qdisc show dev va0" --interval 0.1',
                      procs['qdisc-wan'].cmd)
        full = {p.name: p for p in self.cell('bloat-cable', capture='full').helpers()}
        self.assertIn('-s 0 ', full['pcap-src0'].cmd)
        c = self.cell('transcontinental', lane='moqt-aiomoqt-msfts', capture='full', placement='first-mile',
                      variant=dict(pub_cc='bbr', relay_cc='bbr2', pub_timing='none'))
        lane_procs = c.lane.procs(c.ctx)
        self.assertEqual(c.capture_env(lane_procs, '/run'), ['qlog-aiomoqt-pub', 'qlog-aiomoqt-sub'])
        pub = next(p for p in lane_procs if p.name == 'aiomoqt-pub')
        self.assertEqual(pub.env['AIOPQUIC_QLOG_DIR'], '/out/qlog-aiomoqt-pub')
        self.assertEqual(pub.env['SSLKEYLOGFILE'], '/out/keys-aiomoqt-pub.log')
        self.assertEqual(pub.env['AIOPQUIC_TEXTLOG_FILE'], '/dev/null')
        self.assertIn('mkdir -p -m 777 /run/qlog-aiomoqt-pub', c.prov.shell)
        with self.assertRaises(config.ConfigError):
            make_test(capture='yes')

    def test_captures_are_gzipped(self):
        import gzip
        with tempfile.TemporaryDirectory() as d:
            c = self.cell('bloat-cable', out=d, capture='headers')
            (c.out / 'qlog-aiomoqt-pub').mkdir(parents=True)
            (c.out / 'src0.pcap').write_bytes(b'pcap')
            (c.out / 'qlog-aiomoqt-pub' / 'x.qlog').write_text('{}')
            (c.out / 'srt-snd.json').write_text('{}')
            c.compress_captures()
            self.assertEqual(sorted(str(f.relative_to(c.out)) for f in c.out.rglob('*') if f.is_file()),
                             ['qlog-aiomoqt-pub/x.qlog.gz', 'src0.pcap.gz', 'srt-snd.json'])
            self.assertEqual(gzip.open(c.out / 'src0.pcap.gz').read(), b'pcap')

    def test_cross_servers_and_clients(self):
        c = self.cell('bloat-cable')
        servers = [p for p in c.helpers() if p.name.startswith('cross-srv')]
        self.assertEqual([(p.name, p.ns, p.cmd) for p in servers],
                         [('cross-srv1', 'dst', 'iperf3 -s -p 5201 -J --logfile /out/cross-srv1.json')])
        on, off = (c.cross_handlers('/run', '/bin')[k] for k in ('cross_on', 'cross_off'))
        rec = on(dict(name='tcp-upload#2', port=5201, proto='tcp', load=None, streams=2, cc='cubic', for_s=20))
        proc = c.prov.started[-1]
        self.assertEqual((proc.name, proc.ns), ('cross-tcp-upload-2', 'src'))
        self.assertEqual(proc.cmd, 'iperf3 -c 10.79.2.10 -p 5201 -t 20 -i 1 -J --connect-timeout 3000 '
                                   '--logfile /out/cross-tcp-upload-2.json -P 2 -C cubic')
        self.assertEqual(rec['action'], 'cross_on')
        off(dict(name='tcp-upload#2', port=5201))
        self.assertEqual(c.prov.stopped, [(['mbc-r001-cross-tcp-upload-2'], 'INT')])
        on(dict(name='u', port=5202, proto='udp', load=0.5, streams=1, cc=None, for_s=5))
        self.assertTrue(c.prov.started[-1].cmd.endswith('-u -l 1200 -b 10319148'))
        self.assertFalse(any(p.name.startswith('cross') for p in self.cell('transcontinental').helpers()))


class Calibration(unittest.TestCase):
    def test_spread_grows_with_burstiness(self):
        bursty = profiles.load('intercontinental-bad').forward
        random = profiles.parse(dict(id='R', forward=dict(loss={'random': 2.952})), 't').forward
        self.assertGreater(calibrate.ge_sigma_pct(bursty, 40000), 3 * calibrate.ge_sigma_pct(random, 40000))
        self.assertEqual(calibrate.ge_sigma_pct(profiles.load('lan').forward, 1000), 0.0)

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
            r = calibrate.analyse(run, profiles.load('transcontinental'), 1900, 1)
        self.assertAlmostEqual(r['fwd_loss_pct'], 0.3)
        self.assertEqual(r['rev_loss_pct'], 0.0)
        self.assertEqual(r['reorder_events'], 1)
        self.assertEqual(r['owd_ms']['p50'], 40.5)
        self.assertTrue(r['ok_delay'])


if __name__ == '__main__':
    unittest.main()
