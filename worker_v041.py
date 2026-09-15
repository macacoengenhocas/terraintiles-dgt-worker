#!/usr/bin/env python3
import urllib.parse
import worker_v04 as core


def validate_https_public(url, *, asset=False):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValueError('URL must use HTTPS')
    if parsed.username or parsed.password:
        raise ValueError('URL userinfo is not allowed')
    if asset:
        if parsed.hostname.lower() != core.ALLOWED_ASSET_HOST:
            raise ValueError('asset host not allowed')
        if not parsed.path.startswith(core.ALLOWED_ASSET_PREFIX):
            raise ValueError('asset path not allowed')
        if parsed.port not in (None, 443):
            raise ValueError('asset non-standard HTTPS port is not allowed')
    if not core.is_public_host(parsed.hostname):
        raise ValueError('target did not resolve to public IPs')
    return parsed


core.validate_https_public = validate_https_public
core.VERSION = '0.4.1'
main = core.main

if __name__ == '__main__':
    main()
