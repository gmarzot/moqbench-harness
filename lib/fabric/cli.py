"""moqbench fabric: bring a relay fabric up, change it, impair it, and run fabric suites.

  moqbench fabric up <deployment> [--topology SHAPE]   render, start, wait for every link
  moqbench fabric down <deployment>
  moqbench fabric render <deployment> [--topology SHAPE]   write the stack locally only
  moqbench fabric deploy <deployment> [--from TARGET]  copy the relay image to every relay VM
  moqbench fabric state <deployment> [node ...]        each relay's peer sessions
  moqbench fabric peer <deployment> add|remove|restart|metric <on> <peer> [metric]
  moqbench fabric link <deployment> impair|down|clear <on> <peer> [netem spec]
                       [--direction both|on|peer] [--side send|receive]
  moqbench fabric node <deployment> kill|stop|start|freeze|thaw <node>
  moqbench fabric check <deployment> [--clear]         impairments left on any relay
  moqbench fabric run <deployment> <suite> [--only N] [--keep]
  moqbench fabric report <run folder> ...

A docker deployment runs every relay on one Docker host; an aws or oci
deployment with a [fabric] table runs one relay per VM of the selected testbed.
Nodes may be named directly or by role: ingest, egress, transit.
"""
import argparse
import datetime
import json
from pathlib import Path
import sys

import config
from fabric import faults, report, topology
from fabric.docker import FabricError
from fabric.run import Run, load_scenario, provider

ROOT = Path(__file__).resolve().parents[2]


def fabric_for(name, shape=None):
    dep = config.load_deployment(name)
    if dep.fabric is None or dep.provider == 'static':
        raise config.ConfigError(f'{name}: moqbench fabric needs a docker, aws or oci deployment with a [fabric] table')
    links = None
    if shape:
        links = topology.links(shape, dep.fabric.order, dep.fabric.hub, dep.fabric.custom)
    return dep, provider(dep, links)


def show(obj):
    print(json.dumps(obj, indent=1, default=str))


def cmd_run(a):
    dep = config.load_deployment(a.deployment)
    suite = config.load_suite(a.suite)
    tests = suite.fabric_tests
    if not tests:
        raise config.ConfigError(f'suite {a.suite} has no fabric tests')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    root = ROOT / 'results' / 'fabric' / f'{dep.name}-{stamp}'
    status = 0
    for i, test in enumerate(tests, 1):
        if a.only and i not in a.only:
            continue
        load_scenario(test['scenario'])
        label = f'{i:02d}-{test["scenario"]}-{test.get("topology", dep.fabric.topology)}'
        if test.get('suffix'):
            label += f'-{test["suffix"]}'
        print(f'-- {label}: {test.get("note", "")}', flush=True)
        out = Run(dep, test, label, root).execute(keep=a.keep)
        summary = report.analyze(out)
        (out / 'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
        print(report.text(summary), flush=True)
        if any(v['status'] in ('FAIL', 'XPASS') for v in summary['verdicts']):
            status = 1
    print(f'results in {root}')
    return status


def main(argv):
    p = argparse.ArgumentParser(prog='moqbench fabric', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='verb', required=True)
    for verb in ('up', 'render'):
        c = sub.add_parser(verb)
        c.add_argument('deployment')
        c.add_argument('--topology', choices=sorted(topology.SHAPES))
    sub.add_parser('down').add_argument('deployment')
    c = sub.add_parser('deploy')
    c.add_argument('deployment')
    c.add_argument('--from', dest='source', metavar='TARGET', help='ssh target of a Docker host with the image')
    c = sub.add_parser('state')
    c.add_argument('deployment')
    c.add_argument('nodes', nargs='*')
    c = sub.add_parser('peer')
    c.add_argument('deployment')
    c.add_argument('action', choices=['add', 'remove', 'restart', 'metric'])
    c.add_argument('on')
    c.add_argument('peer')
    c.add_argument('metric', nargs='?', type=int)
    c = sub.add_parser('link')
    c.add_argument('deployment')
    c.add_argument('action', choices=['impair', 'down', 'clear'])
    c.add_argument('on')
    c.add_argument('peer')
    c.add_argument('netem', nargs='*')
    c.add_argument('--direction', default='both', choices=['both', 'on', 'peer'])
    c.add_argument('--side', default='send', choices=['send', 'receive'])
    c = sub.add_parser('node')
    c.add_argument('deployment')
    c.add_argument('action', choices=['kill', 'stop', 'start', 'freeze', 'thaw'])
    c.add_argument('node')
    c.add_argument('--signal', default='KILL')
    c = sub.add_parser('check')
    c.add_argument('deployment')
    c.add_argument('--clear', action='store_true')
    c = sub.add_parser('run')
    c.add_argument('deployment')
    c.add_argument('suite')
    c.add_argument('--only', type=int, action='append', metavar='N', help='run only test N (1-based, repeatable)')
    c.add_argument('--keep', action='store_true', help='leave the fabric up after the last test')
    sub.add_parser('report').add_argument('runs', nargs='+')
    a = p.parse_args(argv)
    try:
        if a.verb == 'run':
            return cmd_run(a)
        if a.verb == 'report':
            return report.main(a.runs)
        dep, fab = fabric_for(a.deployment, getattr(a, 'topology', None))
        if a.verb == 'render':
            print(fab.render())
        elif a.verb == 'up':
            took = fab.up()
            print(f'{dep.name}: {len(fab.order)} relays up, {len(fab.links)} links connected in {took:.1f} s')
        elif a.verb == 'down':
            fab.down()
        elif a.verb == 'deploy':
            if fab.kind != 'vm':
                raise config.ConfigError(f'{dep.name}: deploy copies images to relay VMs; a docker fabric builds on its host')
            copied = fab.deploy(a.source)
            print(f'{dep.fabric.image}: copied to {", ".join(copied) or "none (every relay has it)"}')
        elif a.verb == 'state':
            show({n: fab.adapter.peer_state(fab, n) for n in (a.nodes or fab.order)})
        elif a.verb == 'peer':
            ev = dict(verb=f'peer.{a.action}', on=a.on, peer=a.peer)
            if a.metric is not None:
                ev['metric'] = a.metric
            show(faults.apply(fab, ev))
        elif a.verb == 'link':
            ev = dict(verb=f'link.{a.action}', on=a.on, peer=a.peer, direction=a.direction, side=a.side)
            if a.action == 'impair':
                ev['netem'] = ' '.join(a.netem)
            show(faults.apply(fab, ev))
        elif a.verb == 'node':
            show(faults.apply(fab, dict(verb=f'node.{a.action}', on=a.node, signal=a.signal)))
        elif a.verb == 'check':
            left = faults.clear_all(fab) if a.clear else faults.check(fab)
            show(left)
            return 1 if left else 0
        return 0
    except (config.ConfigError, FabricError, faults.FaultError, topology.TopologyError) as exc:
        print(f'moqbench fabric: {exc}', file=sys.stderr)
        return 2
