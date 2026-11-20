"""moqbench contrib: compare contribution transports (MoQT lanes, SRT, RIST) over an emulated WAN.

  moqbench contrib up <deployment> [--dual-path]    namespaces, veths, routes (VMs: holders, ifb); offloads off
  moqbench contrib down <deployment>                remove every container (VMs: and the network setup)
  moqbench contrib check <deployment>               reachability, offloads, leftover impairments, clock sync
  moqbench contrib source <deployment> <tier> <seconds>   encode a source fixture
  moqbench contrib calibrate <deployment> [--profile P ...] [--seconds S]
  moqbench contrib run <deployment> <suite> [--only N] [--lane L ...] [--profile P ...]
                       [--repeats N] [--seconds S] [--scenario NAME] [--keep]
  moqbench contrib report <run folder> ...
  moqbench contrib explain <results or run folder> ...   why late stretches were late; explain.json, explain.md
  moqbench contrib render-vm <deployment>           the network commands a VM deployment runs per node

A docker deployment runs on one host; an aws or oci deployment runs on its src and dst VMs over ssh.
"""
import argparse
import datetime
import json
from pathlib import Path
import sys

import config
from fabric.docker import FabricError
from contrib import calibrate, explain, lanes, profiles, report, run, sources, spec, topology
from contrib.docker import DockerContrib
from contrib.vm import VmContrib

ROOT = Path(__file__).resolve().parents[2]


def provider(dep, dual=False):
    if dep.provider == 'docker':
        return DockerContrib(dep, dual=dual)
    return VmContrib(dep, dual=dual)


def render_vm(dep, nic='<nic>'):
    """{node: commands} up runs in each VM's holder, with the interface shown as nic."""
    paths = topology.VmPaths(dep.nodes['src']['address'], dep.nodes['dst']['address'], lambda ns: nic)
    return {ns: paths.setup_cmds(ns) for ns in ('src', 'dst')}


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')


def budget(value, prof, test):
    """A filter value; NxRTT names the budget N x max(RTT, floor) of the profile."""
    if value.endswith('xRTT'):
        return str(int(round(float(value[:-4]) * max(prof.rtt_ms, test.rtt_floor_ms))))
    return value


def cells(suite, a):
    """(test, lane, profile, repeat, variant) for every run the suite and filters select."""
    for i, test in enumerate(suite.tests, 1):
        if a.only and i not in a.only:
            continue
        for lane_name in test.lanes:
            if a.lane and lane_name not in a.lane:
                continue
            lane = lanes.LANES[lane_name]
            for pid in test.profiles:
                if a.profile and pid not in a.profile:
                    continue
                prof = profiles.load(pid)
                for repeat in range(a.repeats or test.repeats):
                    for variant in lane.variants(test, prof):
                        if all(k not in variant or str(variant[k]) == budget(v, prof, test) for k, v in
                               (f.split('=', 1) for f in a.variant or [])):
                            yield test, lane, prof, repeat, variant


def cmd_run(a):
    dep = spec.load_deployment(a.deployment)
    suite = spec.load_suite(a.suite)
    dual = any(t.dual_path for t in suite.tests)
    prov = provider(dep, dual)
    prov.ids()
    root = ROOT / 'results' / 'contrib' / f'{dep.name}-{stamp()}'
    root.mkdir(parents=True, exist_ok=True)
    topo = prov.up()
    host = dict(calibrate.host_info(prov), topology=topo)
    (root / 'host.json').write_text(json.dumps(host, indent=1) + '\n')
    print(f'host: {json.dumps(host)}', flush=True)
    feature_cache, summaries, status = {}, [], 0
    try:
        for k, (test, lane, prof, repeat, variant) in enumerate(cells(suite, a), 1):
            scenario = spec.load_scenario(a.scenario or test.scenario)
            if a.seconds:
                scenario = dict(scenario, seconds=a.seconds)
            features = run.probe_features(prov, lane, feature_cache)
            why = lane.pending(features, variant)
            if lane.name == 'moqt-aiomoqt-dual' and not test.dual_path:
                why = 'needs dual_path = true'
            if not why and hasattr(prov, 'pending'):
                why = prov.pending(lane, test, features)
            label = f'{k:03d}-{lane.name}-{prof.id}-{lane.label(variant)}-r{repeat + 1}'
            if why:
                print(f'{label}: PENDING ({why})', flush=True)
                summaries.append(dict(label=label, lane=lane.name, profile=prof.id, variant=variant,
                                      repeat=repeat, verdict='PENDING', reason=why))
                continue
            cell = run.Cell(prov, test, scenario, lane, prof, variant, repeat, label, root, features)
            s = cell.execute()
            summaries.append(s)
            print(report.line(s), flush=True)
            if s['verdict'] == 'FAIL':
                status = 1
    finally:
        if not a.keep:
            prov.down()
    (root / 'suite.json').write_text(json.dumps(summaries, indent=1, default=str) + '\n')
    print(report.table(summaries))
    print(f'results in {root}')
    return status


def cmd_calibrate(a):
    dep = spec.load_deployment(a.deployment)
    if dep.provider != 'docker':
        raise config.ConfigError(f'{dep.name}: calibration measures one-way delay on one clock; it runs on '
                                 'a docker deployment')
    prov = provider(dep)
    prov.ids()
    root = ROOT / 'results' / 'contrib' / f'{dep.name}-calibration-{stamp()}'
    root.mkdir(parents=True, exist_ok=True)
    prov.up()
    try:
        rows = calibrate.run(prov, a.profile or profiles.names(), a.seconds, root, sources.TIERS[a.tier])
    finally:
        prov.down()
    print(calibrate.table(rows))
    print(f'results in {root}')
    return 0 if all(r['ok'] for r in rows) else 1


def main(argv):
    p = argparse.ArgumentParser(prog='moqbench contrib', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='verb', required=True)
    c = sub.add_parser('up')
    c.add_argument('deployment')
    c.add_argument('--dual-path', action='store_true')
    for verb in ('down', 'check', 'render-vm'):
        sub.add_parser(verb).add_argument('deployment')
    c = sub.add_parser('source')
    c.add_argument('deployment')
    c.add_argument('tier', choices=sorted(sources.TIERS))
    c.add_argument('seconds', type=int)
    c = sub.add_parser('calibrate')
    c.add_argument('deployment')
    c.add_argument('--profile', action='append')
    c.add_argument('--seconds', type=float, default=60)
    c.add_argument('--tier', default='mpeg2-20m', choices=sorted(sources.TIERS))
    c = sub.add_parser('run')
    c.add_argument('deployment')
    c.add_argument('suite')
    c.add_argument('--only', type=int, action='append', metavar='N')
    c.add_argument('--lane', action='append')
    c.add_argument('--profile', action='append')
    c.add_argument('--variant', action='append', metavar='KEY=VALUE',
                   help='only variants with this value, e.g. D=300 or D=4xRTT')
    c.add_argument('--repeats', type=int)
    c.add_argument('--seconds', type=int)
    c.add_argument('--scenario')
    c.add_argument('--keep', action='store_true', help='leave the namespaces up afterwards')
    sub.add_parser('report').add_argument('runs', nargs='+')
    sub.add_parser('explain').add_argument('runs', nargs='+')
    a = p.parse_args(argv)
    try:
        if a.verb == 'run':
            return cmd_run(a)
        if a.verb == 'calibrate':
            return cmd_calibrate(a)
        if a.verb == 'report':
            return report.main(a.runs)
        if a.verb == 'explain':
            return explain.main(a.runs)
        dep = spec.load_deployment(a.deployment)
        if a.verb == 'render-vm':
            if dep.provider == 'docker':
                raise config.ConfigError(f'{dep.name}: render-vm is for an aws or oci deployment')
            print(json.dumps(dict(nodes={ns: n['host'] for ns, n in dep.nodes.items()}, commands=render_vm(dep)), indent=1))
            return 0
        prov = provider(dep, getattr(a, 'dual_path', False))
        if a.verb == 'up':
            print(json.dumps(prov.up(), indent=1))
        elif a.verb == 'down':
            prov.down()
        elif a.verb == 'check':
            print(json.dumps(dict(topology=prov.check(), host=calibrate.settings(prov)), indent=1))
        elif a.verb == 'source':
            prov.ids()
            print(run.ensure_source(prov, sources.TIERS[a.tier], a.seconds))
        return 0
    except (config.ConfigError, FabricError, RuntimeError) as exc:
        print(f'moqbench contrib: {exc}', file=sys.stderr)
        return 2

