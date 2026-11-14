#!/usr/bin/env python3
"""Write .testbed/inventory.json from `terraform output -json`.

    terraform output -json | lib/inventory.py --deployment D --environment E --provider P [--region R]
    lib/inventory.py ... outputs.json

The host list every command in bin/ reads (lib/hosts.sh). The v1 keys are
unchanged; schema 2 adds region, arch and per-host id and name; schema 3 adds
relays, every relay host in order, of which relay is the first. Exits 1
without writing anything when the outputs hold no relay yet.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def build(outputs, deployment, environment, provider, user='ubuntu', region=None, arch=None):
    def v(key, default=None):
        value = outputs.get(key, {}).get('value')
        return default if value is None else value

    ids = list(v('instance_ids', []))
    counter = iter(ids)

    def host(public, private, name, id_=None):
        return {'public': public, 'private': private, 'name': name, 'id': id_}

    def pairs(role, pub, priv):
        return [host(a, b, f'{role}-{i:02d}', next(counter, None))
                for i, (a, b) in enumerate(zip(v(pub, []), v(priv, [])), 1)]

    if 'relay_public_ips' in outputs:
        relays = pairs('relay', 'relay_public_ips', 'relay_private_ips')
    else:
        relays = [host(v('relay_public_ip'), v('relay_private_ip'), 'relay-01', next(counter, None))]
    first = dict(relays[0], name='relay')
    return {
        'deployment': deployment, 'environment': environment, 'provider': provider, 'user': user,
        'ssh_key_file': outputs['ssh_key_file']['value'],
        'relay': first,
        'clients': pairs('client', 'client_public_ips', 'client_private_ips'),
        'probes': pairs('probe', 'probe_public_ips', 'probe_private_ips'),
        'relays': relays,
        'schema': 3,
        'region': v('region', region),
        'arch': v('arch', arch),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('outputs', nargs='?', default='-', help='terraform output -json file (default stdin)')
    p.add_argument('--deployment', required=True)
    p.add_argument('--environment', required=True)
    p.add_argument('--provider', required=True)
    p.add_argument('--user', default='ubuntu')
    p.add_argument('--region', help='when the outputs carry none')
    p.add_argument('--arch', help='when the outputs carry none')
    p.add_argument('--out', default=str(ROOT / '.testbed' / 'inventory.json'))
    a = p.parse_args(argv)
    outputs = json.load(sys.stdin if a.outputs == '-' else open(a.outputs))
    if 'relay_public_ip' not in outputs:
        return 1
    inv = build(outputs, a.deployment, a.environment, a.provider, a.user, a.region or None, a.arch or None)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + '.new')
    tmp.write_text(json.dumps(inv, indent=2) + '\n')
    os.replace(tmp, out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
