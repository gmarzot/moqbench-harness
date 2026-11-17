#!/usr/bin/env python3
"""Copy a UDP stream (unicast or multicast) to stdout, datagram payloads unchanged, for a sender that
reads TS from a pipe.

  udp2pipe.py GROUP:PORT
"""
import os
import socket
import struct
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    host, _, port = argv[0].rpartition(':')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.bind((host, int(port)))
    if 224 <= int(host.split('.')[0]) <= 239:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack('4s4s', socket.inet_aton(host), socket.inet_aton('0.0.0.0')))
    out = sys.stdout.buffer.fileno()
    try:
        while True:
            data = s.recv(65536)
            while data:
                data = data[os.write(out, data):]
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == '__main__':
    sys.exit(main())
