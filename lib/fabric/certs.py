"""A per-testbed CA and one leaf certificate per relay node, made with the openssl CLI.

Everything lives in an ignored local folder. Leaves are EC P-256, SHA-256, valid
for `days`, with the node's name, <name>.fabric.test and its addresses as SANs.
"""
import datetime
from pathlib import Path
import subprocess

DAYS = 30


def run(*args, cwd):
    subprocess.run(['openssl', *args], cwd=cwd, check=True, capture_output=True)


def valid_for(pem, seconds):
    """True when the certificate at pem is still valid `seconds` from now."""
    if not Path(pem).is_file():
        return False
    return subprocess.run(['openssl', 'x509', '-checkend', str(seconds), '-noout', '-in', str(pem)],
                          capture_output=True).returncode == 0


def ensure_ca(folder, days=DAYS):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if valid_for(folder / 'ca.pem', 2 * 86400) and (folder / 'ca.key').is_file():
        return folder / 'ca.pem'
    run('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-sha256',
        '-keyout', 'ca.key', '-out', 'ca.pem', '-days', str(days), '-subj', '/CN=moqbench fabric CA',
        '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign', cwd=folder)
    (folder / 'ca.key').chmod(0o600)
    return folder / 'ca.pem'


def sans(name, addresses):
    return ','.join([f'DNS:{name}', f'DNS:{name}.fabric.test', *(f'IP:{a}' for a in sorted(set(addresses)))])


def ensure_leaf(folder, name, addresses, days=DAYS):
    """<folder>/<name>.pem and .key, reissued when the CA changed, SANs changed or expiry is near."""
    folder = Path(folder)
    ensure_ca(folder, days)
    pem, key, ext = folder / f'{name}.pem', folder / f'{name}.key', folder / f'{name}.ext'
    want = f'subjectAltName={sans(name, addresses)}\nbasicConstraints=CA:FALSE\n' \
           'keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\n'
    fresh = (valid_for(pem, 2 * 86400) and ext.is_file() and ext.read_text() == want
             and pem.stat().st_mtime >= (folder / 'ca.pem').stat().st_mtime)
    if fresh:
        return pem, key
    ext.write_text(want)
    run('req', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-keyout', key.name,
        '-out', f'{name}.csr', '-subj', f'/CN={name}', cwd=folder)
    serial = str(int(datetime.datetime.now().timestamp() * 1e6))
    run('x509', '-req', '-in', f'{name}.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', serial,
        '-days', str(days), '-sha256', '-extfile', ext.name, '-out', pem.name, cwd=folder)
    key.chmod(0o600)
    (folder / f'{name}.csr').unlink()
    return pem, key
