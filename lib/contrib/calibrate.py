"""Calibration: what the emulator applies, measured with a sequenced UDP probe at the stream's packet
rate, against what each profile commands; and the host settings a run depends on.

Per profile and direction: measured loss against the commanded mean (with the expected spread of a
Gilbert-Elliott chain over that many packets), one-way delay and its spread, RTT, and reordering.
Events are not applied: the probe measures the steady profile.
"""
import json
import math
from pathlib import Path
import time

from fabric.report import pct, r3
from contrib import profiles, report
from contrib.lanes import Proc

PROBE_PORT = 7000
RMEM_MIN = 8 << 20


def host_settings(prov):
    """Socket buffer limits on the host kernel (global, not per namespace) and the receive buffer a
    socket in dst actually gets for an 8 MB request."""
    out = {}
    for key in ('rmem_max', 'wmem_max', 'rmem_default', 'wmem_default'):
        r = prov.sh(f'cat /proc/sys/net/core/{key} 2>/dev/null', check=False)
        out[key] = int(r.stdout.strip()) if r.stdout.strip().isdigit() else None
    rc, text = prov.exec('dst', 'python3 -c "import socket;s=socket.socket(2,2);'
                                's.setsockopt(1,8,8<<20);print(s.getsockopt(1,8))"')
    out['dst_rcvbuf_for_8MB'] = int(text.strip()) if rc == 0 and text.strip().isdigit() else None
    out['kernel'] = prov.sh('uname -r', check=False).stdout.strip()
    return out


def ensure_rmem(prov, minimum=RMEM_MIN):
    """Raise net.core.rmem_max to minimum when it is lower (sudo -n on the Docker host); returns
    (before, after)."""
    before = host_settings(prov).get('rmem_max')
    if before is not None and before < minimum:
        prov.sh(f'sudo -n sysctl -w net.core.rmem_max={minimum}', check=False)
    return before, host_settings(prov).get('rmem_max')


def host_info(prov):
    """host.json: the host settings with rmem raised; per node on a VM deployment."""
    if getattr(prov, 'nodes', None):
        return {ns: host_info(NodeView(prov, ns)) for ns in prov.nodes}
    before, after = ensure_rmem(prov)
    return dict(host_settings(prov), rmem_max_before=before, rmem_max_after=after)


def settings(prov):
    """host_settings, per node on a VM deployment."""
    if getattr(prov, 'nodes', None):
        return {ns: host_settings(NodeView(prov, ns)) for ns in prov.nodes}
    return host_settings(prov)


class NodeView:
    """One VM node as host_settings and ensure_rmem see a provider."""

    def __init__(self, prov, ns):
        self.node = prov.nodes[ns]

    def sh(self, script, check=True, timeout=300):
        return self.node.sh(script, check=check, timeout=timeout)

    def exec(self, ns, cmd):
        return self.node.exec(cmd)


def ge_sigma_pct(side, n):
    """Standard deviation (percent) of the loss rate measured over n packets: the Bernoulli term plus
    the covariance of the two-state chain, lag k decaying as (1 - p - r)^k."""
    if not side.loss or not n:
        return 0.0
    loss = side.mean_loss_pct() / 100
    var = loss * (1 - loss)
    if 'gemodel' in side.loss:
        p, r, bad, good = (v / 100 for v in side.loss['gemodel'])
        pb = p / (p + r) if p + r else 0.0
        lam = 1 - p - r
        if lam < 1:
            var += 2 * (bad - good) ** 2 * pb * (1 - pb) * lam / (1 - lam)
    return 100 * math.sqrt(max(var, 0.0) / n)


def loss_periods(received, sent):
    """RFC 3357 loss periods over sequence numbers 0..sent-1: (period lengths, distances between
    period starts), in packets."""
    lengths, starts, run = [], [], 0
    for seq in range(sent):
        if seq in received:
            if run:
                lengths.append(run)
                run = 0
        else:
            if not run:
                starts.append(seq)
            run += 1
    if run:
        lengths.append(run)
    return lengths, [b - a for a, b in zip(starts, starts[1:])]


def analyse(run, prof, pps, seconds):
    echo = report.rows(run / 'probe-echo.csv')
    back = report.rows(run / 'probe-send.csv')
    tail = [ln for ln in (run / 'probe-send.csv').read_text().splitlines() if ln.startswith('# sent ')]
    sent = int(tail[-1].split()[-1]) if tail else int(pps * seconds)
    got = {int(r['seq']): (int(r['t_send_ns']), int(r['t_recv_ns'])) for r in echo}
    owd = [(tr - ts) / 1e6 for ts, tr in got.values()]
    rtt = [(int(r['t_echo_ns']) - int(r['t_send_ns'])) / 1e6 for r in back]
    reordered, events, top, prev = 0, 0, -1, -1
    for r in echo:
        seq = int(r['seq'])
        if seq < top:
            reordered += 1
        if seq < prev:
            events += 1
        top, prev = max(top, seq), seq
    fwd_loss = 100 * (1 - len(got) / sent) if sent else None
    rev_loss = 100 * (1 - len(back) / len(got)) if got else None
    exp_f, exp_r = prof.forward.mean_loss_pct(), prof.reverse.mean_loss_pct()
    sig_f, sig_r = ge_sigma_pct(prof.forward, sent), ge_sigma_pct(prof.reverse, len(got))
    slot_f = prof.forward.slot_ms[1] if prof.forward.slot_ms else 0
    owd50 = pct(owd, 50)
    ok_loss = all(abs(m - e) <= 3 * s + 0.01 for m, e, s in ((fwd_loss, exp_f, sig_f), (rev_loss, exp_r, sig_r))
                  if m is not None)
    ok_delay = owd50 is not None and prof.forward.delay_ms <= owd50 <= prof.forward.delay_ms + slot_f + 2
    rtt50 = pct(rtt, 50)
    rtt_cmd = prof.forward.delay_ms + prof.reverse.delay_ms
    lengths, distances = loss_periods(got, sent)
    periods = dict(count=len(lengths), mean_pkts=r3(sum(lengths) / len(lengths)) if lengths else None,
                   p99_pkts=pct(lengths, 99) if lengths else None, max_pkts=max(lengths, default=None),
                   mean_ms=r3(1000 * sum(lengths) / len(lengths) / pps) if lengths and pps else None,
                   distance_mean_pkts=r3(sum(distances) / len(distances)) if distances else None)
    burst = prof.forward.burst_pkts()
    ok_rtt = rtt50 is not None and rtt_cmd <= rtt50 <= rtt_cmd + 2 * slot_f + 3
    return dict(profile=prof.id, packets=sent, fwd_loss_pct=r3(fwd_loss), fwd_expected_pct=r3(exp_f),
                fwd_sigma_pct=r3(sig_f), rev_loss_pct=r3(rev_loss), rev_expected_pct=r3(exp_r),
                rev_sigma_pct=r3(sig_r), owd_ms=dict(p1=r3(pct(owd, 1)), p50=r3(owd50), p99=r3(pct(owd, 99))),
                owd_commanded_ms=prof.forward.delay_ms, slot_ms=list(prof.forward.slot_ms or ()),
                rtt_ms=dict(p50=r3(rtt50), p99=r3(pct(rtt, 99))), rtt_commanded_ms=rtt_cmd,
                reordered=reordered, reorder_events=events, reorder_commanded_pct=prof.forward.reorder_pct,
                fwd_loss_periods=periods, fwd_burst_commanded_pkts=r3(burst) if burst else None,
                ok=bool(ok_loss and ok_delay and ok_rtt), ok_loss=ok_loss, ok_delay=ok_delay, ok_rtt=ok_rtt)


def run(prov, profile_ids, seconds, root, tier):
    bin_dir = prov.bin_dir()
    rows = []
    host = host_info(prov)
    (Path(root) / 'host.json').write_text(json.dumps(host, indent=1) + '\n')
    for i, pid in enumerate(profile_ids, 1):
        prof = profiles.load(pid)
        label = f'{i:02d}-cal-{pid}'
        out = Path(root) / label
        out.mkdir(parents=True, exist_ok=True)
        run_dir = prov.run_dir(label, out)
        emu = prov.emulator(tier.mux_bps)
        emu.build(prof)
        names = []
        try:
            echo = Proc('probe-echo', 'dst', 'tools',
                        f'python3 /contrib/bin/probe.py echo --bind 0.0.0.0:{PROBE_PORT} --out /out/probe-echo.csv')
            names.append(prov.start(echo, run_dir, bin_dir, f'{i:02d}-probe-echo'))
            time.sleep(1)
            send = Proc('probe-send', 'src', 'tools',
                        f'python3 /contrib/bin/probe.py send --to {prov.paths.dst_ip()}:{PROBE_PORT} '
                        f'--pps {tier.pps:.0f} --size 1316 --seconds {seconds} --out /out/probe-send.csv')
            names.append(prov.start(send, run_dir, bin_dir, f'{i:02d}-probe-send'))
            end = time.time() + seconds + 15
            while time.time() < end and prov.state(names[-1]).get('Running'):
                time.sleep(1)
            prov.stop(names[:1], signal='TERM')
            final = dict(qdisc=emu.qdisc_stats(), nft=emu.counters())
        finally:
            emu.clear()
            prov.remove(names)
        prov.collect(run_dir, out)
        row = analyse(out, prof, tier.pps, seconds)
        row['netem'] = {f"{q['dev']}:{q['handle']}": dict(packets=q['packets'], drops=q['drops'])
                        for q in final['qdisc']}
        (out / 'calibration.json').write_text(json.dumps(row, indent=1) + '\n')
        rows.append(row)
        print(line(row), flush=True)
    return rows


def line(r):
    return (f"{r['profile']:<21} fwd loss {r['fwd_loss_pct']}% (cmd {r['fwd_expected_pct']} ± {r['fwd_sigma_pct']}), "
            f"rev {r['rev_loss_pct']}% (cmd {r['rev_expected_pct']} ± {r['rev_sigma_pct']}), "
            f"OWD p1/p50/p99 {r['owd_ms']['p1']}/{r['owd_ms']['p50']}/{r['owd_ms']['p99']} ms (cmd {r['owd_commanded_ms']}"
            f"{' + slot ' + str(r['slot_ms']) if r['slot_ms'] else ''}), RTT p50 {r['rtt_ms']['p50']} ms "
            f"(cmd {r['rtt_commanded_ms']}), reordered {r['reordered']} in {r['reorder_events']} events, "
            f"loss periods {r['fwd_loss_periods']['count']} of mean {r['fwd_loss_periods']['mean_pkts']} pkts "
            f"(cmd {r['fwd_burst_commanded_pkts']})  "
            f"{'OK' if r['ok'] else 'CHECK'}")


def table(rows):
    return '\n'.join(line(r) for r in rows)
