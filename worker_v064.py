#!/usr/bin/env python3
import socket

import worker_v06 as core

VERSION = '0.6.4'
core.VERSION = VERSION

HOSTS = ('cdd.dgterritorio.gov.pt', 'auth.cdd.dgterritorio.gov.pt')


def _probe_host(host):
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        print(f'CDD_DNS_PROBE host={host} error={type(exc).__name__}:{getattr(exc, "errno", None)}:{str(exc)[:160]}', flush=True)
        return
    seen = set()
    for family, socktype, proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        key = (family, sockaddr[0])
        if key in seen:
            continue
        seen.add(key)
        label = 'ipv4' if family == socket.AF_INET else 'ipv6'
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(4)
        try:
            sock.connect(sockaddr)
            print(f'CDD_TCP_PROBE host={host} family={label} ok=true', flush=True)
        except OSError as exc:
            print(f'CDD_TCP_PROBE host={host} family={label} ok=false errno={getattr(exc, "errno", None)} detail={str(exc)[:160]}', flush=True)
        finally:
            sock.close()


def main():
    for host in HOSTS:
        _probe_host(host)
    core.main()


if __name__ == '__main__':
    main()
