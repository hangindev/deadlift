"""The server-setup tests' fake Tailscale API, through HTTP as `deadlift app` reaches it."""

import json
import urllib.error
import urllib.request

from tests.support.fake_tailscale_api import FAKE_API_KEY, FakeTailscaleApi, fake_tailscale_api

SERVICE = {
    "name": "svc:shop",
    "comment": "deadlift: the App shop",
    "ports": ["tcp:443", "tcp:10001"],
    "tags": ["tag:deadlift-svc"],
}


def call(
    api: FakeTailscaleApi, method: str, path: str, body: object = None, key: str = FAKE_API_KEY
) -> tuple[int, object]:
    request = urllib.request.Request(  # noqa: S310 (the fake's loopback address)
        f"{api.url}/api/v2/tailnet/-/{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_keeps_a_service_put_to_it_until_it_is_deleted() -> None:
    with fake_tailscale_api() as api:
        assert call(api, "GET", "vip-services/svc:shop") == (404, {"message": "not found"})
        assert call(api, "PUT", "vip-services/svc:shop", SERVICE) == (
            200,
            {**SERVICE, "addrs": ["100.100.0.1"]},
        )
        assert call(api, "GET", "vip-services/svc:shop") == (
            200,
            {**SERVICE, "addrs": ["100.100.0.1"]},
        )
        assert api.services["svc:shop"].ports == ["tcp:443", "tcp:10001"]
        assert api.services["svc:shop"].tags == ["tag:deadlift-svc"]

        assert call(api, "DELETE", "vip-services/svc:shop") == (200, {})
        assert "svc:shop" not in api.services
        assert call(api, "DELETE", "vip-services/svc:shop") == (404, {"message": "not found"})


def test_records_every_request_with_its_credential_and_body() -> None:
    with fake_tailscale_api() as api:
        call(api, "PUT", "vip-services/svc:shop", SERVICE)
        call(api, "GET", "vip-services/svc:shop", key="wrong")

        assert [(r.method, r.path, r.authorization, r.body) for r in api.requests] == [
            ("PUT", "/api/v2/tailnet/-/vip-services/svc:shop", f"Bearer {FAKE_API_KEY}", SERVICE),
            ("GET", "/api/v2/tailnet/-/vip-services/svc:shop", "Bearer wrong", None),
        ]


def test_refuses_a_wrong_credential_a_mismatched_name_and_other_paths() -> None:
    with fake_tailscale_api() as api:
        assert call(api, "GET", "vip-services/svc:shop", key="wrong") == (
            401,
            {"message": "API token invalid"},
        )
        assert call(api, "PUT", "vip-services/svc:other", SERVICE) == (
            400,
            {"message": "name mismatch"},
        )
        assert call(api, "GET", "devices") == (404, {"message": "not found"})
        assert call(api, "POST", "vip-services/svc:shop", SERVICE) == (
            405,
            {"message": "method not allowed"},
        )
        assert api.services == {}
