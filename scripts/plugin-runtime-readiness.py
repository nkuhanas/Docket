"""Read-only private-boundary checks; never performs account linking or provider I/O."""

import json
import urllib.error
import urllib.request

from docket.config import get_settings

settings = get_settings()
if settings.plugin_enabled:
    root = "http://127.0.0.1:8000/plugin"
    discovery_url = root + "/.well-known/oauth-protected-resource"
    with urllib.request.urlopen(discovery_url, timeout=5) as response:
        metadata = json.load(response)
        assert metadata["resource"] == settings.plugin_public_origin.rstrip("/")
        assert metadata["authorization_servers"] == [settings.plugin_oauth_issuer]
    try:
        urllib.request.urlopen(urllib.request.Request(root + "/mcp/", data=b"{}"), timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("Native MCP accepted an anonymous request")
    for path in ("/internal/v1/agent/requests", "/triage-mcp/", "/openapi.json"):
        try:
            urllib.request.urlopen(root + path, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Native app exposed a private route")
print(json.dumps({"native_boundary": "passed" if settings.plugin_enabled else "disabled"}))
