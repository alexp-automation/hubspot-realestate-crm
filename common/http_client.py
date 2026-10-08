"""Tiny JSON API client on top of urllib, shared by the HubSpot and Dataverse scripts.

- Retries 429 and 5xx with backoff (uses Retry-After when the API sends it).
- Dry-run mode: write calls are logged and never sent. Reads are only sent when
  `live_reads=True` (e.g. to plan against a real portal without changing it).
- The transport is injectable, so tests run fully offline.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HUBSPOT_BASE = "https://api.hubapi.com"


class ApiError(Exception):
    def __init__(self, status, method, url, body):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def urllib_transport(method, url, headers, data, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read()


class ApiClient:
    def __init__(self, base_url, headers=None, dry_run=False, live_reads=False,
                 transport=None, sleep=time.sleep, max_retries=5, log=print, name="api"):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.dry_run = dry_run
        self.live_reads = live_reads
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.max_retries = max_retries
        self.log = log
        self.name = name
        self.planned = []  # calls skipped in dry-run, kept for tests and summaries
        self._fake_id = 0

    def url(self, path, params=None):
        url = path if path.startswith("http") else self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def request(self, method, path, body=None, params=None, extra_headers=None):
        url = self.url(path, params)
        send = not self.dry_run or (self.live_reads and method == "GET")
        if not send:
            self._fake_id += 1
            self.planned.append({"method": method, "url": url, "body": body})
            shown = "" if body is None else " " + json.dumps(body, ensure_ascii=False)
            self.log(f"  [dry-run {self.name}] {method} {url}{shown}")
            return {"id": f"dry-run-{self._fake_id}", "results": []}

        headers = {"Accept": "application/json", **self.headers, **(extra_headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")

        for attempt in range(self.max_retries + 1):
            status, resp_headers, raw = self.transport(method, url, headers, data)
            retryable = status == 429 or status >= 500
            if retryable and attempt < self.max_retries:
                self.sleep(self._delay(resp_headers, attempt))
                continue
            text = raw.decode("utf-8", "replace") if raw else ""
            if status >= 400:
                raise ApiError(status, method, url, text)
            return json.loads(text) if text.strip() else {}
        raise AssertionError("unreachable")

    @staticmethod
    def _delay(headers, attempt):
        retry_after = {k.lower(): v for k, v in (headers or {}).items()}.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(2 ** attempt, 30)

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body=None):
        return self.request("POST", path, body=body)

    def patch(self, path, body=None):
        return self.request("PATCH", path, body=body)

    def put(self, path, body=None):
        return self.request("PUT", path, body=body)


def hubspot_client(dry_run=False, live_reads=None, transport=None, log=print, token=None, **kwargs):
    """HubSpot client using a private app token from HUBSPOT_TOKEN.

    In dry-run without a token nothing is sent at all. In dry-run with a token,
    GET requests are sent so the plan reflects the real portal.
    """
    token = token if token is not None else os.environ.get("HUBSPOT_TOKEN", "")
    if not token and not dry_run:
        raise SystemExit("HUBSPOT_TOKEN is not set. Export it, or run with --dry-run.")
    if live_reads is None:
        live_reads = bool(token)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return ApiClient(HUBSPOT_BASE, headers=headers, dry_run=dry_run, live_reads=live_reads,
                     transport=transport, log=log, name="hubspot", **kwargs)
