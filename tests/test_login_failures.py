"""Login failures stay bounded, redacted, and before all sync writes."""

import traceback
from argparse import Namespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import requests
from PyTado.exceptions import TadoException

import sync_octopus_tado as sync


@pytest.mark.parametrize("provided,expected", [(None, (10, 30)), (8, 8)])
def test_login_requests_have_timeout_without_automatic_replay(provided, expected):
    with patch.object(
        requests.Session, "request", return_value=Mock(status_code=200)
    ) as request:
        session = sync.TadoLoginSession()
        session.request("POST", "https://login.tado.com/oauth2/token", timeout=provided)
        request.assert_called_once_with(
            "POST", "https://login.tado.com/oauth2/token", timeout=expected
        )


def test_reset_does_not_retry_token_exchange_or_retain_previous_status():
    with patch.object(
        requests.Session,
        "request",
        side_effect=requests.ConnectionError("private-device-code"),
    ) as request:
        session = sync.TadoLoginSession()
        session.last_http_status = 400
        with pytest.raises(requests.ConnectionError):
            session.request("POST", "https://login.tado.com/oauth2/token")
        request.assert_called_once()
        assert session.last_http_status is None


@pytest.mark.parametrize(
    "error,kind",
    [
        (requests.ConnectionError("private-device-code"), "connection failure"),
        (requests.Timeout("private-device-code"), "timeout"),
    ],
)
def test_wrapped_activation_transport_failure_is_redacted(
    monkeypatch, tmp_path, error, kind
):
    client = Mock()
    client.device_activation_status.return_value = "PENDING"
    client.device_activation.side_effect = TadoException(error)
    monkeypatch.setattr(sync, "Tado", Mock(return_value=client))
    monkeypatch.setattr(sync, "browser_login", AsyncMock())
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    username, password = "private-user", "private-password"
    with pytest.raises(
        RuntimeError, match=f"device token activation: .*{kind}"
    ) as caught:
        sync.tado_login(username, password)
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "private-device-code" not in rendered
    assert "private-password" not in rendered
    assert "Failed before sync" in summary.read_text()
    client.device_activation.assert_called_once()


def test_confirmed_rate_limit_is_not_reported_as_wrong_password(monkeypatch):
    session = Mock(last_http_status=429)
    monkeypatch.setattr(sync, "TadoLoginSession", Mock(return_value=session))
    monkeypatch.setattr(
        sync, "Tado", Mock(side_effect=TadoException("private-response"))
    )
    with pytest.raises(
        RuntimeError, match="client initialization: HTTP 429 rate limit"
    ):
        sync.tado_login("test", "test")
    session.close.assert_called_once()


def test_incomplete_activation_never_returns_client(monkeypatch):
    client = Mock()
    client.device_activation_status.return_value = "NOT_STARTED"
    monkeypatch.setattr(sync, "Tado", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="authorization did not complete"):
        sync.tado_login("test", "test")


def test_login_failure_stops_main_before_meter_and_tariff_calls(monkeypatch):
    monkeypatch.setattr(
        sync,
        "parse_args",
        Mock(return_value=Namespace(tado_email="test", tado_password="test")),
    )
    monkeypatch.setattr(
        sync, "Tado", Mock(side_effect=requests.ConnectionError("private-code"))
    )
    meter, tariff = Mock(), Mock()
    monkeypatch.setattr(sync, "get_meter_reading_total_consumption", meter)
    monkeypatch.setattr(sync, "sync_octopus_tariffs_to_tado", tariff)
    with pytest.raises(RuntimeError, match="client initialization: connection failure"):
        sync.main()
    meter.assert_not_called()
    tariff.assert_not_called()
