"""Optional timed and cross-device delivery for :mod:`proactive_service`.

The classes in this module are adapters, not lifecycle authorities.  They do
not start threads on import or construction, and a notification acceptance is
never interpreted as completion of the activity it describes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib import error, parse, request


class DeliverySink(Protocol):
    """Conversation/event delivery boundary used by the composed lifecycle.

    The retained record and identity envelope are passed without flattening.
    ``state`` must be accepted, unavailable, or failed; accepted means only
    transport acceptance and never user acknowledgement or task completion.
    """

    def __call__(self, record: dict[str, Any],
                 identity_envelope: dict[str, Any]) -> dict[str, Any]: ...


def _instant(value: datetime | str | None, name: str) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, name: str, limit: int = 512, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonblank text of at most {limit} characters")
    return value.strip()


@dataclass(frozen=True)
class DeliveryIdentity:
    """Identity carried unchanged from preparation through reply handling."""

    activity_id: str | None = None
    device_id: str | None = None
    correlation_id: str | None = None
    job_id: str | None = None
    continuation: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None

    @classmethod
    def from_value(cls, value: "DeliveryIdentity | dict[str, Any] | None") -> "DeliveryIdentity":
        if isinstance(value, cls):
            result = value
        elif value is None:
            result = cls()
        elif isinstance(value, dict):
            unknown = set(value) - set(cls.__dataclass_fields__)
            if unknown:
                raise ValueError("unknown delivery identity fields: " + ", ".join(sorted(unknown)))
            result = cls(**value)
        else:
            raise ValueError("identity must be an object")
        for name in ("activity_id", "device_id", "correlation_id", "job_id"):
            _text(getattr(result, name), name)
        if not isinstance(result.continuation, dict) or len(result.continuation) > 16:
            raise ValueError("continuation must be a bounded object")
        _instant(result.expires_at, "expires_at")
        return result

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class APSchedulerProvider:
    """Lazy APScheduler adapter supporting durable date/interval/cron jobs.

    A scheduler can be injected by the composition root.  Otherwise ``open``
    creates a background scheduler with APScheduler's SQLAlchemy job store.
    Calling ``open`` only constructs it; ``start`` is deliberately separate.
    """

    def __init__(self, *, state_path: str | Path | None = None, scheduler=None):
        self.state_path = Path(state_path).resolve() if state_path else None
        self.scheduler = scheduler

    def open(self) -> dict[str, Any]:
        if self.scheduler is not None:
            return {"ok": True, "state": "ready", "persistent": self.state_path is not None}
        if self.state_path is None:
            return {"ok": False, "state": "needs_setup", "provider": "apscheduler",
                    "error": "A persistent scheduler state_path is required"}
        try:
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError as exc:
            return {"ok": False, "state": "unavailable", "provider": "apscheduler",
                    "error": str(exc), "setup": "Install APScheduler with its SQLAlchemy job store"}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler = BackgroundScheduler(jobstores={
            "default": SQLAlchemyJobStore(url="sqlite:///" + str(self.state_path))})
        return {"ok": True, "state": "ready", "persistent": True}

    def start(self) -> dict[str, Any]:
        status = self.open()
        if not status["ok"]:
            return status
        if not getattr(self.scheduler, "running", False):
            self.scheduler.start()
        return {**status, "state": "running"}

    def schedule(self, callback: Callable[..., Any], *, identity=None, trigger="date",
                 trigger_args=None, coalesce=True, misfire_grace_time=300,
                 max_instances=1, replace_existing=False) -> dict[str, Any]:
        ident = DeliveryIdentity.from_value(identity)
        job_id = _text(ident.job_id, "job_id", required=True)
        if trigger not in {"date", "interval", "cron"}:
            raise ValueError("trigger must be date, interval, or cron")
        if not callable(callback):
            raise ValueError("callback must be callable")
        if not isinstance(trigger_args or {}, dict):
            raise ValueError("trigger_args must be an object")
        if not isinstance(misfire_grace_time, int) or misfire_grace_time < 1:
            raise ValueError("misfire_grace_time must be a positive integer")
        status = self.open()
        if not status["ok"]:
            return {**status, "job_id": job_id, "identity": ident.payload()}
        try:
            job = self.scheduler.add_job(
                callback, trigger, id=job_id, kwargs={"identity": ident.payload()},
                coalesce=bool(coalesce), misfire_grace_time=misfire_grace_time,
                max_instances=max_instances, replace_existing=bool(replace_existing),
                **(trigger_args or {}))
        except Exception as exc:
            return {"ok": False, "state": "failed", "provider": "apscheduler",
                    "job_id": job_id, "identity": ident.payload(), "error": str(exc)}
        next_run = getattr(job, "next_run_time", None)
        return {"ok": True, "state": "scheduled", "provider": "apscheduler",
                "job_id": job.id, "identity": ident.payload(), "trigger": trigger,
                "next_run_at": next_run.isoformat() if next_run else None,
                "coalesce": bool(coalesce), "misfire_grace_time": misfire_grace_time}

    def cancel(self, job_id: str) -> dict[str, Any]:
        job_id = _text(job_id, "job_id", required=True)
        status = self.open()
        if not status["ok"]:
            return {**status, "job_id": job_id, "cancellation_requested": False}
        try:
            self.scheduler.remove_job(job_id)
        except Exception as exc:
            return {"ok": False, "state": "failed", "provider": "apscheduler",
                    "job_id": job_id, "cancellation_requested": False, "error": str(exc)}
        return {"ok": True, "state": "accepted", "provider": "apscheduler",
                "job_id": job_id, "cancellation_requested": True,
                "completed": False}


class NtfyProvider:
    """Small ntfy publishing adapter using its documented HTTP contract."""

    def __init__(self, endpoint: str | None = None, topic: str | None = None,
                 *, token: str | None = None, timeout: float = 10,
                 opener: Callable[..., Any] | None = None):
        self.endpoint, self.topic, self.token = endpoint, topic, token
        self.timeout, self.opener = timeout, opener or request.urlopen

    def readiness(self) -> dict[str, Any]:
        if not self.endpoint or not self.topic:
            return {"ok": False, "state": "needs_setup", "provider": "ntfy",
                    "error": "ntfy endpoint and topic are required"}
        parsed = parse.urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "state": "needs_setup", "provider": "ntfy",
                    "error": "ntfy endpoint must be an HTTP(S) URL"}
        return {"ok": True, "state": "ready", "provider": "ntfy"}

    def publish(self, message: str, *, title: str | None = None, priority: int | str = 3,
                actions: list[dict[str, Any]] | None = None, identity=None,
                method: str = "POST") -> dict[str, Any]:
        ident = DeliveryIdentity.from_value(identity)
        ready = self.readiness()
        if not ready["ok"]:
            return {**ready, "identity": ident.payload()}
        expires = _instant(ident.expires_at, "expires_at")
        if expires and expires <= datetime.now(timezone.utc):
            return {"ok": False, "state": "expired", "provider": "ntfy",
                    "identity": ident.payload(), "delivered": False}
        if method not in {"POST", "PUT"}:
            raise ValueError("method must be POST or PUT")
        _text(message, "message", 4096, required=True)
        if actions is not None and (not isinstance(actions, list) or len(actions) > 3):
            raise ValueError("actions must be a list containing at most three actions")
        body = {"topic": self.topic, "message": message, "priority": priority,
                "title": title, "actions": actions or [], "tags": ["assistant"]}
        headers = {"Content-Type": "application/json", "User-Agent": "UltimateAssistant/1"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        target = self.endpoint.rstrip("/")
        req = request.Request(target, data=json.dumps(body).encode("utf-8"),
                              headers=headers, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                response_body = json.loads(raw) if raw else {}
                code = getattr(response, "status", 200)
        except (error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "state": "unavailable", "provider": "ntfy",
                    "identity": ident.payload(), "delivered": False, "retryable": True,
                    "error": str(exc)}
        return {"ok": 200 <= code < 300, "state": "accepted" if 200 <= code < 300 else "failed",
                "provider": "ntfy", "identity": ident.payload(), "delivered": 200 <= code < 300,
                "acknowledged": False, "completed": False, "status_code": code,
                "provider_response": response_body}


class ActivityWatchProvider:
    """Optional ActivityWatch bucket/event adapter; observations are not evidence of completion."""

    def __init__(self, *, client=None, client_id="ultimate-assistant", hostname=None):
        self.client, self.client_id, self.hostname = client, client_id, hostname

    def open(self) -> dict[str, Any]:
        if self.client is not None:
            return {"ok": True, "state": "ready", "provider": "activitywatch"}
        try:
            from aw_client import ActivityWatchClient
        except ImportError as exc:
            return {"ok": False, "state": "unavailable", "provider": "activitywatch",
                    "error": str(exc), "setup": "Install and configure aw-client"}
        try:
            self.client = ActivityWatchClient(self.client_id, testing=False)
        except Exception as exc:
            return {"ok": False, "state": "unavailable", "provider": "activitywatch",
                    "error": str(exc), "setup": "Start or configure ActivityWatch"}
        return {"ok": True, "state": "ready", "provider": "activitywatch"}

    def buckets(self) -> dict[str, Any]:
        status = self.open()
        if not status["ok"]:
            return status
        try:
            return {**status, "buckets": self.client.get_buckets()}
        except Exception as exc:
            return {"ok": False, "state": "unavailable", "provider": "activitywatch", "error": str(exc)}

    def create_bucket(self, bucket_id: str, bucket_type: str) -> dict[str, Any]:
        status = self.open()
        if not status["ok"]:
            return status
        try:
            self.client.create_bucket(
                _text(bucket_id, "bucket_id", required=True),
                _text(bucket_type, "bucket_type", required=True),
                client=self.client_id, hostname=self.hostname, queued=True)
            return {**status, "state": "accepted", "bucket_id": bucket_id,
                    "observation_only": True, "completed": False}
        except Exception as exc:
            return {"ok": False, "state": "unavailable", "provider": "activitywatch", "error": str(exc)}

    def events(self, bucket_id: str, *, start=None, end=None, limit=100) -> dict[str, Any]:
        status = self.open()
        if not status["ok"]:
            return status
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        try:
            values = self.client.get_events(_text(bucket_id, "bucket_id", required=True),
                                            starttime=_instant(start, "start"),
                                            endtime=_instant(end, "end"), limit=limit)
            return {**status, "bucket_id": bucket_id, "events": values,
                    "observation_only": True, "completion_inferred": False}
        except Exception as exc:
            return {"ok": False, "state": "unavailable", "provider": "activitywatch", "error": str(exc)}

    def heartbeat(self, bucket_id: str, event: dict[str, Any], *, pulsetime=5) -> dict[str, Any]:
        status = self.open()
        if not status["ok"]:
            return status
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        try:
            self.client.heartbeat(_text(bucket_id, "bucket_id", required=True),
                                  event, pulsetime=pulsetime, queued=True)
            return {**status, "state": "accepted", "bucket_id": bucket_id,
                    "observation_only": True, "completed": False}
        except Exception as exc:
            return {"ok": False, "state": "unavailable", "provider": "activitywatch", "error": str(exc)}

    def query(self, queries: list[str], *, start, end) -> dict[str, Any]:
        status = self.open()
        if not status["ok"]:
            return status
        if not isinstance(queries, list) or not queries or not all(isinstance(q, str) for q in queries):
            raise ValueError("queries must be a nonempty list of strings")
        try:
            period = [(_instant(start, "start"), _instant(end, "end"))]
            values = self.client.query(queries, period)
            return {**status, "results": values, "observation_only": True,
                    "completion_inferred": False}
        except Exception as exc:
            return {"ok": False, "state": "unavailable", "provider": "activitywatch", "error": str(exc)}


# Descriptive aliases keep composition code independent of upstream branding.
ScheduleProvider = APSchedulerProvider
NotificationProvider = NtfyProvider
ActivityProvider = ActivityWatchProvider
