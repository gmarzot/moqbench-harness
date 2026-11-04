"""Guard against accidentally tracking deployment IDs, credentials or local files."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygiene(unittest.TestCase):
    def test_no_concrete_cloud_identifiers_or_private_keys(self):
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        concrete_id = re.compile(r'ocid[0-9]+\.[a-z]+\.[a-z0-9]+\.[a-z0-9.]{24,}', re.I)
        private_key = re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE' + r' KEY-----')
        aws_access_key = re.compile(r'\bAKIA[0-9A-Z]{16}\b')
        findings = []
        for name in filter(None, names):
            p = ROOT / name
            if not p.is_file():
                continue
            data = p.read_text(errors='replace')
            if concrete_id.search(data) or private_key.search(data) or aws_access_key.search(data):
                findings.append(name)
        self.assertEqual(findings, [], 'Deployment identifiers or private key material in tracked files')

    def test_local_environment_paths_are_ignored_and_untracked(self):
        paths = ['.env', 'relays.local.sh', 'oci/envs/test.local.tfvars', 'aws/envs/test.local.tfvars',
                 '.testbed/private-notes/audit.txt', 'results/private-run/log.txt']
        for path in paths:
            with self.subTest(path=path):
                result = subprocess.run(['git', 'check-ignore', '-q', path], cwd=ROOT)
                self.assertEqual(result.returncode, 0)
                tracked = subprocess.check_output(['git', 'ls-files', '--', path], cwd=ROOT)
                self.assertEqual(tracked, b'')


if __name__ == '__main__':
    unittest.main()
