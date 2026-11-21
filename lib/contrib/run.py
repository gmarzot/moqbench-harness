"""One contribution run (a cell): a lane and variant on a profile, from emulator build to collected files.

Order: emulator tree and counters; taps, analyser and probes; the lane's relay, receivers and senders;
the paced source; subscribers. The measurement window opens join_s after the first source datagram;
profile events are timed from it, cross-traffic flows included (iperf3 from src to a server in dst). At the end the source stops, the lane drains, everything stops, and
logs, counters and the summary land in the run folder.

With capture set, tcpdump records each endpoint's WAN-facing interface (headers: 96 bytes per packet;
full: whole packets, and the aiomoqt endpoints' TLS keys), every aiomoqt endpoint writes picoquic
qlog, and the bottleneck's qdiscs are sampled every 100 ms. Captures and qlogs are gzipped on
collection.

On a VM deployment every tap stamps CLOCK_REALTIME, and each run's meta records both nodes' chrony
tracking and the unimpaired RTT before and after the run.
"""
import gzip
import json
from pathlib import Path
import re
import shlex
import shutil
import threading
import time

from contrib import lanes, profiles, report, sources
from contrib.lanes import Proc

STAGES = ('relay', 'receiver', 'sender', 'source', 'subscriber')
SNAPLEN = {'headers': 96, 'full': 0}


def source_seconds(scenario, profile, test):
    seconds = max(test.seconds or scenario['seconds'], profile.run_s)
    return seconds, int(scenario['join_s'] + seconds + scenario['drain_s'] + 10)


def ensure_source(prov, tier, length_s):
    """The fixture for a tier and length under the provider's work folder; encoded once."""
    folder = f'{prov.work}/sources'
    name = tier.file(length_s)
    prov.sh(f'mkdir -p {shlex.quote(folder)}')
    if prov.sh(f'test -s {shlex.quote(folder)}/{name}', check=False).returncode == 0:
        return folder, name
    args = ' '.join(shlex.quote(a) for a in tier.ffmpeg_args(length_s))
    prov.sh(f'docker run --rm --network none --user {prov.user or prov.ids()} -v {shlex.quote(folder)}:/sources '
            f'{shlex.quote(prov.image("ffmpeg"))} {args} -y /sources/{name}.part && '
            f'mv {shlex.quote(folder)}/{name}.part {shlex.quote(folder)}/{name}', timeout=3600)
    return folder, name


def probe_features(prov, lane, cache):
    out = {}
    for feature, (image, cmd, text) in lane.needs.items():
        key = (image, cmd)
        if key not in cache:
            cache[key] = prov.probe(image, cmd)
        out[feature] = text in cache[key]
    return out


class Cell:
    def __init__(self, prov, test, scenario, lane, profile, variant, repeat, label, out_root, features=None):
        self.prov, self.test, self.scen = prov, test, scenario
        self.lane, self.profile, self.variant, self.repeat = lane, profile, variant, repeat
        self.label = label
        self.out = Path(out_root) / label
        self.tier = sources.TIERS['h264-20m'] if lane.codec == 'h264' else sources.TIERS[test.source]
        self.seconds, self.length = source_seconds(scenario, profile, test)
        slot = profile.forward.slot_ms
        self.ctx = lanes.Ctx(paths=prov.paths, placement=test.placement, tier=self.tier, rtt_ms=profile.rtt_ms,
                             variant=variant, playout_ms=list(test.playout_ms),
                             slot_max_ms=slot[1] if slot else 0.0, ingest=test.ingest, features=features or {},
                             relay=test.relay)
        self.wall = lane.wall_clock(self.ctx) or prov.paths.wall_clock
        self.names = []
        self.samples = []
        self.cross = {}

    def helpers(self):
        """Taps, analyser and probes for this lane."""
        bin_ = 'python3 /contrib/bin'
        form = self.lane.in_tap_hash(self.ctx)
        frames = f'--frames --hash --hash-form {form}' if form else '--frames'
        if self.wall:
            frames += ' --clock real'
        gap = self.scen['gap_ms']
        procs = [Proc('tap-in', 'src', 'tools',
                      f'{bin_}/tstap.py --udp in={sources.GROUP_IN}:{sources.PORT_IN} {frames} --gap-ms {gap} --out /out'),
                 Proc('clock-src', 'src', 'tools', f'{bin_}/clockprobe.py --out /out/clock-src.csv'),
                 Proc('clock-dst', 'dst', 'tools', f'{bin_}/clockprobe.py --out /out/clock-dst.csv'),
                 Proc('ss-dst', 'dst', 'tools', f'{bin_}/sampler.py --cmd ss="ss -uamn" --interval 1 --out /out/ss-dst.jsonl'),
                 Proc('ss-src', 'src', 'tools', f'{bin_}/sampler.py --cmd ss="ss -uamn" --interval 1 --out /out/ss-src.jsonl')]
        taps = self.lane.taps(self.ctx)
        if taps:
            specs = ' '.join(f'--udp {name}={g}:{p}' for name, (g, p, _) in taps.items())
            procs.append(Proc('tap-out', 'dst', 'tools', f'{bin_}/tstap.py {specs} {frames} --gap-ms {gap} --out /out'))
            main = next(name for name in taps if name != 'raw')
            g, p, _ = taps[main]
            procs.append(Proc('analyser', 'dst', 'tools', f'tsp -I ip {g}:{p} -P continuity -P pcrverify -O drop'))
        if self.test.capture != 'none':
            snap = SNAPLEN[self.test.capture]
            ports = f'{profiles.CROSS_PORT}-{profiles.CROSS_PORT + profiles.CROSS_MAX - 1}'
            for ln in self.prov.paths.links:
                for ns, dev in (('src', ln['src_if']), ('dst', ln['dst_if'])):
                    procs.append(Proc(f'pcap-{ns}{ln["k"]}', ns, 'tools',
                                      f'tcpdump -i {dev} -p -n -s {snap} -U -w /out/{ns}{ln["k"]}.pcap '
                                      f'not portrange {ports}', root=True))
            for ns, name, devs in self.prov.paths.qdisc_samplers():
                sides = ' '.join(f'--cmd {d}="tc -s -j qdisc show dev {dev}"' for d, dev in devs)
                procs.append(Proc(name, ns, 'tools', f'{bin_}/sampler.py {sides} --interval 0.1 --out /out/{name}.jsonl'))
        for k, _ in enumerate(self.profile.cross_events()):
            procs.append(Proc(f'cross-srv{k + 1}', 'dst', 'tools',
                              f'iperf3 -s -p {profiles.CROSS_PORT + k} -J --logfile /out/cross-srv{k + 1}.json'))
        if self.lane.relay and self.lane.relay_impl(self.ctx) == 'moqx':
            procs.append(Proc('relay-admin', self.lane.relay_ns(self.ctx), 'tools',
                              f'{bin_}/sampler.py --url metrics=http://127.0.0.1:{lanes.ADMIN_PORT}/metrics '
                              f'--interval 1 --out /out/relay-admin.jsonl'))
        return procs

    def cross_handlers(self, run_dir, bin_dir):
        """Timeline handlers that start a cross flow's iperf3 client in src and stop it."""
        def on(args):
            name = 'cross-' + re.sub(r'[^A-Za-z0-9]+', '-', args['name']).strip('-')
            cmd = (f"iperf3 -c {self.prov.paths.dst_ip()} -p {args['port']} -t {profiles.fmt(args['for_s'])} -i 1 -J "
                   f"--connect-timeout 3000 --logfile /out/{name}.json")
            if args['proto'] == 'udp':
                cmd += f" -u -l 1200 -b {int(args['load'] * profiles.l2_bps(self.tier.mux_bps))}"
            else:
                cmd += (f" -P {args['streams']}" if args['streams'] > 1 else '') + (f" -C {args['cc']}" if args['cc'] else '')
            proc = Proc(name, 'src', 'tools', cmd)
            t0 = time.time()
            n = self.prov.start(proc, run_dir, bin_dir, self.cid(proc))
            self.names.append((n, name))
            self.cross[args['name']] = n
            return dict(action='cross_on', **args, command=cmd, t0=t0, t1=time.time())

        def off(args):
            n = self.cross.pop(args['name'], None)
            t0 = time.time()
            if n:
                self.prov.stop([n], signal='INT', grace_s=2)
            return dict(action='cross_off', **args, t0=t0, t1=time.time())
        return {'cross_on': on, 'cross_off': off}

    def capture_env(self, procs, run_dir):
        """qlog (and with full capture, TLS keys) for every aiomoqt endpoint; returns the qlog folders."""
        if self.test.capture == 'none':
            return []
        dirs = []
        for p in procs:
            if p.image != 'aiomoqt':
                continue
            dirs.append(f'qlog-{p.name}')
            self.prov.sh(f'mkdir -p -m 777 {shlex.quote(run_dir)}/{dirs[-1]}')
            # A text log raises picoquic's log level, so qlog keeps every packet rather than the first 100.
            p.env = dict(p.env, AIOPQUIC_QLOG_DIR=f'/out/{dirs[-1]}', AIOPQUIC_TEXTLOG_FILE='/dev/null')
            if self.test.capture == 'full':
                p.env['SSLKEYLOGFILE'] = f'/out/keys-{p.name}.log'
        return dirs

    def compress_captures(self):
        """gzip the run's pcaps and qlogs in place."""
        for f in [*self.out.glob('*.pcap'), *self.out.glob('qlog-*/*')]:
            if f.is_file() and f.suffix != '.gz':
                with open(f, 'rb') as src, gzip.open(f'{f}.gz', 'wb', compresslevel=1) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                f.unlink()

    def sample_emulator(self, emu, stop):
        while not stop.wait(1.0):
            try:
                self.samples.append(dict(t=time.time(), nft=emu.counters(), qdisc=emu.qdisc_stats()))
            except (RuntimeError, ValueError):
                pass

    def execute(self):
        prov = self.prov
        self.out.mkdir(parents=True, exist_ok=True)
        run_dir = prov.run_dir(self.label, self.out)
        bin_dir = prov.bin_dir()
        live = self.scen['source_mode'] == 'live'
        if live:
            folder, name = None, 'live'
            prov.sh(f'mkfifo {shlex.quote(run_dir)}/source.fifo')
        else:
            folder, name = ensure_source(prov, self.tier, self.length)
        for fname, text in self.lane.files(self.ctx).items():
            prov.write(run_dir, fname, text)
        emu = prov.emulator(self.tier.mux_bps)
        meta = dict(label=self.label, lane=self.lane.name, variant=self.variant, repeat=self.repeat,
                    profile=self.profile.summary(self.tier.mux_bps), profile_id=self.profile.id,
                    rtt_ms=self.profile.rtt_ms, placement=self.test.placement, ingest=self.test.ingest,
                    source=dict(tier=self.tier.name, mux_bps=self.tier.mux_bps, file=name, codec=self.tier.codec),
                    seconds=self.seconds, join_s=self.scen['join_s'], break_ms=self.scen['break_ms'],
                    budgets_ms=self.test.budgets(self.profile.rtt_ms),
                    playout_ms=self.ctx.playout_ms, features=self.ctx.features, taps=list(self.lane.taps(self.ctx)),
                    wall_clock=self.wall,
                    seeds=self.profile.seeds(self.repeat), topology=dict(links=prov.paths.links))
        path_meta = getattr(prov, 'cell_meta', None)
        if path_meta:
            meta['path'] = dict(start=path_meta())
        emu.build(self.profile, self.repeat)
        meta['emulator_build'] = emu.qdisc_stats()
        stop = threading.Event()
        sampler = threading.Thread(target=self.sample_emulator, args=(emu, stop), daemon=True)
        procs = []
        timeline = None
        try:
            sampler.start()
            helpers = self.helpers()
            for p in helpers:
                self.names.append((prov.start(p, run_dir, bin_dir, self.cid(p), sources_dir=folder), p.name))
            time.sleep(1.0)
            lane_procs = self.lane.procs(self.ctx)
            meta['capture'] = dict(mode=self.test.capture, qlog=self.capture_env(lane_procs, run_dir))
            source = [Proc('source', 'src', 'tools',
                           sources.tsp_cmd('/out/source.fifo' if live else f'/sources/{name}'), 'source')]
            if live:
                args = ' '.join(shlex.quote(a) for a in self.tier.ffmpeg_args(self.length))
                source.insert(0, Proc('encoder', 'src', 'ffmpeg', f'ffmpeg {args} -y /out/source.fifo', 'source'))
            ordered = sorted(lane_procs + source, key=lambda p: STAGES.index(p.stage))
            meta['commands'] = {p.name: dict(ns=p.ns, image=prov.image(p.image), cmd=p.cmd, env=p.env)
                                for p in helpers + ordered}
            t_source = None
            for p in ordered:
                if p.stage == 'subscriber' and t_source is not None:
                    while time.monotonic() < t_source + 3:
                        time.sleep(0.1)
                n = prov.start(p, run_dir, bin_dir, self.cid(p), sources_dir=folder)
                procs.append((p, n))
                self.names.append((n, p.name))
                if p.stage == 'source':
                    t_source = time.monotonic()
                    meta['source_started'] = time.time()
                time.sleep(p.settle_s)
            actions = self.profile.timeline(self.seconds, start_s=self.scen['join_s'])
            meta['events_planned'] = [dict(t_s=t, action=a, **args) for t, a, args in actions]
            timeline = threading.Thread(target=emu.run_timeline,
                                        args=(actions, t_source, str(self.out / 'faults.jsonl'), stop.is_set,
                                              self.cross_handlers(run_dir, bin_dir)),
                                        daemon=True)
            timeline.start()
            end = t_source + self.scen['join_s'] + self.seconds + 2
            src_name = dict((p.name, n) for p, n in procs)['source']
            while time.monotonic() < end:
                if not prov.state(src_name).get('Running', True):
                    break
                time.sleep(1.0)
            time.sleep(self.scen['drain_s'])
        finally:
            stop.set()
            if timeline:
                timeline.join(timeout=10)
            meta['ended'] = time.time()
            lane_names = [n for _, n in reversed(procs)]
            prov.stop(lane_names)
            prov.stop([n for n, _ in self.names if n not in lane_names], signal='TERM')
            meta['emulator_final'] = dict(nft=emu.counters(), qdisc=emu.qdisc_stats())
            emu.clear()
            meta['leftover_impairments'] = emu.leftovers()
            if path_meta:
                meta['path']['end'] = path_meta()
            meta['exit'] = {}
            for n, short in self.names:
                st = prov.state(n)
                meta['exit'][short] = dict(code=st.get('ExitCode'), oom=st.get('OOMKilled'), error=st.get('Error'))
                (self.out / f'{short}.log').write_text(prov.logs(n))
            prov.remove([n for n, _ in self.names])
            prov.collect(run_dir, self.out)
            if self.test.capture != 'none':
                self.compress_captures()
            (self.out / 'emu-samples.jsonl').write_text(''.join(json.dumps(s) + '\n' for s in self.samples))
            (self.out / 'meta.json').write_text(json.dumps(meta, indent=1, default=str) + '\n')
        summary = report.analyze(self.out)
        (self.out / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
        return summary

    def cid(self, proc):
        return f'{self.label.split("-", 1)[0]}-{proc.name}'
