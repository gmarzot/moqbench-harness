"""One contribution run (a cell): a lane and variant on a profile, from emulator build to collected files.

Order: emulator tree and counters; taps, analyser and probes; the lane's relay, receivers and senders;
the paced source; subscribers. The measurement window opens join_s after the first source datagram;
profile events are timed from it. At the end the source stops, the lane drains, everything stops, and
logs, counters and the summary land in the run folder.
"""
import json
from pathlib import Path
import shlex
import threading
import time

from contrib import emu as emulator
from contrib import lanes, report, sources
from contrib.lanes import Proc

STAGES = ('relay', 'receiver', 'sender', 'source', 'subscriber')


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
                             slot_max_ms=slot[1] if slot else 0.0, ingest=test.ingest, features=features or {})
        self.names = []
        self.samples = []

    def helpers(self):
        """Taps, analyser and probes for this lane."""
        bin_ = 'python3 /contrib/bin'
        frames = '--frames --hash' if self.lane.in_tap_hash() else '--frames'
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
        if self.lane.relay:
            ns, _, _ = self.prov.paths.relay_addresses(self.test.placement)
            if self.lane.name == 'moqt-aiomoqt-dual':
                ns = 'dst'
            procs.append(Proc('relay-admin', ns, 'tools',
                              f'{bin_}/sampler.py --url metrics=http://127.0.0.1:{lanes.ADMIN_PORT}/metrics '
                              f'--interval 1 --out /out/relay-admin.jsonl'))
        return procs

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
        emu = emulator.Emulator(prov.paths, prov.wan_exec, self.tier.mux_bps)
        meta = dict(label=self.label, lane=self.lane.name, variant=self.variant, repeat=self.repeat,
                    profile=self.profile.summary(self.tier.mux_bps), profile_id=self.profile.id,
                    rtt_ms=self.profile.rtt_ms, placement=self.test.placement, ingest=self.test.ingest,
                    source=dict(tier=self.tier.name, mux_bps=self.tier.mux_bps, file=name, codec=self.tier.codec),
                    seconds=self.seconds, join_s=self.scen['join_s'], break_ms=self.scen['break_ms'],
                    budgets_ms=self.test.budgets(self.profile.rtt_ms),
                    playout_ms=self.ctx.playout_ms, features=self.ctx.features, taps=list(self.lane.taps(self.ctx)),
                    seeds=self.profile.seeds(self.repeat), topology=dict(links=prov.paths.links))
        emu.build(self.profile, self.repeat)
        meta['emulator_build'] = emu.qdisc_stats()
        stop = threading.Event()
        sampler = threading.Thread(target=self.sample_emulator, args=(emu, stop), daemon=True)
        procs = []
        try:
            sampler.start()
            helpers = self.helpers()
            for p in helpers:
                self.names.append((prov.start(p, run_dir, bin_dir, self.cid(p), sources_dir=folder), p.name))
            time.sleep(1.0)
            lane_procs = self.lane.procs(self.ctx)
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
                                        args=(actions, t_source, str(self.out / 'faults.jsonl'), stop.is_set),
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
            meta['ended'] = time.time()
            lane_names = [n for _, n in reversed(procs)]
            prov.stop(lane_names)
            prov.stop([n for n, _ in self.names if n not in lane_names], signal='TERM')
            meta['emulator_final'] = dict(nft=emu.counters(), qdisc=emu.qdisc_stats())
            emu.clear()
            meta['leftover_impairments'] = emu.leftovers()
            meta['exit'] = {}
            for n, short in self.names:
                st = prov.state(n)
                meta['exit'][short] = dict(code=st.get('ExitCode'), oom=st.get('OOMKilled'), error=st.get('Error'))
                (self.out / f'{short}.log').write_text(prov.logs(n))
            prov.remove([n for n, _ in self.names])
            prov.collect(run_dir, self.out)
            (self.out / 'emu-samples.jsonl').write_text(''.join(json.dumps(s) + '\n' for s in self.samples))
            (self.out / 'meta.json').write_text(json.dumps(meta, indent=1, default=str) + '\n')
        summary = report.analyze(self.out)
        (self.out / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
        return summary

    def cid(self, proc):
        return f'{self.label.split("-", 1)[0]}-{proc.name}'
