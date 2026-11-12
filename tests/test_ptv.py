import hashlib
import hmac
import logging
import traceback

import httpx
import pytest

from ptv import PTVAuth, PTVConfig, PTVRequestError, get_route_types


@pytest.fixture
def config():
    return PTVConfig("mock-developer", "mock-secret", "https://ptv.example")


def test_dotenv(tmp_path, monkeypatch):
    for name in ("PTV_DEV_ID", "PTV_API_KEY", "PTV_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text("PTV_DEV_ID=file-id\nPTV_API_KEY=file-key\nPTV_BASE_URL=https://ptv.example\n")
    monkeypatch.setenv("PTV_DEV_ID", "environment-id")
    config = PTVConfig.from_env(path)
    assert config.dev_id == "environment-id"
    assert config.api_key == "file-key"
    assert "file-key" not in repr(config)
    assert "environment-id" not in repr(config)


def test_missing_config(tmp_path, monkeypatch):
    for name in ("PTV_DEV_ID", "PTV_API_KEY", "PTV_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="PTV_API_KEY"):
        PTVConfig.from_env(tmp_path / "absent")


@pytest.mark.parametrize("query", ["", "?name=A%20B&limit=2"])
def test_signature(config, query):
    request = httpx.Request("GET", config.base_url + "/v3/route_types" + query)
    signed = next(PTVAuth(config.dev_id, config.api_key).auth_flow(request))
    raw, signature = signed.url.raw_path.rsplit(b"&signature=", 1)
    expected = hmac.new(b"mock-secret", raw, hashlib.sha1).hexdigest().upper()
    assert signature.decode() == expected
    assert signed.url.params["devid"] == config.dev_id
    assert config.api_key not in str(signed.url)
    if query:
        assert signed.url.params["name"] == "A B"
        assert signed.url.params["limit"] == "2"


def test_route_types(config, caplog):
    payload = {"route_types": [{"route_type": 0, "route_type_name": "Train"}]}
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v3/route_types"
        assert request.url.params["devid"] == config.dev_id
        assert "signature" in request.url.params
        return httpx.Response(200, json=payload)
    with caplog.at_level(logging.DEBUG):
        assert get_route_types(config, transport=httpx.MockTransport(handler)) == payload
    assert config.dev_id not in caplog.text
    assert config.api_key not in caplog.text
    assert "signature=" not in caplog.text


@pytest.mark.parametrize("failure", ["status", "network", "json"])
def test_safe_errors(config, failure):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "json":
            return httpx.Response(200, text="invalid JSON")
        return httpx.Response(403, json={"error": "Forbidden"})
    with pytest.raises(PTVRequestError) as caught:
        get_route_types(config, transport=httpx.MockTransport(handler))
    rendered = "".join(traceback.format_exception(caught.value))
    assert config.dev_id not in rendered
    assert config.api_key not in rendered
    assert "signature=" not in rendered
