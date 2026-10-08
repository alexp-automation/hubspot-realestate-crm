"""Shared test helpers: repo on sys.path and a fake HTTP transport."""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (ROOT, os.path.join(ROOT, "lead_intake"), os.path.join(ROOT, "dynamics_sync")):
    if path not in sys.path:
        sys.path.insert(0, path)


class FakeTransport:
    """Records every request and answers from a list of (method, url regex, status, body) routes.

    Unmatched requests get 200 with {"id": "new-N"}, like a successful create.
    """

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.calls = []
        self._n = 0

    def add(self, method, pattern, body=None, status=200, headers=None, times=None):
        self.routes.append({"method": method, "pattern": re.compile(pattern), "body": body,
                            "status": status, "headers": headers or {}, "times": times})

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "url": url, "body": body, "headers": headers})
        for route in self.routes:
            if route["method"] == method and route["pattern"].search(url):
                if route["times"] is not None:
                    if route["times"] == 0:
                        continue
                    route["times"] -= 1
                payload = route["body"](url, body) if callable(route["body"]) else route["body"]
                return route["status"], route["headers"], json.dumps(payload if payload is not None else {}).encode()
        self._n += 1
        return 200, {}, json.dumps({"id": f"new-{self._n}", "objectTypeId": f"2-{9000 + self._n}"}).encode()

    def writes(self):
        return [c for c in self.calls if c["method"] != "GET" and "/search" not in c["url"]]


def quiet(*_args, **_kwargs):
    pass
