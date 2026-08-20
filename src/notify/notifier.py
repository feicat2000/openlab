"""
Notifier – multi-backend notification system for OpenLab events.

Backends: LogBackend (always active), SlackBackend, EmailBackend.
Configured via hardware_config.yaml ``notifications`` section.
"""
from __future__ import annotations

import smtplib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from loguru import logger


# ── Event model ───────────────────────────────────────────────────────────────

@dataclass
class NotificationEvent:
    event_type: str      # "run_complete"|"run_failed"|"estop"|"low_stock"|"test"
    title: str
    message: str
    run_id: Optional[str] = None
    severity: str = "info"   # "info"|"warning"|"critical"
    timestamp: float = field(default_factory=time.time)
    detail: Dict[str, Any] = field(default_factory=dict)


# ── Backend ABC ───────────────────────────────────────────────────────────────

class NotificationBackend(ABC):
    @abstractmethod
    def send(self, event: NotificationEvent) -> bool: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


# ── Concrete backends ─────────────────────────────────────────────────────────

class LogBackend(NotificationBackend):
    """Always available – logs every event via loguru."""

    @property
    def name(self) -> str:
        return "log"

    def send(self, event: NotificationEvent) -> bool:
        level_map = {"info": "INFO", "warning": "WARNING", "critical": "ERROR"}
        level = level_map.get(event.severity, "INFO")
        getattr(logger, level.lower())(
            f"[{event.event_type}] {event.title}: {event.message}"
        )
        return True


class SlackBackend(NotificationBackend):
    """POST a message to a Slack incoming webhook URL."""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    @property
    def name(self) -> str:
        return "slack"

    def send(self, event: NotificationEvent) -> bool:
        import requests  # type: ignore

        icon = {"info": "🔬", "warning": "⚠️", "critical": "🚨"}.get(event.severity, "ℹ️")
        payload = {
            "text": f"{icon} *{event.title}*",
            "attachments": [
                {
                    "color": {"info": "good", "warning": "warning", "critical": "danger"}.get(
                        event.severity, "good"
                    ),
                    "text": event.message,
                    "footer": f"OpenLab · {event.event_type}",
                    "ts": int(event.timestamp),
                }
            ],
        }
        try:
            resp = requests.post(self._url, json=payload, timeout=5)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Slack notification failed: {exc}")
            return False


class EmailBackend(NotificationBackend):
    """Send notifications via SMTP."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        recipients: List[str],
    ) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._user = username
        self._pass = password
        self._recipients = recipients

    @property
    def name(self) -> str:
        return "email"

    def send(self, event: NotificationEvent) -> bool:
        if not self._recipients:
            return False
        body = f"{event.title}\n\n{event.message}\n\nEvent: {event.event_type}\nSeverity: {event.severity}"
        if event.run_id:
            body += f"\nRun ID: {event.run_id}"

        msg = MIMEText(body)
        msg["Subject"] = f"[OpenLab] {event.title}"
        msg["From"] = self._user
        msg["To"] = ", ".join(self._recipients)

        try:
            with smtplib.SMTP(self._host, self._port) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(self._user, self._pass)
                smtp.sendmail(self._user, self._recipients, msg.as_string())
            return True
        except Exception as exc:
            logger.error(f"Email notification failed: {exc}")
            return False


# ── Notifier ─────────────────────────────────────────────────────────────────

class Notifier:
    """
    Broadcast notifications to all registered backends.

    Usage::

        n = Notifier()
        n.add_backend(LogBackend())
        n.on_run_complete(run_record)
    """

    def __init__(self) -> None:
        self._backends: List[NotificationBackend] = []

    def add_backend(self, backend: NotificationBackend) -> None:
        self._backends.append(backend)
        logger.info(f"Notification backend added: {backend.name}")

    def notify(
        self,
        event_type: str,
        title: str,
        message: str,
        run_id: Optional[str] = None,
        severity: str = "info",
        **detail: Any,
    ) -> List[bool]:
        event = NotificationEvent(
            event_type=event_type,
            title=title,
            message=message,
            run_id=run_id,
            severity=severity,
            detail=dict(detail),
        )
        results = []
        for backend in self._backends:
            try:
                ok = backend.send(event)
                results.append(ok)
            except Exception as exc:
                logger.error(f"Backend '{backend.name}' raised: {exc}")
                results.append(False)
        return results

    # ── Convenience wrappers ──────────────────────────────────────────────────

    def on_run_complete(self, run_record) -> None:
        duration = round(run_record.ended_at - run_record.started_at, 1)
        self.notify(
            event_type="run_complete",
            title=f"Run Complete: {run_record.protocol_name}",
            message=f"Run {run_record.run_id} finished successfully in {duration}s.",
            run_id=run_record.run_id,
            severity="info",
        )

    def on_run_failed(self, run_record, error: str) -> None:
        self.notify(
            event_type="run_failed",
            title=f"Run FAILED: {run_record.protocol_name}",
            message=f"Run {run_record.run_id} failed. Error: {error}",
            run_id=run_record.run_id,
            severity="critical",
        )

    def on_estop(self, source: str) -> None:
        self.notify(
            event_type="estop",
            title="EMERGENCY STOP TRIGGERED",
            message=f"E-stop activated by: {source}. All equipment halted.",
            severity="critical",
        )

    def on_low_stock(self, items: List[str]) -> None:
        self.notify(
            event_type="low_stock",
            title="Low Stock Alert",
            message=f"The following items are below reorder threshold: {', '.join(items)}",
            severity="warning",
        )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: dict) -> "Notifier":
        """
        Build a Notifier from the ``notifications`` section of hardware_config.yaml.

        Example config::

            notifications:
              slack_webhook: "https://hooks.slack.com/..."
              email:
                smtp_host: "smtp.gmail.com"
                smtp_port: 587
                username: "lab@example.com"
                password: "secret"
                recipients: ["pi@example.com"]
        """
        n = cls()
        n.add_backend(LogBackend())

        notif_cfg = config.get("notifications", {})

        webhook = notif_cfg.get("slack_webhook", "")
        if webhook:
            n.add_backend(SlackBackend(webhook_url=webhook))

        email_cfg = notif_cfg.get("email", {})
        host = email_cfg.get("smtp_host", "")
        if host:
            n.add_backend(
                EmailBackend(
                    smtp_host=host,
                    smtp_port=int(email_cfg.get("smtp_port", 587)),
                    username=email_cfg.get("username", ""),
                    password=email_cfg.get("password", ""),
                    recipients=email_cfg.get("recipients", []),
                )
            )

        return n
