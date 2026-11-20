"""Lanes: one transport each, as the processes a run starts in the src and dst namespaces.

Every lane reads the paced source from the src multicast group and delivers TS (or, for the LOC
fallback, H.264) to the dst output groups, where the common taps and the analyser read it. SRT and
RIST are one hop; MoQT lanes cross the emulated WAN between publisher and relay (first-mile),
between relay and subscriber (last-mile), or, with relay = "none", from the publisher straight to a
listening subscriber, and play their TS out on its PCR at fixed buffers.
"""
from dataclasses import dataclass, field
import itertools
import json
import math

from contrib import sources, topology

GROUP_IN = f'{sources.GROUP_IN}:{sources.PORT_IN}'
OUT = topology.GROUP_OUT
SRT_PORT, RIST_PORT, MOQT_PORT, ADMIN_PORT, INGEST_PORT, LOCAL_TS = 9000, 8193, 4433, 4500, 23106, 6010
NAMESPACE = 'test/spts'
REPAIR_HISTORY_S = 30    # seconds of the stream a repairing publisher keeps for FETCH
PY = 'python3 /contrib/bin'


@dataclass
class Proc:
    name: str
    ns: str
    image: str
    cmd: str = ''
    stage: str = 'sender'
    env: dict = field(default_factory=dict)
    entrypoint: bool = True
    settle_s: float = 0.0
    root: bool = False


@dataclass
class Ctx:
    """What a lane needs to render one run."""
    paths: object
    placement: str
    tier: object
    rtt_ms: float
    variant: dict
    playout_ms: list
    slot_max_ms: float = 0.0
    ingest: str = 'srt'
    features: dict = field(default_factory=dict)
    relay: str = 'moqx'


def outputs(playout_ms):
    """{tap name: (group, port, playout buffer or None)} of a MoQT lane: raw arrival, then each buffer."""
    out = {'raw': (OUT, topology.PORTS_OUT['raw'], None)}
    ports = [topology.PORTS_OUT['out'], topology.PORTS_OUT['second']] + [6003 + i for i in range(8)]
    for b, port in zip(playout_ms, ports):
        out[f'po{int(b)}'] = (OUT, port, int(b))
    return out


def srt_buffers(latency_ms, rtt_ms, mux_bps):
    """(fc packets, buffer bytes) holding (latency + RTT + 1 s) of input at 1.5x, doubled; never below
    libsrt's defaults."""
    rate = mux_bps / 8 * 1.5
    need = (latency_ms + rtt_ms + 1000) / 1000 * rate * 2
    fc = max(25600, math.ceil(need / 1316))
    return fc, fc * 1456


def rist_params(rtt_ms, slot_max_ms):
    return dict(rtt_min=max(5, int(rtt_ms / 2)), rtt_max=max(50, int(2 * rtt_ms + 40)),
                reorder=max(25, int(slot_max_ms + 5)))


def again(cmd, name):
    """A receiver that reconnects after it exits, one second apart, logging each exit; its stdout is
    one continuous stream for the next stage. On TERM or INT the loop passes TERM to the receiver and
    waits for it, so the receiver's end-of-run summary is written before the container stops."""
    return (f"trap 'kill -TERM $pid 2>/dev/null; wait $pid; exit 0' TERM INT; "
            f"while :; do {cmd} & pid=$!; wait $pid; "
            f"echo \"[harness] {name} exited rc=$?; reconnecting\" >&2; sleep 1; done")


def playout_cmd(source, playout_ms):
    outs = outputs(playout_ms)
    args = ' '.join(f'--out {b}:{g}:{p}' for name, (g, p, b) in outs.items() if b is not None)
    return f'{PY}/playout.py {source} {args} --raw {OUT}:{outs["raw"][1]} --log /out/playout.jsonl'


class Lane:
    name = ''
    codec = 'ts'
    relay = False
    # Run features the lane needs from an image, probed before a run: {feature: (image, command, text)}.
    needs = {}

    def variants(self, test, profile):
        return [{}]

    def cc_on_wan(self, placement):
        """The congestion-controller keys (pub_cc, relay_cc) this lane sweeps on the impaired hop."""
        return set()

    def taps(self, ctx):
        """{name: (group, port, playout buffer)} on dst."""
        return {'out': (OUT, topology.PORTS_OUT['out'], None)}

    def in_tap_hash(self, ctx):
        """The source tap's frame hash form ('vcl' or 'loc'), or None for no hashing."""
        return None

    def wall_clock(self, ctx):
        """True when the lane's output is a wall-clock log (sub_media's frame log), so the source tap
        stamps wall-clock time to match it instead of CLOCK_MONOTONIC."""
        return False

    def files(self, ctx):
        return {}

    def procs(self, ctx):
        raise NotImplementedError

    def pending(self, features, variant=None):
        """Why the lane cannot run this variant with these probed features, or None."""
        return None

    def label(self, variant):
        return '-'.join(f'{k}{v}' for k, v in variant.items())


class Srt(Lane):
    name = 'srt'

    def variants(self, test, profile):
        return [dict(D=d) for d in test.budgets(profile.rtt_ms)]

    def procs(self, ctx):
        d, t = ctx.variant['D'], ctx.tier
        fc, buf = srt_buffers(d, ctx.rtt_ms, t.mux_bps)
        pps = int(t.pps)
        rx = (f'srt-live-transmit -chunk:1316 -s:{pps} -pf:json -statsout:/out/srt-rcv.json '
              f'"srt://:{SRT_PORT}?mode=listener&latency={d}&lossmaxttl=32&fc={fc}&rcvbuf={buf}" '
              f'"udp://{OUT}:{topology.PORTS_OUT["out"]}?ttl=1"')
        tx = (f'srt-live-transmit -chunk:1316 -s:{pps} -pf:json -statsout:/out/srt-snd.json "udp://{GROUP_IN}" '
              f'"srt://{ctx.paths.dst_ip()}:{SRT_PORT}?mode=caller&latency={d}&maxbw=0&inputbw={t.mux_bps // 8}'
              f'&oheadbw=50&lossmaxttl=32&fc={fc}&sndbuf={buf}&rcvbuf={buf}"')
        return [Proc('srt-rcv', 'dst', 'tools', rx, 'receiver', settle_s=1),
                Proc('srt-snd', 'src', 'tools', tx, 'sender', settle_s=2)]


class Rist(Lane):
    name = 'rist'

    def variants(self, test, profile):
        return [dict(D=d) for d in test.budgets(profile.rtt_ms)]

    def procs(self, ctx):
        d, t = ctx.variant['D'], ctx.tier
        r = rist_params(ctx.rtt_ms, ctx.slot_max_ms)
        q = f'buffer={d}&rtt-min={r["rtt_min"]}&rtt-max={r["rtt_max"]}&reorder-buffer={r["reorder"]}'
        rx = (f'ristreceiver -p 1 -S 1000 -i "rist://@0.0.0.0:{RIST_PORT}?{q}" '
              f'-o "udp://{OUT}:{topology.PORTS_OUT["out"]}"')
        tx = (f'ristsender -p 1 -S 1000 -i "udp://@{GROUP_IN}" -o "rist://{ctx.paths.dst_ip()}:{RIST_PORT}?{q}'
              f'&bandwidth={int(t.mux_bps * 1.5 / 1000)}&congestion-control=1&max-retries=10"')
        return [Proc('rist-rcv', 'dst', 'tools', rx, 'receiver', settle_s=1),
                Proc('rist-snd', 'src', 'tools', tx, 'sender', settle_s=2)]


def moqx_env(cc):
    """The relay as the setup runs it, through the image's environment."""
    return dict(MOQX_INSECURE='true', MOQX_PORT=str(MOQT_PORT), MOQX_ADMIN_PORT=str(ADMIN_PORT),
                MOQX_CC=cc, MOQX_SEND_PKTS='16', MOQX_RECV_PKTS='256', MOQX_UDP_BUFFER='33554432',
                MOQX_THREADS='8', MOQX_CACHE='true', MOQX_IGNORE_PATH_MTU='true',
                MOQX_MOQT_VERSIONS='[18, 16]', MOQX_JEMALLOC='auto', MALLOC_CONF='thp:always')


def aiomoqt_relay_proc(ns, cc=None):
    """aiomoqt-relay on a self-signed certificate; the clients dial it insecure."""
    cmd = (f'{self_signed_tls()}python -m aiomoqt.tools.moq_interop_relay --quic --port {MOQT_PORT} '
           '--cert /tmp/cert.pem --key /tmp/priv.key')
    return Proc('relay', ns, 'aiomoqt', f'{cmd} --cc-algo {cc}' if cc else cmd, 'relay',
                env=dict(AIOPQUIC_GSO='0'), settle_s=3)


def relay_variant(test):
    """The relay implementation as a variant key, left out for the default (moqx)."""
    return {} if test.relay == 'moqx' else {'relay': test.relay}


def relay_cc_variant(cc):
    """The relay controller as a variant key; none without a relay."""
    return {} if cc is None else {'relay_cc': cc}


def per_budget(key, values, test, profile):
    """Variant keys for a per-budget option: none, or each budget D ("budget") or D/2 ("half")."""
    out = []
    for v in values:
        if v == 'none':
            out.append({})
        else:
            out += [{key: b if v == 'budget' else b // 2} for b in test.budgets(profile.rtt_ms)]
    return out


def deadline_variants(test, profile):
    """Publisher delivery timeouts as variant keys."""
    return per_budget('deadline', test.pub_deadline, test, profile)


def repair_variants(test, profile):
    """sub_media FETCH repair budgets as variant keys."""
    return per_budget('repair', test.sub_repair, test, profile)


def self_signed_tls():
    """Shell that writes a self-signed certificate to /tmp/cert.pem and /tmp/priv.key, ahead of a
    listener's command."""
    return ('openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=relay -keyout /tmp/priv.key '
            '-out /tmp/cert.pem 2>/dev/null && ')


class Moqt(Lane):
    relay = True
    direct = False

    def taps(self, ctx):
        return outputs(ctx.playout_ms)

    def cc_on_wan(self, placement):
        return {'pub_cc'} if placement == 'first-mile' else {'relay_cc'}

    def relay_impl(self, ctx):
        return ctx.relay

    def relay_ns(self, ctx):
        return ctx.paths.relay_addresses(ctx.placement)[0]

    def relay_proc(self, ctx):
        ns = self.relay_ns(ctx)
        if ctx.relay == 'none':
            return None
        if ctx.relay == 'aiomoqt':
            return aiomoqt_relay_proc(ns, ctx.variant.get('relay_cc', 'bbr'))
        return Proc('relay', ns, 'moqx', '', 'relay', env=moqx_env(ctx.variant.get('relay_cc', 'bbr2')),
                    entrypoint=False, settle_s=3)

    def urls(self, ctx):
        """(the URL the publisher dials, the subscriber's URL or listener flags); with no relay the
        subscriber listens in dst and the publisher dials it across the WAN, as SRT's caller does."""
        if ctx.relay == 'none':
            return f'moqt://{ctx.paths.dst_ip()}:{MOQT_PORT}/', f'-Q -p {MOQT_PORT} --cert /tmp/cert.pem --key /tmp/priv.key'
        _, pub, sub = ctx.paths.relay_addresses(ctx.placement)
        return f'moqt://{pub}:{MOQT_PORT}/moq-relay', f'moqt://{sub}:{MOQT_PORT}/moq-relay'


class Moq2ts(Moqt):
    name = 'moqt-moq2ts'

    def variants(self, test, profile):
        return [dict(relay_variant(test), relay_cc=cc) for cc in test.relay_cc]

    def cc_on_wan(self, placement):
        return {'relay_cc'} if placement == 'last-mile' else set()

    def files(self, ctx):
        cfg = {'srt_callers': [{'id': 'src', 'srt': {'mode': 'listener', 'host': '0.0.0.0', 'port': INGEST_PORT,
                                                     'latency_ms': 100}}]}
        return {'srt.json': json.dumps(cfg) + '\n'}

    def procs(self, ctx):
        pub_url, sub_url = self.urls(ctx)
        pub = (f'moq2ts-cli --endpoint {pub_url} --namespace {NAMESPACE} --transparent --draft 18 '
               '--quic-idle-timeout 3000 --insecure')
        procs = [self.relay_proc(ctx)]
        if ctx.ingest == 'srt':
            procs.append(Proc('moq2ts-pub', 'src', 'moq2ts', f'{pub} --srt-config /out/srt.json', 'sender', settle_s=1))
            procs.append(Proc('srt-ingest', 'src', 'tools',
                              f'tsp -I ip {GROUP_IN} -O srt --caller 127.0.0.1:{INGEST_PORT} --latency 100',
                              'sender', settle_s=1))
        else:
            procs.append(Proc('moq2ts-pub', 'src', 'moq2ts', f'{PY}/udp2pipe.py {GROUP_IN} | {pub} --video /dev/stdin',
                              'sender', settle_s=1))
        rx = again(f'moq2ts-receiver --url {sub_url} --namespace {NAMESPACE} --draft 18 --insecure --out - '
                   '--stats-json /out/moq2ts-receiver.json', 'moq2ts-receiver')
        rx = f'{{ {rx}; }} | {playout_cmd("--stdin", ctx.playout_ms)}'

        procs.append(Proc('moq2ts-rcv', 'dst', 'moq2ts', rx, 'subscriber', settle_s=1))
        return procs


PUB_MEDIA = 'python -m aiomoqt.tools.pub_media'
SUB_MEDIA = 'python -m aiomoqt.tools.sub_media'
STAMP = '$(date +%s%N)'


class AiomoqtLoc(Moqt):
    """pub_media repackages the source's H.264 and AAC as LOC; sub_media logs every frame it receives
    with the sha1 of its payload, which the source tap computes the same way."""
    name = 'moqt-aiomoqt-loc'
    codec = 'h264'
    needs = {'ts_udp': ('aiomoqt', f'{PUB_MEDIA} -h', 'udp://'),
             'frame_log': ('aiomoqt', f'{SUB_MEDIA} -h', '--frame-log'),
             'delivery_timeout': ('aiomoqt', f'{PUB_MEDIA} -h', '--delivery-timeout')}
    namespace = 'test/loc'
    pub_extra = ''
    direct = True

    def variants(self, test, profile):
        return [dict(relay_variant(test), pub_cc=p, **relay_cc_variant(r), **dl)
                for p, r, dl in itertools.product(test.pub_cc, test.relay_cc, deadline_variants(test, profile))]

    def in_tap_hash(self, ctx):
        return 'vcl' if self.fallback(ctx) else 'loc'

    def wall_clock(self, ctx):
        return not self.fallback(ctx)

    def fallback(self, ctx):
        return not ctx.features.get('frame_log')

    def taps(self, ctx):
        return {}

    def pending(self, features, variant=None):
        if (variant or {}).get('deadline') and not features.get('delivery_timeout'):
            return 'aiomoqt lacks pub_media --delivery-timeout'
        return None

    def pub_cmd(self, ctx, url):
        src = f'--ts udp://@{GROUP_IN}' if ctx.features.get('ts_udp') else '--ts -'
        mode = ' --pub-ns' if ctx.relay == 'none' else ''
        cmd = (f'{PUB_MEDIA} {url} -k{mode} --draft 18 -N {self.namespace} {src}{self.pub_extra} '
               f'--cc-algo {ctx.variant["pub_cc"]} --keepalive 10 --reconnect -t 0 --stats-json')
        if ctx.variant.get('deadline'):
            cmd += f' --delivery-timeout {ctx.variant["deadline"]}'
        return cmd if ctx.features.get('ts_udp') else f'{PY}/udp2pipe.py {GROUP_IN} | {cmd}'

    def sub_cmd(self, url, ctx=None):
        """sub_media dialing `url`, or listening when `url` holds listener flags (relay = "none")."""
        insecure = '' if ctx is not None and ctx.relay == 'none' else ' -k'
        return f'{SUB_MEDIA} {url}{insecure} --draft 18 -N {self.namespace} -t 0'

    def tls(self, ctx):
        """The listener's certificate step ahead of a direct subscriber; nothing otherwise."""
        return self_signed_tls() if ctx.relay == 'none' else ''

    def sub_proc(self, ctx, cmd, env):
        """sub_media's process; a direct subscriber listens, so it starts ahead of the publisher."""
        if ctx.relay == 'none':
            return Proc('aiomoqt-sub', 'dst', 'aiomoqt', cmd, 'receiver', env=env, settle_s=3)
        return Proc('aiomoqt-sub', 'dst', 'aiomoqt', cmd, 'subscriber', env=env, settle_s=1)

    def procs(self, ctx):
        pub_url, sub_url = self.urls(ctx)
        env = dict(AIOPQUIC_GSO='0')
        procs = [self.relay_proc(ctx), Proc('aiomoqt-pub', 'src', 'aiomoqt', self.pub_cmd(ctx, pub_url), 'sender',
                                            env=env, settle_s=2)]
        procs += self.receiver_procs(ctx, sub_url, env)
        return [p for p in procs if p is not None]

    def receiver_procs(self, ctx, url, env):
        if self.fallback(ctx):
            rx = again(f'{self.sub_cmd(url, ctx)} --out /tmp/media --pipe video', 'sub_media')
            return [self.sub_proc(ctx, f'{self.tls(ctx)}{{ {rx}; }} | {PY}/tstap.py --stdin raw --annexb --out /out', env)]
        rx = (f'{self.sub_cmd(url, ctx)} --analyze -i 5 --report /out/sub-analyze-{STAMP}.jsonl '
              f'--frame-log /out/frames-{STAMP}.jsonl')
        return [self.sub_proc(ctx, self.tls(ctx) + again(rx, 'sub_media'), env)]


class AiomoqtMsfts(AiomoqtLoc):
    """pub_media carries the source's TS packets unmodified (MSFTS, 7 per object); sub_media writes
    them out on arrival to the common playout and taps."""
    name = 'moqt-aiomoqt-msfts'
    codec = 'ts'
    needs = {'ts_udp': AiomoqtLoc.needs['ts_udp'], 'ts_out': ('aiomoqt', f'{SUB_MEDIA} -h', '--ts-out'),
             'msfts': ('aiomoqt', f'{PUB_MEDIA} -h', '--msfts'),
             'delivery_timeout': AiomoqtLoc.needs['delivery_timeout'],
             'repair': ('aiomoqt', f'{SUB_MEDIA} -h', '--repair')}
    namespace = 'test/msfts'
    pub_extra = ' --msfts --msfts-packets 7'

    def variants(self, test, profile):
        return [dict(relay_variant(test), pub_cc=p, **relay_cc_variant(r), timing=t, **dl, **rp)
                for p, r, t, dl, rp in itertools.product(test.pub_cc, test.relay_cc, test.pub_timing,
                                                         deadline_variants(test, profile),
                                                         repair_variants(test, profile))]

    def pub_cmd(self, ctx, url):
        cmd = super().pub_cmd(ctx, url)
        if ctx.variant.get('timing') == 'mapping':
            cmd = cmd.replace(self.pub_extra, self.pub_extra + ' --timestamp-mapping')
        if ctx.variant.get('repair'):
            cmd = cmd.replace(self.pub_extra, f'{self.pub_extra} --fetch-history {REPAIR_HISTORY_S * ctx.tier.mux_bps // 8}')
        return cmd

    def in_tap_hash(self, ctx):
        return None

    def wall_clock(self, ctx):
        return False

    def taps(self, ctx):
        return outputs(ctx.playout_ms)

    def pending(self, features, variant=None):
        missing = [f for f in ('ts_udp', 'msfts', 'ts_out') if not features.get(f)]
        if missing:
            return f'aiomoqt lacks {", ".join(missing)} (pub_media --ts udp:// and --msfts, sub_media --ts-out)'
        if (variant or {}).get('repair') and not features.get('repair'):
            return 'aiomoqt lacks sub_media --repair'
        return super().pending(features, variant)

    def receiver_procs(self, ctx, url, env):
        repair = f' --repair {ctx.variant["repair"]}' if ctx.variant.get('repair') else ''
        rx = (f'{self.sub_cmd(url, ctx)} --ts-out udp://127.0.0.1:{LOCAL_TS}{repair} --analyze -i 5 '
              f'--report /out/sub-analyze-{STAMP}.jsonl --arrival-log /out/arrival-{STAMP}.jsonl')
        return [self.sub_proc(ctx, self.tls(ctx) + again(rx, 'sub_media'), env),
                Proc('playout', 'dst', 'tools', playout_cmd(f'--udp 127.0.0.1:{LOCAL_TS}', ctx.playout_ms), 'receiver')]


class AiomoqtDual(AiomoqtMsfts):
    """One aiomoqt-relay in dst reached over two independent paths; the relay merges the two
    publisher sessions by Location."""
    name = 'moqt-aiomoqt-dual'
    needs = dict(AiomoqtMsfts.needs, relay=('aiomoqt', 'python -m aiomoqt.tools.moq_interop_relay -h', '--port'))
    direct = False

    def cc_on_wan(self, placement):
        return {'pub_cc'}

    def relay_impl(self, ctx):
        return 'aiomoqt'

    def relay_ns(self, ctx):
        return 'dst'

    def relay_proc(self, ctx):
        return aiomoqt_relay_proc('dst')

    def pub_cmd(self, ctx, url):
        urls = ' '.join(f'moqt://{ctx.paths.dst_ip(k)}:{MOQT_PORT}/moq-relay' for k in range(len(ctx.paths.links)))
        return super().pub_cmd(ctx, urls)

    def urls(self, ctx):
        return None, f'moqt://127.0.0.1:{MOQT_PORT}/moq-relay'


LANES = {cls.name: cls() for cls in (Srt, Rist, Moq2ts, AiomoqtLoc, AiomoqtMsfts, AiomoqtDual)}
