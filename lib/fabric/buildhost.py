"""Print the build host for a fabric deployment: ssh target, key file (or -) and docker command.

A docker deployment builds on its host; a VM fabric builds on its first relay host.
"""
import json
import sys

import config


def main(name):
    dep = config.load_deployment(name)
    if dep.host:
        print(dep.host, '-', 'docker')
        return 0
    path = config.ROOT / '.testbed' / 'inventory.json'
    inv = json.loads(path.read_text()) if path.is_file() else {}
    if inv.get('deployment') != dep.name:
        print(f'the selected hosts are not {dep.name}: ./bin/testbed.sh {dep.name} use', file=sys.stderr)
        return 1
    relay = (inv.get('relays') or [inv['relay']])[0]
    print(f"{inv.get('user') or 'ubuntu'}@{relay['public']}", inv['ssh_key_file'], 'sudo_docker')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
