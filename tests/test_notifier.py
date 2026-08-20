"""
Tests for Notifier and notification backends.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch, call

import pytest

from src.notify.notifier import (
    Notifier,
    NotificationEvent,
    LogBackend,
    SlackBackend,
    EmailBackend,
)


# ── Stubs ─────────────────────────────────────────────────────────────────────

@dataclass
class _RunRecord:
    run_id: str = "run_001"
    protocol_name: str = "DNA Extraction"
    started_at: float = 0.0
    ended_at: float = 60.0
    status: str = "completed"


def _make_event(**kwargs) -> NotificationEvent:
    defaults = dict(
        event_type="test",
        title="Test Event",
        message="Hello",
        severity="info",
    )
    defaults.update(kwargs)
    return NotificationEvent(**defaults)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestLogBackend:
    def test_always_returns_true(self):
        b = LogBackend()
        e = _make_event()
        assert b.send(e) is True

    def test_name_is_log(self):
        assert LogBackend().name == "log"

    def test_critical_logged_as_error(self, caplog):
        b = LogBackend()
        e = _make_event(severity="critical")
        b.send(e)  # should not raise


class TestSlackBackend:
    def test_send_calls_requests_post(self):
        b = SlackBackend("https://hooks.slack.com/test")
        e = _make_event()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = b.send(e)
        mock_post.assert_called_once()
        assert result is True

    def test_send_correct_payload(self):
        b = SlackBackend("https://hooks.slack.com/test")
        e = _make_event(title="MyTitle", message="MyMessage")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        with patch("requests.post", return_value=mock_resp) as mock_post:
            b.send(e)
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert "MyTitle" in payload["text"]

    def test_send_returns_false_on_error(self):
        b = SlackBackend("https://hooks.slack.com/test")
        e = _make_event()
        with patch("requests.post", side_effect=Exception("network error")):
            result = b.send(e)
        assert result is False

    def test_name_is_slack(self):
        assert SlackBackend("https://example.com").name == "slack"


class TestEmailBackend:
    def _make_backend(self):
        return EmailBackend(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="lab@example.com",
            password="secret",
            recipients=["pi@example.com"],
        )

    def test_send_uses_smtp(self):
        b = self._make_backend()
        e = _make_event(title="Run Done", message="Completed.")
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)
        with patch("smtplib.SMTP", return_value=mock_smtp):
            result = b.send(e)
        mock_smtp.login.assert_called_once()
        mock_smtp.sendmail.assert_called_once()
        assert result is True

    def test_send_returns_false_on_smtp_error(self):
        b = self._make_backend()
        e = _make_event()
        with patch("smtplib.SMTP", side_effect=Exception("connection refused")):
            result = b.send(e)
        assert result is False

    def test_send_false_if_no_recipients(self):
        b = EmailBackend("smtp.example.com", 587, "u", "p", recipients=[])
        assert b.send(_make_event()) is False

    def test_name_is_email(self):
        assert self._make_backend().name == "email"


class TestNotifier:
    def test_notify_broadcasts_to_all_backends(self):
        n = Notifier()
        b1, b2 = MagicMock(), MagicMock()
        b1.send.return_value = True
        b2.send.return_value = True
        n.add_backend(b1)
        n.add_backend(b2)
        results = n.notify("test", "T", "M")
        b1.send.assert_called_once()
        b2.send.assert_called_once()
        assert results == [True, True]

    def test_failed_backend_does_not_block_others(self):
        n = Notifier()
        bad = MagicMock()
        bad.send.side_effect = RuntimeError("boom")
        bad.name = "bad"
        good = MagicMock()
        good.send.return_value = True
        good.name = "good"
        n.add_backend(bad)
        n.add_backend(good)
        results = n.notify("test", "T", "M")
        good.send.assert_called_once()
        assert results == [False, True]

    def test_on_run_complete(self):
        n = Notifier()
        mock_backend = MagicMock()
        mock_backend.send.return_value = True
        mock_backend.name = "mock"
        n.add_backend(mock_backend)
        rec = _RunRecord()
        n.on_run_complete(rec)
        mock_backend.send.assert_called_once()
        event = mock_backend.send.call_args[0][0]
        assert event.event_type == "run_complete"
        assert "run_001" in event.run_id

    def test_on_run_failed(self):
        n = Notifier()
        mock_backend = MagicMock()
        mock_backend.send.return_value = True
        mock_backend.name = "mock"
        n.add_backend(mock_backend)
        n.on_run_failed(_RunRecord(), "Something broke")
        event = mock_backend.send.call_args[0][0]
        assert event.event_type == "run_failed"
        assert event.severity == "critical"
        assert "Something broke" in event.message

    def test_on_estop(self):
        n = Notifier()
        mock_backend = MagicMock()
        mock_backend.send.return_value = True
        mock_backend.name = "mock"
        n.add_backend(mock_backend)
        n.on_estop("safety_monitor")
        event = mock_backend.send.call_args[0][0]
        assert event.event_type == "estop"
        assert event.severity == "critical"

    def test_on_low_stock(self):
        n = Notifier()
        mock_backend = MagicMock()
        mock_backend.send.return_value = True
        mock_backend.name = "mock"
        n.add_backend(mock_backend)
        n.on_low_stock(["Ethanol", "Water"])
        event = mock_backend.send.call_args[0][0]
        assert event.event_type == "low_stock"
        assert "Ethanol" in event.message


class TestFromConfig:
    def test_from_config_empty_adds_log_backend(self):
        n = Notifier.from_config({})
        assert any(b.name == "log" for b in n._backends)

    def test_from_config_slack_added_when_webhook_set(self):
        cfg = {"notifications": {"slack_webhook": "https://hooks.slack.com/test"}}
        n = Notifier.from_config(cfg)
        assert any(b.name == "slack" for b in n._backends)

    def test_from_config_no_slack_when_webhook_empty(self):
        cfg = {"notifications": {"slack_webhook": ""}}
        n = Notifier.from_config(cfg)
        assert not any(b.name == "slack" for b in n._backends)

    def test_from_config_email_added_when_host_set(self):
        cfg = {
            "notifications": {
                "email": {
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 587,
                    "username": "u",
                    "password": "p",
                    "recipients": ["a@b.com"],
                }
            }
        }
        n = Notifier.from_config(cfg)
        assert any(b.name == "email" for b in n._backends)
