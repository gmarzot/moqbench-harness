#!/usr/bin/env python3
"""One HTTP call to a relay's control API: METHOD URL [JSON body]. Prints t0, t1, status and body as JSON.

URL is http://... or http+unix://<quoted socket path>/path.
"""
import json
import sys
import time

from unixhttp import request


def main(method, url, body=None):
    t0 = time.time()
    status, text = request(url, method, body.encode() if body is not None else None)
    print(json.dumps({'t0': t0, 't1': time.time(), 'status': status, 'body': text}))


if __name__ == '__main__':
    main(*sys.argv[1:4])
