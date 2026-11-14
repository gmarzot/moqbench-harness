import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

V1_KEYS = ['deployment', 'environment', 'provider', 'user', 'ssh_key_file', 'relay', 'clients', 'probes']


def out(**values):
    return {k: {'sensitive': False, 'type': 'x', 'value': v} for k, v in values.items()}


OCI = out(relay_public_ip='203.0.113.10', relay_private_ip='10.42.1.10',
          client_public_ips=['203.0.113.11', '203.0.113.12'], client_private_ips=['10.42.1.11', '10.42.1.12'],
          probe_public_ips=[], probe_private_ips=[], ssh_key_file='/home/t/.ssh/id_ed25519',
          instance_ids=['ocid-relay', 'ocid-c1', 'ocid-c2'],
          inventory={'relay': '10.42.1.10', 'clients': [], 'probes': [], 'ssh': 'ssh ubuntu@203.0.113.10'})

AWS = out(relay_public_ip='198.51.100.10', relay_private_ip='10.42.1.20',
          client_public_ips=['198.51.100.11'], client_private_ips=['10.42.1.21'],
          probe_public_ips=['198.51.100.12'], probe_private_ips=['10.42.1.22'],
          ssh_key_file='/home/t/.ssh/id_ed25519', instance_ids=['i-relay', 'i-client', 'i-probe'],
          arch='arm64', region='us-west-2')


def run(outputs, *args):
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / 'sub' / 'inventory.json'
        result = subprocess.run([sys.executable, str(ROOT / 'lib/inventory.py'), '--out', str(dest),
                                 '--deployment', 'fanout-1', '--environment', 'env1', *args],
                                input=json.dumps(outputs), capture_output=True, text=True)
        return result.returncode, (json.loads(dest.read_text()) if dest.exists() else None)


def hosts_sh(inventory):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / '.testbed').mkdir()
        (Path(d) / '.testbed/inventory.json').write_text(json.dumps(inventory))
        script = f'source {ROOT}/lib/hosts.sh; echo "$TESTBED|$RELAY|$RPRIV|${{CLIENTS[*]}}|${{PROBES[*]}}|$KEY|$RUSER"'
        return subprocess.check_output(['bash', '-c', script], cwd=d, text=True,
                                       env={'PATH': '/usr/bin:/bin', 'HOME': d}).strip()


class Inventory(unittest.TestCase):
    def test_oci_keeps_v1_keys(self):
        code, inv = run(OCI, '--provider', 'oci', '--region', 'us-ashburn-1')
        self.assertEqual(code, 0)
        self.assertEqual(list(inv)[:len(V1_KEYS)], V1_KEYS)
        self.assertEqual((inv['deployment'], inv['environment'], inv['provider'], inv['user']),
                         ('fanout-1', 'env1', 'oci', 'ubuntu'))
        self.assertEqual(inv['ssh_key_file'], '/home/t/.ssh/id_ed25519')
        self.assertEqual({k: inv['relay'][k] for k in ('public', 'private')},
                         {'public': '203.0.113.10', 'private': '10.42.1.10'})
        self.assertEqual([(h['public'], h['private']) for h in inv['clients']],
                         [('203.0.113.11', '10.42.1.11'), ('203.0.113.12', '10.42.1.12')])
        self.assertEqual(inv['probes'], [])
        self.assertEqual((inv['schema'], inv['region'], inv['arch']), (3, 'us-ashburn-1', None))
        self.assertEqual([(h['name'], h['id']) for h in [inv['relay']] + inv['clients']],
                         [('relay', 'ocid-relay'), ('client-01', 'ocid-c1'), ('client-02', 'ocid-c2')])

    def test_aws_adds_region_and_arch_from_outputs(self):
        code, inv = run(AWS, '--provider', 'aws', '--region', 'ignored-1')
        self.assertEqual(code, 0)
        self.assertEqual(list(inv)[:len(V1_KEYS)], V1_KEYS)
        self.assertEqual((inv['provider'], inv['region'], inv['arch']), ('aws', 'us-west-2', 'arm64'))
        self.assertEqual(inv['probes'], [{'public': '198.51.100.12', 'private': '10.42.1.22',
                                          'name': 'probe-01', 'id': 'i-probe'}])
        self.assertEqual(inv['clients'][0]['id'], 'i-client')

    def test_several_relays(self):
        outputs = dict(OCI, **out(relay_public_ips=['203.0.113.10', '203.0.113.20'],
                                  relay_private_ips=['10.42.1.10', '10.42.1.20'],
                                  instance_ids=['ocid-r1', 'ocid-r2', 'ocid-c1', 'ocid-c2']))
        code, inv = run(outputs, '--provider', 'oci')
        self.assertEqual(code, 0)
        self.assertEqual([(h['name'], h['private'], h['id']) for h in inv['relays']],
                         [('relay-01', '10.42.1.10', 'ocid-r1'), ('relay-02', '10.42.1.20', 'ocid-r2')])
        self.assertEqual((inv['relay']['name'], inv['relay']['id']), ('relay', 'ocid-r1'))
        self.assertEqual([h['id'] for h in inv['clients']], ['ocid-c1', 'ocid-c2'])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.testbed').mkdir()
            (Path(d) / '.testbed/inventory.json').write_text(json.dumps(inv))
            script = f'source {ROOT}/lib/hosts.sh; echo "${{RELAYS[*]}}|${{RELAYS_PRIV[*]}}|${{RELAY_NAMES[*]}}|$RELAY"'
            got = subprocess.check_output(['bash', '-c', script], cwd=d, text=True,
                                          env={'PATH': '/usr/bin:/bin', 'HOME': d}).strip()
        self.assertEqual(got, '203.0.113.10 203.0.113.20|10.42.1.10 10.42.1.20|relay-01 relay-02|203.0.113.10')

    def test_no_relay_writes_nothing(self):
        self.assertEqual(run({}, '--provider', 'aws'), (1, None))

    def test_hosts_sh_reads_it_unchanged(self):
        _, inv = run(AWS, '--provider', 'aws')
        self.assertEqual(hosts_sh(inv),
                         'fanout-1|198.51.100.10|10.42.1.20|198.51.100.11|198.51.100.12|/home/t/.ssh/id_ed25519|ubuntu')


if __name__ == '__main__':
    unittest.main()
