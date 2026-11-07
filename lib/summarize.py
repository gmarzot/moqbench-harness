#!/usr/bin/env python3
"""Reduce one run to a CSV row, including the pass/fail health verdict.

Reads the snapshots and log streams run_point leaves in /tmp: .r[01] (relay
CPU), .c[01].N (client CPU), .cli.N (client stdout), .prom.N (client Prometheus
latency histogram).

A run is healthy only if all three hold: p99 latency within budget, resets at
or below tolerance, and the subscriber count actually sustained. The third
matters because a relay that sheds subscribers can otherwise post excellent
latency for whoever survived.
"""
import os
from statistics import median

NCLI = int(os.environ["NCLI"])
BUSY_CAP = float(os.environ["CLIENT_MAX_BUSY"])
SLO_MS = float(os.environ["SLO_MS"])
SLO_PCT = float(os.environ["SLO_PCT"])
# Absolute floor plus a share of subscriber count. A flat zero proved too
# strict: --send-pkts 48 shows one reset per subscriber that 16 does not, at
# identical throughput and better latency, so a zero gate would disqualify the
# faster configuration on a difference that has no visible cost. Raw counts are
# always reported.
MAX_RESETS = int(os.environ["MAX_RESETS"])
RESET_PCT = float(os.environ.get("RESET_PCT", "2"))
# Absolute floor beneath the percentage. At the low subscriber counts a 16 Mbps
# profile reaches, 2% of target rounds below one, so a single reset failed the
# run - and with a delivery timeout set, dropping one late object is the relay
# behaving correctly rather than failing. Raw counts are always reported, so a
# genuine reset storm is still visible in the row.
RESET_FLOOR = int(os.environ.get("RESET_FLOOR", "3"))
TARGET = int(os.environ["SUBS"])
# Share of the requested subscribers that must be sustained. Not 98: a dozen
# subscribers routinely fail to attach during the ramp at high counts (500
# requested, 488 held), and at 98 the search converges on that attach artifact
# instead of the relay's actual ceiling. Real shedding is a collapse, not a
# rounding error. The held count is always reported so the margin stays visible.
SHED_PCT = float(os.environ.get("SHED_PCT", "95"))
# Delivered object rate per subscriber, as a share of what the publisher emits.
# The publisher runs a fixed clock (one object every OBJ_DELAY_MS), so a relay
# that cannot keep up delivers fewer objects per second per subscriber. This is
# a coarser signal than latency, but it needs no timestamp extension and so
# works for every relay - including moq-dev, whose latency is unmeasurable
# because moqtest carries its timestamp in a private extension (0xC000) that
# moq-dev does not recognise. Set RATE_PCT=0 to disable.
# Off by default: the publisher's real cadence is slower than 1000/OBJ_DELAY_MS,
# so even at a trivial load a 95% gate fails everything. Enable only
# with a threshold calibrated from a low-load run on the same hardware.
RATE_PCT = float(os.environ.get("RATE_PCT", "0"))
OBJ_DELAY_MS = float(os.environ.get("OBJ_DELAY_MS", "33"))
JIFFY = 100.0  # USER_HZ


def snap(path):
    """First line is the host aggregate + process jiffies; the rest per-core."""
    try:
        lines = open(path).read().strip().splitlines()
        total, idle, proc, ncpu = lines[0].split()
        per = [tuple(float(x) for x in ln.split()) for ln in lines[1:]]
        return float(total), float(idle), float(proc), int(ncpu), per
    except Exception:
        return None


def hottest_core_pct(a, b):
    """Utilisation of the busiest single core. A host average cannot reveal one
    pegged CPU, which is the usual shape of a single-RX-queue softirq limit."""
    if not a or not b or not a[4] or len(a[4]) != len(b[4]):
        return None
    worst = None
    for (ta, ia), (tb, ib) in zip(a[4], b[4]):
        dt, di = tb - ta, ib - ia
        if dt > 0:
            pct = 100.0 * (dt - di) / dt
            worst = pct if worst is None else max(worst, pct)
    return worst


def busy_pct(a, b):
    if not a or not b:
        return None
    dt, di = b[0] - a[0], b[1] - a[1]
    return 100.0 * (dt - di) / dt if dt > 0 else None


def wall_seconds(a, b):
    """/proc/stat jiffies accumulate across every core, so elapsed time is the
    total delta divided by the core count."""
    return (b[0] - a[0]) / JIFFY / a[3] if a and b and a[3] else None


def proc_cores(a, b):
    w = wall_seconds(a, b)
    return (b[2] - a[2]) / JIFFY / w if w else None


def host_cores(a, b):
    """Busy cores across the host. This, not process CPU, is the honest
    denominator for Mbps/core: softirq packet processing is real cost
    that is never charged to the relay process."""
    bp = busy_pct(a, b)
    return bp * a[3] / 100.0 if bp is not None and a else None


def parse_client(path):
    """One [AGGREGATE] line per second carries everything: live subscriber
    count, throughput, interval latency min/avg/max, and cumulative resets.

    Resets are returned per-row as a running total rather than a single final
    figure: the publisher ends the track at lastGroupInTrack, which resets one
    stream per subscriber at teardown. Counting those would fail healthy runs.
    """
    rows, bad = [], {"Resets": 0, "Failures": 0}
    pctiles = {}
    try:
        for line in open(path):
            if "[AGGREGATE]" not in line:
                continue
            fields = {}
            for seg in line.split("|"):
                if ":" not in seg:
                    continue
                k, _, v = seg.partition(":")
                # The first segment still carries the "[AGGREGATE] [12s] "
                # prefix, so keep only what follows the last bracket.
                fields[k.rsplit("]", 1)[-1].strip()] = v.strip()
            try:
                mbps = float(fields["Mbps"])
                subs = int(fields["Subs"])
                objs = int(fields["Obj/s"])
            except (KeyError, ValueError):
                continue
            if mbps >= 20000:   # counter corruption seen on the Linode runs
                continue
            # Cumulative percentiles over every object delivered so far. This
            # is the gate: an average hides the tail and an interval maximum is
            # a single sample, so neither supports a latency budget.
            pct = fields.get("Latency(p50/p90/p99)", "")
            parts_p = pct.replace("ms", "").strip().split("/")
            if len(parts_p) == 3:
                try:
                    pctiles = {
                        50: float(parts_p[0]),
                        90: float(parts_p[1]),
                        99: float(parts_p[2]),
                    }
                except ValueError:
                    pass
            maxlat = None
            ivl = fields.get("Latency(interval min/avg/max)", "")
            parts = ivl.replace("ms", "").strip().split("/")
            if len(parts) == 3:
                try:
                    maxlat = float(parts[2])
                except ValueError:
                    pass
            # Resets and Failures are both cumulative "N/s, M total"; a
            # subscriber that never attached is as much a failure to serve as
            # one that was torn down.
            for key in ("Resets", "Failures"):
                v = fields.get(key, "")
                if "total" in v:
                    try:
                        bad[key] = int(v.split(",")[1].split()[0])
                    except (IndexError, ValueError):
                        pass
            rows.append((subs, objs, mbps, maxlat,
                         bad["Resets"] + bad["Failures"],
                         pctiles.get(SLO_PCT)))
    except FileNotFoundError:
        return None
    return rows


def steady_window(rows):
    """Samples from the moment attachment finishes.

    A fixed fraction does not work: the ramp takes subscribers/rate seconds, so
    at high counts it outlasts the discarded prefix and the still-climbing
    samples pull the median down, which reads as shedding when nothing is
    actually saturated. Start instead at the first sample within 2% of peak
    subscribers. Real shedding still registers, because it happens after the
    peak and so stays inside the window.
    """
    if not rows:
        return rows
    peak = max(r[0] for r in rows)
    if peak == 0:
        return rows
    start = next(i for i, r in enumerate(rows) if r[0] >= peak * 0.98)
    # Keep enough samples to be a measurement rather than a snapshot.
    if len(rows) - start < 3:
        start = max(0, len(rows) - 3)
    return rows[start:]


def merge_hist(paths):
    """Sum the per-client cumulative histograms into one bucket->count map."""
    buckets, total = {}, 0
    for p in paths:
        try:
            for line in open(p):
                if line.startswith("moqperf_object_latency_seconds_bucket"):
                    le = line.split('le="')[1].split('"')[0]
                    n = float(line.rsplit(None, 1)[1])
                    key = float("inf") if le == "+Inf" else float(le) * 1000
                    buckets[key] = buckets.get(key, 0.0) + n
                elif line.startswith("moqperf_object_latency_seconds_count"):
                    total += float(line.rsplit(None, 1)[1])
        except FileNotFoundError:
            continue
    return buckets, total


def percentile_ms(buckets, total, pct):
    """Bucket-resolution percentile: the smallest bound whose cumulative count
    covers pct. Buckets are already cumulative (Prometheus '<= le'), so no
    interpolation is done or implied."""
    if not buckets or total <= 0:
        return None
    need = total * pct / 100.0
    for bound in sorted(buckets):
        if buckets[bound] >= need:
            return bound
    return float("inf")


r0, r1 = snap("/tmp/.r0"), snap("/tmp/.r1")
relay_busy = busy_pct(r0, r1)
relay_proc = proc_cores(r0, r1)
relay_host = host_cores(r0, r1)
relay_hot = hottest_core_pct(r0, r1)

mbps = objs = 0.0
subs_held = 0
resets_total = 0
client_busy = []
client_hot = []
interval_max = []
reported_pctile = []
have_data = False
for i in range(NCLI):
    rows = parse_client(f"/tmp/.cli.{i}")
    if rows:
        have_data = True
        # Teardown lines report zero subscribers; counting them as shedding
        # would fail every run.
        while rows and rows[-1][0] == 0:
            rows.pop()
        steady = steady_window(rows)
        mbps += median(r[2] for r in steady)
        objs += median(r[1] for r in steady)
        # Median, not peak: a relay that briefly reaches the target and then
        # sheds has not sustained it, and one dropped sample is not shedding.
        subs_held += median(r[0] for r in steady)
        interval_max += [r[3] for r in steady if r[3] is not None]
        # Percentiles are cumulative, so the last sample is the run-wide value.
        tail = [r[5] for r in steady if r[5] is not None]
        if tail:
            reported_pctile.append(tail[-1])
        # Resets accrued during the window, excluding the final samples where
        # the publisher's end-of-track resets every subscriber's stream.
        scored = steady[:-2] if len(steady) > 4 else steady
        resets_total += max(0, scored[-1][4] - scored[0][4])
    ca, cb = snap(f"/tmp/.c0.{i}"), snap(f"/tmp/.c1.{i}")
    b = busy_pct(ca, cb)
    if b is not None:
        client_busy.append(b)
    # A client with one saturated core is client-bound even at low average.
    h = hottest_core_pct(ca, cb)
    if h is not None:
        client_hot.append(h)

# Prefer the histogram's p99. Without it, gate on the worst interval max, which
# is a stricter test than p99 and is labelled so the two are never conflated.
# Prefer the client's own percentile over actual objects; fall back only if an
# older client that does not report it is deployed.
if reported_pctile:
    latency = max(reported_pctile)
else:
    buckets, count = merge_hist([f"/tmp/.prom.{i}" for i in range(NCLI)])
    latency = percentile_ms(buckets, count, SLO_PCT)
    if latency is None and interval_max:
        latency = max(interval_max)
cmax = max(client_busy) if client_busy else None
chot = max(client_hot) if client_hot else None

# Expected per-subscriber object rate from the publisher's emission clock.
expected_rate = 1000.0 / OBJ_DELAY_MS if OBJ_DELAY_MS > 0 else 0
actual_rate = (objs / subs_held) if subs_held else 0

if not have_data:
    status = "NO_DATA"
elif cmax is not None and cmax > BUSY_CAP:
    status = "CLIENT_BOUND"                  # not the relay's limit; unusable
elif chot is not None and chot > 97:
    status = "CLIENT_CORE_BOUND"             # one client core pegged
elif subs_held < TARGET * SHED_PCT / 100.0:
    status = f"SHED_{subs_held:.0f}"
elif resets_total > max(MAX_RESETS, RESET_FLOOR, TARGET * RESET_PCT / 100.0):
    status = f"RESETS_{resets_total}"
elif latency is None:
    status = "NO_LATENCY"
elif latency > SLO_MS:
    status = f"SLO_p{SLO_PCT:.0f}_{latency:.0f}ms"
elif RATE_PCT and expected_rate and actual_rate < expected_rate * RATE_PCT / 100.0:
    status = f"RATE_{actual_rate:.1f}of{expected_rate:.1f}"
else:
    status = "ok"


def f(v, p=1):
    if v is None:
        return ""
    return "inf" if v == float("inf") else f"{v:.{p}f}"


print(",".join([
    os.environ["CFG"], os.environ["THREADS"], os.environ["SUBS"],
    f(mbps), f(objs, 0), f(relay_proc, 2), f(relay_host, 2),
    f(mbps / relay_host if relay_host else None),
    f(latency, 0), f(actual_rate, 1), str(resets_total), f(subs_held, 0),
    f(relay_busy), f(relay_hot), f(cmax), f(chot), status,
]))
