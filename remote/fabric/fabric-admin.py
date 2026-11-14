#!/usr/bin/env python3
"""One HTTP call to a relay's control API: METHOD URL [JSON body]. Prints t0, t1, status and body as JSON."""
import json
import sys
import time
import urllib.error
import urllib.request


def main(method, url, body=None):
    data = body.encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, text = r.status, r.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode(errors='replace')
    except OSError as e:
        status, text = 0, str(e)
    print(json.dumps({'t0': t0, 't1': time.time(), 'status': status, 'body': text}))


if __name__ == '__main__':
    main(*sys.argv[1:4])
