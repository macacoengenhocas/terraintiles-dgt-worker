#!/usr/bin/env python3
import time
import urllib.error

import worker_v06 as core

VERSION = '0.6.5'
core.VERSION = VERSION
core.core.VERSION = VERSION

_original_request = core.core.request


def _request_with_timeout(opener, url, *, method='GET', data=None, extra_headers=None, timeout=10):
    return _original_request(
        opener,
        url,
        method=method,
        data=data,
        extra_headers=extra_headers,
        timeout=min(timeout, 10),
    )


core.core.request = _request_with_timeout


def _search_assets_fast(bbox, collection):
    last_error = None
    for attempt in range(2):
        try:
            return core._search_once(core._active_opener(force=attempt > 0), bbox, collection)
        except urllib.error.HTTPError as exc:
            last_error = RuntimeError(f'DGT_STAC_HTTP_{exc.code}')
            if attempt == 0 and (exc.code in (401, 403) or exc.code >= 500):
                core._reset_session()
                time.sleep(0.5)
                continue
            raise last_error
        except urllib.error.URLError as exc:
            core._safe_network_log('search-fast-fail', exc)
            raise RuntimeError(f'DGT_NETWORK_{type(exc.reason).__name__}') from exc
        except RuntimeError as exc:
            last_error = exc
            if attempt == 0 and str(exc).startswith(('DGT_SESSION_', 'DGT_LOGIN_')):
                core._reset_session()
                time.sleep(0.5)
                continue
            raise
    raise last_error or RuntimeError('DGT_SEARCH_FAILED')


def _resolve_asset_fast(href):
    last_error = None
    for attempt in range(2):
        try:
            return core._resolve_once(core._active_opener(force=attempt > 0), href)
        except urllib.error.HTTPError as exc:
            last_error = RuntimeError(f'DGT_GATE_HTTP_{exc.code}')
            if attempt == 0 and (exc.code in (401, 403) or exc.code >= 500):
                core._reset_session()
                time.sleep(0.5)
                continue
            raise last_error
        except urllib.error.URLError as exc:
            core._safe_network_log('resolve-fast-fail', exc)
            raise RuntimeError(f'DGT_NETWORK_{type(exc.reason).__name__}') from exc
        except RuntimeError as exc:
            last_error = exc
            if attempt == 0 and str(exc).startswith(('DGT_SESSION_', 'DGT_LOGIN_')):
                core._reset_session()
                time.sleep(0.5)
                continue
            raise
    raise last_error or RuntimeError('DGT_RESOLVE_FAILED')


core.search_assets = _search_assets_fast
core.resolve_asset = _resolve_asset_fast


def main():
    core.main()


if __name__ == '__main__':
    main()
