"""HTTP for the fabric helpers: http:// URLs, and http+unix://<quoted socket path>/path for a relay's
control socket."""
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def request(url, method='GET', data=None, timeout=10):
    """(status, body text); status 0 and the error text when the server cannot be reached."""
    parts = urllib.parse.urlsplit(url)
    try:
        if parts.scheme == 'http+unix':
            conn = UnixConnection(urllib.parse.unquote(parts.netloc), timeout)
            target = parts.path + (f'?{parts.query}' if parts.query else '')
            headers = {'Content-Type': 'application/json'} if data is not None else {}
            try:
                conn.request(method, target, body=data, headers=headers)
                r = conn.getresponse()
                return r.status, r.read().decode(errors='replace')
            finally:
                conn.close()
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')
    except (OSError, http.client.HTTPException) as e:
        return 0, str(e)
