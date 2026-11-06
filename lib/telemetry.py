"""Rates from monitor samples, and the load tools' per-interval log lines."""
import calendar
import re


def cpu(text):
    result = {}
    for line in text.splitlines():
        key, *fields = line.split()
        if key.startswith('cpu'):
            v = list(map(int, fields))[:8]  # guest time is already included in user/nice
            result[key] = (sum(v), v[3] + v[4])
    return result


def net(text):
    return {line.split(':')[0].strip(): list(map(int, line.split(':')[1].split()))
            for line in text.splitlines() if ':' in line}


def udp(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith('Udp:'):
            return dict(zip(line.split()[1:], map(int, lines[i+1].split()[1:])))
    return {}


def rates(a, b, hz):
    dt = b['monotonic'] - a['monotonic']
    ac, bc = cpu(a['cpu']), cpu(b['cpu'])
    busy = {k: 100 * (1 - (bc[k][1] - ac[k][1]) / max(1, bc[k][0] - ac[k][0]))
            for k in ac.keys() & bc.keys()}
    an, bn = net(a['netdev']), net(b['netdev'])
    interfaces = (an.keys() & bn.keys()) - {'lo'}
    au, bu = udp(a['snmp']), udp(b['snmp'])
    ap, bp = a.get('process'), b.get('process')
    same = bool(ap and bp and ap['pid'] == bp['pid'] and ap['start_ticks'] == bp['start_ticks']
                and bp.get('state') != 'Z')
    row = dict(t_wall=b['t_wall'], interval_s=dt, host_busy_pct=busy['cpu'],
               hottest_cpu_pct=max(v for k, v in busy.items() if k != 'cpu'),
               cpus_over_90_pct=sum(v >= 90 for k, v in busy.items() if k != 'cpu'),
               process_cores=(bp['ticks']-ap['ticks'])/hz/dt if same else '',
               process_rss_bytes=bp['rss_bytes'] if bp else '',
               net_tx_mbps=sum(bn[k][8]-an[k][8] for k in interfaces)*8/dt/1e6,
               net_rx_mbps=sum(bn[k][0]-an[k][0] for k in interfaces)*8/dt/1e6,
               net_tx_kpps=sum(bn[k][9]-an[k][9] for k in interfaces)/dt/1e3,
               net_rx_kpps=sum(bn[k][1]-an[k][1] for k in interfaces)/dt/1e3)
    for key in ('InErrors', 'RcvbufErrors', 'SndbufErrors', 'InDatagrams', 'OutDatagrams'):
        row['udp_' + key + '_delta'] = bu.get(key, 0) - au.get(key, 0)
    row['nic_allowance_exceeded_delta'] = (sum(b.get('nic_allowance', {}).values())
                                           - sum(a.get('nic_allowance', {}).values()))
    return row


def allowance_deltas(samples):
    """Whole-run growth of each NIC allowance counter; {} when none were recorded."""
    seen = [s['nic_allowance'] for s in samples if s.get('nic_allowance')]
    return {k: seen[-1].get(k, 0) - seen[0][k] for k in seen[0]} if seen else {}


def steal_pct(a, b):
    """Host CPU stolen by the hypervisor between two samples, percent."""
    fa, fb = (list(map(int, s['cpu'].splitlines()[0].split()[1:9])) for s in (a, b))
    return 100 * (fb[7] - fa[7]) / max(1, sum(fb) - sum(fa))


def hottest_thread_pct(a, b, hz):
    """The busiest thread of the monitored process, percent of one CPU."""
    ta, tb = a.get('threads') or {}, b.get('threads') or {}
    dt = b['monotonic'] - a['monotonic']
    deltas = [tb[k]['ticks'] - ta[k]['ticks'] for k in tb.keys() & ta.keys()]
    return 100 * max(deltas) / hz / dt if deltas and dt > 0 else None


UNIT = {'us': 1e-3, 'ms': 1.0, 's': 1e3}
BW = {'Kbps': 1e-6, 'Mbps': 1e-3, 'Gbps': 1.0}
ROW = re.compile(r'^\s*(\d+)s\s+([\d.]+)(Kbps|Mbps|Gbps)\s+(\d+)\s+│\s+([\d.]+)(Kbps|Mbps|Gbps)\s+([\d.]+)(Kbps|Mbps|Gbps)'
                 r'\s+(\d+)\s+│\s+(n/a|[\d.]+(?:us|ms|s))\s+(n/a|[\d.]+(?:us|ms|s))\s+(n/a|[\d.]+(?:us|ms|s))\s+│'
                 r'\s+([\d.]+)%\s+(.*)$')
AGG = re.compile(r'^I(\d\d)(\d\d) (\d\d):(\d\d):(\d\d)\.\d+ .*\[AGGREGATE\] \[(\d+)s\] Subs: (\d+) \| Obj/s: (\d+) '
                 r'\| Mbps: ([\d.]+) .*Latency\(interval min/avg/max\): ([\d.]+)/([\d.]+)/([\d.]+) ms '
                 r'\| Resets: (\d+)/s, (\d+) total \| Failures: (\d+)/s, (\d+) total')


def ms(v):
    if v == 'n/a':
        return None
    m = re.match(r'([\d.]+)(us|ms|s)$', v)
    return float(m.group(1)) * UNIT[m.group(2)]


def calendar_seconds(year, stamp):
    """Seconds since the epoch of a glog stamp's month, day, hour, minute and second, UTC."""
    return calendar.timegm((year, *map(int, stamp)))
