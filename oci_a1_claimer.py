#!/usr/bin/env python3
"""Poll OCI capacity and launch one Always Free Ampere A1.Flex instance.

The program uses OCI's public API and API signing-key authentication.  It does
not automate the OCI web console, bypass a CAPTCHA, or create more than one
instance with the configured display name.

The default target matches the current Always Free allowance discussed in the
project: VM.Standard.A1.Flex with 2 OCPUs and 12 GB of RAM in Monterrey.
"""

from __future__ import annotations

import argparse
import dataclasses
import email.utils
import fcntl
import json
import logging
import math
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    import oci  # type: ignore
except ImportError:  # Keep --help and local unit tests usable without the SDK.
    oci = None  # type: ignore


LOG = logging.getLogger("oci-a1-claimer")

RUNTIME_FORMAT_VERSION = 1
RUNTIME_RETENTION_SECONDS = 24 * 60 * 60
RUNTIME_EVENT_LIMIT = 2000
DEFAULT_FAULT_DOMAINS = (
    "FAULT-DOMAIN-1",
    "FAULT-DOMAIN-2",
    "FAULT-DOMAIN-3",
)
LAUNCH_RESULTS = {
    "created",
    "out_of_capacity",
    "rate_limited",
    "transient_error",
    "fatal_error",
}


class ConfigurationError(RuntimeError):
    """The local configuration is incomplete or inconsistent."""


class FatalCloudError(RuntimeError):
    """OCI returned an error that should not be retried automatically."""


class TemporaryCloudError(RuntimeError):
    """A non-fatal OCI error prevented a read-only check."""


class RateLimitedError(TemporaryCloudError):
    """OCI asked the process to slow down."""


class SystemClock:
    """Clock adapter used by the claimer and replaceable by deterministic tests."""

    @staticmethod
    def time() -> float:
        return time.time()

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    """Parse a human-friendly environment boolean."""

    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigurationError(
        f"Valor booleano inválido: {value!r}. Usa true/false, yes/no o 1/0."
    )


def parse_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None or raw.strip() == "" else int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} debe ser un entero; recibí {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} debe ser >= {minimum}; recibí {value}.")
    return value


def parse_float(name: str, default: float, minimum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None or raw.strip() == "" else float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} debe ser un número; recibí {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} debe ser >= {minimum}; recibí {value}.")
    return value


def parse_csv(value: Optional[str]) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Falta la variable obligatoria {name}.")
    return value


def attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read an SDK model attribute without failing on older SDK versions."""

    return getattr(obj, name, default)


def numeric_status(exc: BaseException) -> Optional[int]:
    value = attr(exc, "status", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def compact_error(exc: BaseException) -> str:
    """Return a useful, non-secret error summary for logs and Telegram."""

    status = attr(exc, "status", "?")
    code = attr(exc, "code", type(exc).__name__)
    message = attr(exc, "message", str(exc)) or str(exc)
    message = " ".join(str(message).split())
    return f"HTTP {status} / {code}: {message[:350]}"


def error_text(exc: BaseException) -> str:
    return " ".join(
        str(part or "")
        for part in (attr(exc, "code", ""), attr(exc, "message", ""), str(exc))
    ).lower()


def is_capacity_error(exc: BaseException) -> bool:
    text = error_text(exc)
    markers = (
        "out of host capacity",
        "out_of_host_capacity",
        "outofhostcapacity",
        "capacity is unavailable",
        "capacity unavailable",
        "not enough capacity",
    )
    return any(marker in text for marker in markers)


def is_rate_limited(exc: BaseException) -> bool:
    status = numeric_status(exc)
    code = str(attr(exc, "code", "")).lower()
    text = error_text(exc)
    return (
        status == 429
        or code in {"too many requests", "toomanyrequests", "ratelimited", "throttled"}
        or "too many requests" in text
        or "rate limit" in text
        or "throttl" in text
    )


def is_transient_error(exc: BaseException) -> bool:
    status = numeric_status(exc)
    code = str(attr(exc, "code", "")).lower()
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    if code in {
        "internalerror",
        "serviceunavailable",
        "toomanyrequests",
        "timeout",
        "requesttimeout",
    }:
        return True
    return type(exc).__name__ in {
        "RequestException",
        "TimeoutError",
        "ConnectionError",
        "URLError",
    }


def is_ambiguous_error(exc: BaseException) -> bool:
    """Whether OCI may have accepted a launch before the response was lost."""

    return (
        type(exc).__name__
        in {"RequestException", "TimeoutError", "ConnectionError", "URLError"}
        or numeric_status(exc) in {408, 500, 502, 503, 504}
    )


def is_capacity_endpoint_permanent_error(exc: BaseException) -> bool:
    """Identify an account/endpoint limitation, not a temporary API failure."""

    status = numeric_status(exc)
    code = str(attr(exc, "code", "")).lower().replace("_", "")
    text = error_text(exc)
    if status in {401, 403, 404, 405, 501}:
        return True
    permanent_codes = {
        "notauthorized",
        "notauthorizedornotfound",
        "notsupported",
        "unsupportedoperation",
        "operationnotsupported",
    }
    if code in permanent_codes:
        return True
    markers = (
        "capacity report is not supported",
        "capacity report not supported",
        "capacity report is unavailable for this account",
        "not authorized to use capacity report",
        "endpoint is not supported",
    )
    return any(marker in text for marker in markers)


def retry_after_seconds(exc: BaseException, now: float) -> Optional[int]:
    """Read a valid Retry-After value from common OCI SDK error shapes."""

    values: list[Any] = []
    direct = attr(exc, "retry_after", None)
    if direct is not None:
        values.append(direct)
    for owner in (exc, attr(exc, "response", None), attr(exc, "raw_response", None)):
        headers = attr(owner, "headers", None)
        if headers is None:
            continue
        try:
            for name in ("retry-after", "Retry-After", "retry_after"):
                if hasattr(headers, "get"):
                    value = headers.get(name)
                    if value is not None:
                        values.append(value)
        except Exception:
            continue

    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        try:
            seconds = float(value)
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                if parsed is None:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = parsed.timestamp() - now
            except (TypeError, ValueError, OverflowError, IndexError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return max(1, int(math.ceil(seconds)))
    return None


def utc_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class Settings:
    region: str
    profile: str
    config_file: Path
    compartment_id: str
    subnet_id: Optional[str]
    vcn_id: Optional[str]
    image_id: Optional[str]
    image_os: str
    image_os_version: Optional[str]
    image_name_regex: Optional[str]
    shape: str
    ocpus: float
    memory_gbs: float
    boot_volume_gbs: int
    instance_name: str
    ssh_public_key_path: Optional[Path]
    ssh_public_key: Optional[str]
    assign_public_ip: bool
    fault_domains: tuple[str, ...]
    capacity_report: bool
    direct_fallback_every: int
    interval_seconds: int
    jitter_seconds: int
    direct_attempt_interval_seconds: int
    direct_attempt_jitter_seconds: int
    min_launch_gap_seconds: int
    create_retries: int
    create_retry_delay: int
    instance_wait_seconds: int
    existing_check_interval_seconds: int
    max_attempts: int
    state_file: Path
    runtime_file: Path
    lock_file: Path
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    ssh_user: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        key_path_raw = os.getenv("OCI_SSH_PUBLIC_KEY_PATH", "").strip()
        ssh_key_raw = os.getenv("OCI_SSH_PUBLIC_KEY", "").strip()
        if not key_path_raw and not ssh_key_raw:
            for candidate in ("~/.ssh/id_ed25519.pub", "~/.ssh/id_rsa.pub"):
                if Path(candidate).expanduser().is_file():
                    key_path_raw = candidate
                    break

        state_file = Path(
            os.getenv("OCI_STATE_FILE", "./instance.json")
        ).expanduser()
        runtime_file = Path(
            os.getenv("OCI_RUNTIME_FILE", str(state_file.with_name("runtime.json")))
        ).expanduser()
        if runtime_file.resolve() == state_file.resolve():
            raise ConfigurationError(
                "OCI_RUNTIME_FILE debe ser diferente de OCI_STATE_FILE; "
                "runtime.json no sustituye a instance.json."
            )
        lock_file = Path(
            os.getenv("OCI_LOCK_FILE", str(state_file.with_suffix(".lock")))
        ).expanduser()

        return cls(
            region=os.getenv("OCI_REGION", "mx-monterrey-1").strip(),
            profile=os.getenv("OCI_PROFILE", "DEFAULT").strip(),
            config_file=Path(
                os.getenv("OCI_CONFIG_FILE", "~/.oci/config")
            ).expanduser(),
            compartment_id=required_env("OCI_COMPARTMENT_ID"),
            subnet_id=os.getenv("OCI_SUBNET_ID", "").strip() or None,
            vcn_id=os.getenv("OCI_VCN_ID", "").strip() or None,
            image_id=os.getenv("OCI_IMAGE_ID", "").strip() or None,
            image_os=os.getenv("OCI_IMAGE_OS", "Oracle Linux").strip(),
            image_os_version=os.getenv("OCI_IMAGE_OS_VERSION", "").strip() or None,
            image_name_regex=os.getenv("OCI_IMAGE_NAME_REGEX", "").strip() or None,
            shape=os.getenv("OCI_SHAPE", "VM.Standard.A1.Flex").strip(),
            ocpus=parse_float("OCI_OCPUS", 2.0, 0.1),
            memory_gbs=parse_float("OCI_MEMORY_GBS", 12.0, 1.0),
            boot_volume_gbs=parse_int("OCI_BOOT_VOLUME_GBS", 50, 50),
            instance_name=os.getenv("OCI_INSTANCE_NAME", "a1-free-monterrey").strip(),
            ssh_public_key_path=Path(key_path_raw).expanduser() if key_path_raw else None,
            ssh_public_key=ssh_key_raw or None,
            assign_public_ip=parse_bool(os.getenv("OCI_ASSIGN_PUBLIC_IP"), True),
            fault_domains=parse_csv(os.getenv("OCI_FALLBACK_FAULT_DOMAINS")),
            capacity_report=parse_bool(os.getenv("OCI_CAPACITY_REPORT"), True),
            # Kept as a compatibility setting; direct attempts are now timed
            # by OCI_DIRECT_ATTEMPT_INTERVAL_SECONDS instead of cycle counts.
            direct_fallback_every=parse_int("OCI_DIRECT_FALLBACK_EVERY", 10, 1),
            interval_seconds=parse_int("OCI_INTERVAL_SECONDS", 60, 15),
            jitter_seconds=parse_int("OCI_JITTER_SECONDS", 15, 0),
            direct_attempt_interval_seconds=parse_int(
                "OCI_DIRECT_ATTEMPT_INTERVAL_SECONDS", 240, 1
            ),
            direct_attempt_jitter_seconds=parse_int(
                "OCI_DIRECT_ATTEMPT_JITTER_SECONDS", 30, 0
            ),
            min_launch_gap_seconds=parse_int("OCI_MIN_LAUNCH_GAP_SECONDS", 60, 1),
            create_retries=parse_int("OCI_CREATE_RETRIES", 3, 1),
            create_retry_delay=parse_int("OCI_CREATE_RETRY_DELAY", 10, 1),
            instance_wait_seconds=parse_int("OCI_INSTANCE_WAIT_SECONDS", 300, 30),
            existing_check_interval_seconds=parse_int(
                "OCI_EXISTING_CHECK_INTERVAL_SECONDS", 900, 60
            ),
            max_attempts=parse_int("OCI_MAX_ATTEMPTS", 0, 0),
            state_file=state_file,
            runtime_file=runtime_file,
            lock_file=lock_file,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
            ssh_user=os.getenv("OCI_SSH_USER", "opc").strip() or "opc",
            log_level=os.getenv("OCI_LOG_LEVEL", "INFO").strip().upper(),
        )


@dataclass(frozen=True)
class Candidate:
    availability_domain: str
    fault_domain: Optional[str]


class Claimer:
    def __init__(
        self,
        settings: Settings,
        *,
        clock: Optional[Any] = None,
        rng: Optional[Any] = None,
        sleeper: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        self.stop_event = threading.Event()
        self._sleeper = sleeper or (lambda seconds: self.stop_event.wait(seconds))
        self.identity: Any = None
        self.compute: Any = None
        self.network: Any = None
        self.oci_config: dict[str, Any] = {}
        self.tenancy_id: Optional[str] = None
        self.availability_domains: list[str] = []
        self.image: Any = None
        self.subnet: Any = None
        self.ssh_key: Optional[str] = None
        self.validated = False
        self.capacity_report_enabled = settings.capacity_report
        self.runtime = self.load_runtime()
        # A new process is a new service run. This is intentionally separate
        # from instance.json, whose meaning remains "successful creation".
        self.runtime["service_started_at"] = utc_timestamp(self.now())
        # OCI_MAX_ATTEMPTS limits this process invocation. The cumulative
        # metric remains in runtime.json independently of that safety limit.
        self.attempts = 0
        self.save_runtime()

    def now(self) -> float:
        return float(self.clock.time())

    def monotonic(self) -> float:
        return float(self.clock.monotonic())

    def install_signal_handlers(self) -> None:
        def stop_handler(signum: int, _frame: Any) -> None:
            LOG.info("Señal %s recibida; terminando de forma segura.", signum)
            self.stop_event.set()

        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)

    def require_sdk(self) -> None:
        if oci is None:
            raise ConfigurationError(
                "Falta el SDK de OCI. Ejecuta: python3 -m pip install -r requirements.txt"
            )

    def _runtime_int(self, key: str, default: int) -> int:
        try:
            value = int(self.runtime.get(key, default))
        except (TypeError, ValueError):
            value = default
        return value if value >= 0 else default

    def _default_runtime(self) -> dict[str, Any]:
        return {
            "format_version": RUNTIME_FORMAT_VERSION,
            "service_started_at": utc_timestamp(self.now()),
            "last_cycle": None,
            "last_capacity_report": None,
            "last_real_attempt": None,
            "next_attempt_allowed": None,
            "fault_domain_index": 0,
            "cooldown_until": None,
            "consecutive_429": 0,
            "capacity_report_backoff_until": None,
            "capacity_report_failures": 0,
            "capacity_report_enabled": self.settings.capacity_report,
            "capacity_report_disabled_reason": None,
            "last_existing_check": None,
            "total_real_attempts": 0,
            "pending_candidates": [],
            "pending_retry": None,
            "recent_events": [],
        }

    def load_runtime(self) -> dict[str, Any]:
        defaults = self._default_runtime()
        path = self.settings.runtime_file
        if not path.exists():
            LOG.warning(
                "No existe %s; iniciaré métricas runtime con valores seguros.",
                path,
            )
            return defaults
        try:
            with path.open("r", encoding="utf-8") as source:
                loaded = json.load(source)
            if not isinstance(loaded, dict):
                raise ValueError("la raíz JSON no es un objeto")
            if loaded.get("format_version", RUNTIME_FORMAT_VERSION) != RUNTIME_FORMAT_VERSION:
                raise ValueError("versión de formato no compatible")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOG.warning(
                "No pude leer %s (%s); usaré valores runtime seguros sin tocar instance.json.",
                path,
                exc,
            )
            return defaults

        runtime = defaults
        runtime.update(loaded)
        if not isinstance(runtime.get("recent_events"), list):
            runtime["recent_events"] = []
        if not isinstance(runtime.get("pending_candidates"), list):
            runtime["pending_candidates"] = []
        if not isinstance(runtime.get("pending_retry"), (dict, type(None))):
            runtime["pending_retry"] = None
        try:
            runtime["fault_domain_index"] = max(0, int(runtime["fault_domain_index"]))
        except (KeyError, TypeError, ValueError):
            runtime["fault_domain_index"] = 0
        try:
            runtime["consecutive_429"] = max(0, int(runtime["consecutive_429"]))
        except (KeyError, TypeError, ValueError):
            runtime["consecutive_429"] = 0
        try:
            runtime["total_real_attempts"] = max(0, int(runtime["total_real_attempts"]))
        except (KeyError, TypeError, ValueError):
            runtime["total_real_attempts"] = 0
        return runtime

    def _prune_events(self, now: Optional[float] = None) -> None:
        current = self.now() if now is None else now
        cutoff = current - RUNTIME_RETENTION_SECONDS
        events: list[dict[str, Any]] = []
        for event in self.runtime.get("recent_events", []):
            if not isinstance(event, dict):
                continue
            timestamp = parse_timestamp(event.get("timestamp"))
            if timestamp is None or timestamp >= cutoff:
                events.append(event)
        self.runtime["recent_events"] = events[-RUNTIME_EVENT_LIMIT:]

    def save_runtime(self) -> None:
        """Persist runtime state atomically with restrictive permissions."""

        path = self.settings.runtime_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._prune_events()
            payload = json.dumps(
                self.runtime,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"
            fd, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.",
                dir=str(path.parent),
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            # Metrics must not take the claimer down. The next heartbeat can
            # use its log fallback if the runtime path is temporarily unwritable.
            LOG.warning("No pude guardar el runtime en %s: %s", path, exc)

    def _append_event(
        self,
        event_type: str,
        result: str,
        *,
        now: Optional[float] = None,
        **details: Any,
    ) -> None:
        timestamp = self.now() if now is None else now
        event: dict[str, Any] = {
            "timestamp": utc_timestamp(timestamp),
            "type": event_type,
            "result": result,
        }
        for key, value in details.items():
            if value is not None:
                event[key] = value
        self.runtime.setdefault("recent_events", []).append(event)
        self._prune_events(timestamp)
        self.save_runtime()

    def events_in_window(
        self,
        seconds: int = 3600,
        now: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        current = self.now() if now is None else now
        cutoff = current - seconds
        events: list[dict[str, Any]] = []
        for event in self.runtime.get("recent_events", []):
            if not isinstance(event, dict):
                continue
            timestamp = parse_timestamp(event.get("timestamp"))
            if timestamp is not None and cutoff <= timestamp <= current:
                events.append(event)
        return events

    def _runtime_time(self, key: str) -> Optional[float]:
        return parse_timestamp(self.runtime.get(key))

    def _set_runtime_time(self, key: str, value: Optional[float]) -> None:
        self.runtime[key] = utc_timestamp(value) if value is not None else None

    def _cooldown_remaining(self, now: Optional[float] = None) -> float:
        current = self.now() if now is None else now
        until = self._runtime_time("cooldown_until")
        return max(0.0, (until or 0.0) - current)

    def cooldown_active(self, now: Optional[float] = None) -> bool:
        return self._cooldown_remaining(now) > 0

    def _random_between(self, lower: float, upper: float) -> float:
        if upper <= lower:
            return lower
        return float(self.rng.uniform(lower, upper))

    def _direct_delay(self) -> float:
        jitter = self.settings.direct_attempt_jitter_seconds
        delay = self.settings.direct_attempt_interval_seconds
        if jitter:
            delay += self._random_between(-jitter, jitter)
        return max(float(self.settings.min_launch_gap_seconds), delay)

    def _retry_delay(self, retry_index: int) -> float:
        base = min(600.0, float(self.settings.create_retry_delay) * (2**retry_index))
        jitter = min(30.0, max(1.0, base * 0.1))
        return base + self._random_between(0.0, jitter)

    def _capacity_report_delay(self, failure_count: int) -> float:
        base = min(600.0, 60.0 * (2 ** max(0, failure_count - 1)))
        jitter = min(30.0, max(1.0, base * 0.1))
        return min(600.0, base + self._random_between(0.0, jitter))

    def _record_non_429_response(self) -> None:
        current = self._runtime_int("consecutive_429", 0)
        if current:
            self.runtime["consecutive_429"] = max(0, current - 1)
            self.save_runtime()

    def activate_rate_limit(self, exc: BaseException, source: str) -> float:
        """Set one global cooldown for capacity checks, instance checks and launches."""

        now = self.now()
        count = self._runtime_int("consecutive_429", 0) + 1
        retry_after = retry_after_seconds(exc, now)
        if retry_after is None:
            delay = min(
                600.0,
                60.0 * (2 ** max(0, count - 1))
                + self._random_between(0.0, min(30.0, 60.0)),
            )
            reason = f"backoff exponencial con jitter ({int(math.ceil(delay))} s)"
        else:
            delay = float(retry_after)
            reason = f"Retry-After de OCI ({int(math.ceil(delay))} s)"
        until = max(self._runtime_time("cooldown_until") or 0.0, now + delay)
        self.runtime["consecutive_429"] = count
        self._set_runtime_time("cooldown_until", until)
        next_allowed = self._runtime_time("next_attempt_allowed") or 0.0
        self._set_runtime_time("next_attempt_allowed", max(next_allowed, until))
        self.save_runtime()
        LOG.warning(
            "HTTP 429 en %s; API limitada, ritmo reducido automáticamente durante %s.",
            source,
            reason,
        )
        return until

    def _schedule_next_attempt(self, now: Optional[float] = None) -> float:
        current = self.now() if now is None else now
        next_allowed = current + self._direct_delay()
        cooldown_until = self._runtime_time("cooldown_until") or 0.0
        next_allowed = max(next_allowed, cooldown_until)
        self._set_runtime_time("next_attempt_allowed", next_allowed)
        self.save_runtime()
        return next_allowed

    def _record_launch_result(
        self,
        result: str,
        candidate: Candidate,
        *,
        retry_index: int,
        source: str,
        now: Optional[float] = None,
    ) -> None:
        if result not in LAUNCH_RESULTS:
            raise ValueError(f"Resultado de launch_instance inválido: {result}")
        self._append_event(
            "launch",
            result,
            now=now,
            availability_domain=candidate.availability_domain,
            fault_domain=candidate.fault_domain,
            retry_index=retry_index,
            source=source,
        )
        LOG.info(
            "Resultado de solicitud real: %s (%s / %s; reintento %d).",
            result,
            candidate.availability_domain,
            candidate.fault_domain or "capacity-report-auto",
            retry_index + 1,
        )

    def _record_capacity_result(
        self,
        result: str,
        availability_domain: str,
        *,
        now: Optional[float] = None,
    ) -> None:
        self._append_event(
            "capacity_report",
            result,
            now=now,
            availability_domain=availability_domain,
        )

    def _begin_real_attempt(
        self,
        candidate: Candidate,
        retry_index: int,
        total_retries: int,
    ) -> bool:
        now = self.now()
        if self.cooldown_active(now):
            return False
        if self.settings.max_attempts and self.attempts >= self.settings.max_attempts:
            return False
        self.attempts += 1
        self.runtime["total_real_attempts"] = self.attempts
        self._set_runtime_time("last_real_attempt", now)
        self.save_runtime()
        LOG.info(
            "Solicitud real de creación #%d: %s / %s (reintento %d/%d).",
            self.attempts,
            candidate.availability_domain,
            candidate.fault_domain or "capacity-report-auto",
            retry_index + 1,
            total_retries,
        )
        return True

    def initialize_clients(self) -> None:
        self.require_sdk()
        if not self.settings.config_file.is_file():
            raise ConfigurationError(
                f"No existe el archivo de configuración OCI: {self.settings.config_file}"
            )
        try:
            self.oci_config = oci.config.from_file(
                str(self.settings.config_file), self.settings.profile
            )
            self.oci_config["region"] = self.settings.region
            oci.config.validate_config(self.oci_config)
        except Exception as exc:
            raise ConfigurationError(
                f"No pude cargar/validar {self.settings.config_file}: {exc}"
            ) from exc

        self.tenancy_id = self.oci_config.get("tenancy")
        if not self.tenancy_id:
            raise ConfigurationError(
                "El perfil OCI no contiene tenancy; usa el fragmento de configuración de Oracle."
            )
        timeout = (10, 60)
        self.identity = oci.identity.IdentityClient(self.oci_config, timeout=timeout)
        self.compute = oci.core.ComputeClient(self.oci_config, timeout=timeout)
        self.network = oci.core.VirtualNetworkClient(self.oci_config, timeout=timeout)

    @staticmethod
    def all_results(call: Any, **kwargs: Any) -> list[Any]:
        """Use OCI's pagination helper while keeping the call site readable."""

        return list(oci.pagination.list_call_get_all_results(call, **kwargs).data)

    def resolve_availability_domains(self) -> None:
        try:
            domains = self.all_results(
                self.identity.list_availability_domains,
                compartment_id=self.tenancy_id,
            )
        except Exception as exc:
            raise FatalCloudError(
                f"No pude listar los availability domains: {compact_error(exc)}"
            ) from exc
        self.availability_domains = [
            attr(item, "name") for item in domains if attr(item, "name")
        ]
        if not self.availability_domains:
            raise ConfigurationError("OCI no devolvió ningún availability domain.")

        requested = os.getenv("OCI_AVAILABILITY_DOMAIN", "").strip()
        if requested:
            if requested not in self.availability_domains:
                raise ConfigurationError(
                    "OCI_AVAILABILITY_DOMAIN no coincide con los dominios disponibles: "
                    + ", ".join(self.availability_domains)
                )
            self.availability_domains = [requested]

    def resolve_image(self) -> None:
        if self.settings.image_id:
            try:
                self.image = self.compute.get_image(self.settings.image_id).data
            except Exception as exc:
                raise FatalCloudError(
                    f"No pude obtener OCI_IMAGE_ID: {compact_error(exc)}"
                ) from exc
            if attr(self.image, "lifecycle_state") not in {None, "AVAILABLE"}:
                raise ConfigurationError(
                    f"La imagen indicada no está AVAILABLE: {attr(self.image, 'lifecycle_state')}"
                )
            return

        kwargs: dict[str, Any] = {
            "compartment_id": self.settings.compartment_id,
            "operating_system": self.settings.image_os,
            "shape": self.settings.shape,
            "sort_by": "TIMECREATED",
            "sort_order": "DESC",
            "limit": 50,
        }
        if self.settings.image_os_version:
            kwargs["operating_system_version"] = self.settings.image_os_version
        try:
            images = self.all_results(self.compute.list_images, **kwargs)
        except Exception as exc:
            raise FatalCloudError(
                f"No pude listar imágenes compatibles con {self.settings.shape}: "
                f"{compact_error(exc)}"
            ) from exc

        pattern = (
            re.compile(self.settings.image_name_regex)
            if self.settings.image_name_regex
            else None
        )
        for image in images:
            if attr(image, "lifecycle_state") != "AVAILABLE":
                continue
            display_name = attr(image, "display_name", "") or ""
            if pattern and not pattern.search(display_name):
                continue
            self.image = image
            break

        if self.image is None:
            raise ConfigurationError(
                "No encontré una imagen AVAILABLE compatible. Ejecuta --discover o define "
                "OCI_IMAGE_ID con el OCID de una imagen ARM compatible."
            )

    def resolve_subnet(self) -> None:
        if self.settings.subnet_id:
            try:
                self.subnet = self.network.get_subnet(self.settings.subnet_id).data
            except Exception as exc:
                raise FatalCloudError(
                    f"No pude obtener OCI_SUBNET_ID: {compact_error(exc)}"
                ) from exc
        else:
            self.subnet = self.discover_public_subnet()

        if attr(self.subnet, "lifecycle_state") not in {None, "AVAILABLE"}:
            raise ConfigurationError(
                f"La subred no está AVAILABLE: {attr(self.subnet, 'lifecycle_state')}"
            )
        if self.settings.assign_public_ip and attr(
            self.subnet, "prohibit_public_ip_on_vnic", False
        ):
            raise ConfigurationError(
                "La subred elegida prohíbe IP pública, pero OCI_ASSIGN_PUBLIC_IP=true. "
                "Usa una subred pública o cambia esa variable."
            )

    def discover_public_subnet(self) -> Any:
        """Find a single sensible public subnet when the OCID was omitted."""

        try:
            if self.settings.vcn_id:
                vcns = [self.network.get_vcn(self.settings.vcn_id).data]
            else:
                vcns = self.all_results(
                    self.network.list_vcns,
                    compartment_id=self.settings.compartment_id,
                )
            if not vcns:
                raise ConfigurationError("No encontré VCNs en el compartimento.")

            default_vcns = [vcn for vcn in vcns if attr(vcn, "is_default", False)]
            search_vcns = default_vcns or vcns
            public_subnets: list[Any] = []
            for vcn in search_vcns:
                subnets = self.all_results(
                    self.network.list_subnets,
                    compartment_id=self.settings.compartment_id,
                    vcn_id=attr(vcn, "id"),
                )
                public_subnets.extend(
                    subnet
                    for subnet in subnets
                    if attr(subnet, "lifecycle_state") == "AVAILABLE"
                    and not attr(subnet, "prohibit_public_ip_on_vnic", True)
                )
        except ConfigurationError:
            raise
        except Exception as exc:
            raise FatalCloudError(
                f"No pude descubrir una subred pública: {compact_error(exc)}"
            ) from exc

        if not public_subnets:
            raise ConfigurationError(
                "No encontré una subred pública. Define OCI_SUBNET_ID con el OCID de "
                "una subred pública de tu VCN."
            )

        # Prefer a regional subnet and then one whose name says public.
        public_subnets.sort(
            key=lambda subnet: (
                attr(subnet, "availability_domain") is not None,
                "public" not in (attr(subnet, "display_name", "") or "").lower(),
            )
        )
        if len(public_subnets) > 1:
            first = public_subnets[0]
            first_score = (
                attr(first, "availability_domain") is None,
                "public" in (attr(first, "display_name", "") or "").lower(),
            )
            second_scores = [
                (
                    attr(subnet, "availability_domain") is None,
                    "public" in (attr(subnet, "display_name", "") or "").lower(),
                )
                for subnet in public_subnets[1:]
            ]
            if any(score == first_score for score in second_scores):
                choices = ", ".join(
                    f"{attr(subnet, 'display_name', '(sin nombre)')}={attr(subnet, 'id')}"
                    for subnet in public_subnets
                )
                raise ConfigurationError(
                    "Hay varias subredes públicas y no es seguro adivinar. Define "
                    f"OCI_SUBNET_ID. Opciones: {choices}"
                )
        return public_subnets[0]

    def resolve_ssh_key(self) -> None:
        if self.settings.ssh_public_key:
            self.ssh_key = self.settings.ssh_public_key.strip()
        elif self.settings.ssh_public_key_path:
            try:
                self.ssh_key = self.settings.ssh_public_key_path.read_text(
                    encoding="utf-8"
                ).strip()
            except OSError as exc:
                raise ConfigurationError(
                    f"No pude leer la clave SSH pública {self.settings.ssh_public_key_path}: {exc}"
                ) from exc
        if not self.ssh_key:
            raise ConfigurationError(
                "Falta una clave SSH pública. Define OCI_SSH_PUBLIC_KEY_PATH o "
                "OCI_SSH_PUBLIC_KEY."
            )
        if not (
            self.ssh_key.startswith("ssh-")
            or self.ssh_key.startswith("ecdsa-")
            or self.ssh_key.startswith("sk-")
        ):
            raise ConfigurationError(
                "La clave SSH no parece estar en formato OpenSSH (.pub)."
            )

    def validate(self) -> None:
        if self.validated:
            return
        self.initialize_clients()
        self.resolve_availability_domains()
        self.resolve_image()
        self.resolve_subnet()
        self.resolve_ssh_key()
        LOG.info("Región: %s", self.settings.region)
        LOG.info("Availability domains: %s", ", ".join(self.availability_domains))
        LOG.info(
            "Objetivo: %s (%s OCPU, %s GB, %s)",
            self.settings.shape,
            self.settings.ocpus,
            self.settings.memory_gbs,
            self.settings.instance_name,
        )
        LOG.info(
            "Imagen: %s (%s)",
            attr(self.image, "display_name", "(indicada por OCID)"),
            attr(self.image, "id", ""),
        )
        LOG.info(
            "Subred: %s (%s)",
            attr(self.subnet, "display_name", "(sin nombre)"),
            attr(self.subnet, "id", ""),
        )
        LOG.info(
            "Ritmo directo: cada %d±%d s; separación mínima: %d s.",
            self.settings.direct_attempt_interval_seconds,
            self.settings.direct_attempt_jitter_seconds,
            self.settings.min_launch_gap_seconds,
        )
        self.validated = True

    def existing_instance(self) -> Any:
        if self.cooldown_active():
            raise RateLimitedError("cooldown global activo")
        try:
            instances = self.all_results(
                self.compute.list_instances,
                compartment_id=self.settings.compartment_id,
                display_name=self.settings.instance_name,
            )
        except Exception as exc:
            if is_rate_limited(exc):
                self.activate_rate_limit(exc, "existing_instance")
                self._append_event("existing_instance", "rate_limited")
                raise RateLimitedError(compact_error(exc)) from exc
            if is_transient_error(exc):
                self._record_non_429_response()
                raise TemporaryCloudError(
                    f"No pude comprobar si ya existe la instancia temporalmente: "
                    f"{compact_error(exc)}"
                ) from exc
            raise FatalCloudError(
                f"No pude comprobar si ya existe la instancia: {compact_error(exc)}"
            ) from exc

        self._record_non_429_response()
        self._set_runtime_time("last_existing_check", self.now())
        self.save_runtime()
        terminal_states = {"TERMINATED", "DELETED", "FAILED"}
        for instance in instances:
            if attr(instance, "lifecycle_state") not in terminal_states:
                return instance
        return None

    def should_check_existing(self) -> bool:
        last = self._runtime_time("last_existing_check")
        return last is None or self.now() - last >= self.settings.existing_check_interval_seconds

    def _fault_domain_names(self) -> tuple[str, ...]:
        return self.settings.fault_domains or DEFAULT_FAULT_DOMAINS

    def fallback_candidates(self, availability_domain: str) -> list[Candidate]:
        """Return one persisted, rotating direct-placement candidate only."""

        domains = self._fault_domain_names()
        index = self._runtime_int("fault_domain_index", 0) % len(domains)
        return [Candidate(availability_domain, domains[index])]

    def _consume_direct_candidate(self, availability_domain: str) -> Candidate:
        domains = self._fault_domain_names()
        index = self._runtime_int("fault_domain_index", 0) % len(domains)
        candidate = Candidate(availability_domain, domains[index])
        self.runtime["fault_domain_index"] = (index + 1) % len(domains)
        self.save_runtime()
        return candidate

    def _pending_candidates(self) -> list[Candidate]:
        candidates: list[Candidate] = []
        for raw in self.runtime.get("pending_candidates", []):
            if not isinstance(raw, dict):
                continue
            availability_domain = raw.get("availability_domain")
            if not availability_domain:
                continue
            candidate = Candidate(availability_domain, raw.get("fault_domain"))
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _enqueue_candidates(self, candidates: Iterable[Candidate]) -> None:
        pending = self._pending_candidates()
        for candidate in candidates:
            if candidate not in pending:
                pending.append(candidate)
        self.runtime["pending_candidates"] = [
            {
                "availability_domain": candidate.availability_domain,
                "fault_domain": candidate.fault_domain,
            }
            for candidate in pending
        ]
        self.save_runtime()

    def _remove_pending_candidate(self, candidate: Candidate) -> None:
        pending = [item for item in self._pending_candidates() if item != candidate]
        self.runtime["pending_candidates"] = [
            {
                "availability_domain": item.availability_domain,
                "fault_domain": item.fault_domain,
            }
            for item in pending
        ]
        self.save_runtime()

    def _retry_context(self) -> Optional[dict[str, Any]]:
        raw = self.runtime.get("pending_retry")
        if not isinstance(raw, dict):
            return None
        candidate = raw.get("candidate")
        token = raw.get("opc_retry_token")
        retry_index = raw.get("retry_index")
        total_retries = raw.get("total_retries")
        if (
            not isinstance(candidate, dict)
            or not candidate.get("availability_domain")
            or not token
        ):
            return None
        try:
            retry_index = int(retry_index)
            total_retries = int(total_retries)
        except (TypeError, ValueError):
            return None
        if retry_index < 0 or total_retries < 1 or retry_index >= total_retries:
            return None
        return {
            "candidate": Candidate(
                candidate["availability_domain"],
                candidate.get("fault_domain"),
            ),
            "opc_retry_token": str(token),
            "retry_index": retry_index,
            "total_retries": total_retries,
            "source": str(raw.get("source") or "direct"),
        }

    def _clear_retry_context(self) -> None:
        if self.runtime.get("pending_retry") is not None:
            self.runtime["pending_retry"] = None
            self.save_runtime()

    def _capacity_report_due(self) -> bool:
        backoff_until = self._runtime_time("capacity_report_backoff_until")
        return backoff_until is None or self.now() >= backoff_until

    def capacity_candidates(self, availability_domain: str) -> Optional[list[Candidate]]:
        """Return available capacity-report candidates, or None if unavailable."""

        if not self.capacity_report_enabled or not self._capacity_report_due():
            return None

        report_started = self.now()
        self._set_runtime_time("last_capacity_report", report_started)
        self.save_runtime()
        LOG.info("Revisión de capacidad en %s.", availability_domain)
        try:
            details = oci.core.models.CreateComputeCapacityReportDetails(
                compartment_id=self.tenancy_id,
                availability_domain=availability_domain,
                shape_availabilities=[
                    oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                        instance_shape=self.settings.shape,
                        instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                            ocpus=self.settings.ocpus,
                            memory_in_gbs=self.settings.memory_gbs,
                        ),
                    )
                ],
            )
            report = self.compute.create_compute_capacity_report(details).data
        except Exception as exc:
            if is_rate_limited(exc):
                self.activate_rate_limit(exc, "capacity_report")
                self._record_capacity_result(
                    "rate_limited",
                    availability_domain,
                    now=self.now(),
                )
                return []

            failure_count = self._runtime_int("capacity_report_failures", 0) + 1
            self._record_non_429_response()
            if is_capacity_endpoint_permanent_error(exc):
                self.capacity_report_enabled = False
                reason = compact_error(exc)
                self.runtime["capacity_report_enabled"] = False
                self.runtime["capacity_report_disabled_reason"] = reason
                self._set_runtime_time("capacity_report_backoff_until", None)
                self.save_runtime()
                self._record_capacity_result(
                    "disabled",
                    availability_domain,
                    now=self.now(),
                )
                LOG.warning(
                    "Capacity report desactivado durante esta ejecución: endpoint no "
                    "autorizado, no soportado o no disponible para la cuenta (%s).",
                    reason,
                )
                return None

            # 5xx, timeout, network and unknown errors are temporary. Keep the
            # endpoint enabled and try it again after an independent backoff.
            self.runtime["capacity_report_failures"] = failure_count
            retry_at = self.now() + self._capacity_report_delay(failure_count)
            self._set_runtime_time("capacity_report_backoff_until", retry_at)
            self.save_runtime()
            self._record_capacity_result(
                "temporary_error",
                availability_domain,
                now=self.now(),
            )
            LOG.warning(
                "Capacity report temporalmente no disponible; lo volveré a probar "
                "después de %.0f s y continuaré con intentos directos: %s",
                max(0.0, retry_at - self.now()),
                compact_error(exc),
            )
            return None

        self._record_non_429_response()
        self.runtime["capacity_report_failures"] = 0
        self._set_runtime_time("capacity_report_backoff_until", None)
        self.runtime["capacity_report_enabled"] = True

        candidates: list[Candidate] = []
        for availability in attr(report, "shape_availabilities", []) or []:
            status = str(attr(availability, "availability_status", "")).upper()
            count = attr(availability, "available_count", None)
            try:
                count_is_positive = count is None or int(count) > 0
            except (TypeError, ValueError):
                count_is_positive = False
            if status == "AVAILABLE" and count_is_positive:
                candidates.append(
                    Candidate(
                        availability_domain,
                        attr(availability, "fault_domain", None),
                    )
                )

        unique: list[Candidate] = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        self._record_capacity_result(
            "available" if unique else "empty",
            availability_domain,
            now=self.now(),
        )
        if unique:
            LOG.info(
                "Capacity report con %d candidato(s) disponible(s) en %s; "
                "conservaré los no elegidos para ciclos posteriores.",
                len(unique),
                availability_domain,
            )
            return unique

        statuses = [
            f"{attr(item, 'fault_domain', 'auto')}="
            f"{attr(item, 'availability_status', 'UNKNOWN')}"
            for item in (attr(report, "shape_availabilities", []) or [])
        ]
        LOG.info(
            "Sin capacidad reportada en %s (%s).",
            availability_domain,
            ", ".join(statuses) or "sin detalle",
        )
        return []

    def candidates(self, max_capacity_reports: Optional[int] = None) -> list[Candidate]:
        """Collect reports without ever expanding a fallback into a burst."""

        if self.capacity_report_enabled:
            report_count = 0
            for availability_domain in self.availability_domains:
                if (
                    max_capacity_reports is not None
                    and report_count >= max_capacity_reports
                ):
                    break
                if not self._capacity_report_due():
                    break
                reported = self.capacity_candidates(availability_domain)
                report_count += 1
                if reported:
                    self._enqueue_candidates(reported)
                if self.cooldown_active():
                    break
        return self._pending_candidates()

    def launch_details(self, candidate: Candidate) -> Any:
        source_details = oci.core.models.InstanceSourceViaImageDetails(
            image_id=attr(self.image, "id"),
            boot_volume_size_in_gbs=self.settings.boot_volume_gbs,
        )
        create_vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id=attr(self.subnet, "id"),
            assign_public_ip=self.settings.assign_public_ip,
        )
        kwargs: dict[str, Any] = {
            "availability_domain": candidate.availability_domain,
            "compartment_id": self.settings.compartment_id,
            "display_name": self.settings.instance_name,
            "shape": self.settings.shape,
            "shape_config": oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=self.settings.ocpus,
                memory_in_gbs=self.settings.memory_gbs,
            ),
            "source_details": source_details,
            "create_vnic_details": create_vnic_details,
            "metadata": {"ssh_authorized_keys": self.ssh_key},
            "freeform_tags": {
                "managed_by": "oci-a1-claimer",
                "always_free_target": "true",
            },
        }
        if candidate.fault_domain:
            kwargs["fault_domain"] = candidate.fault_domain
        return oci.core.models.LaunchInstanceDetails(**kwargs)

    def launch_candidate(
        self,
        candidate: Candidate,
        *,
        source: str = "direct",
        max_retries: Optional[int] = None,
    ) -> tuple[Optional[Any], str]:
        """Make exactly one HTTP launch call for this cycle.

        A timeout/5xx retry is persisted as pending_retry and resumed by a
        later cycle with the same candidate and opc_retry_token. This keeps
        retry safety without turning one cycle into a burst.
        """

        retry_context = self._retry_context()
        if retry_context and retry_context["candidate"] == candidate:
            retry_token = retry_context["opc_retry_token"]
            retry_index = retry_context["retry_index"]
            total_retries = retry_context["total_retries"]
            source = "retry"
        else:
            retry_token = uuid.uuid4().hex
            retry_index = 0
            total_retries = max_retries or self.settings.create_retries
            total_retries = max(1, min(total_retries, self.settings.create_retries))

        details = self.launch_details(candidate)
        if not self._begin_real_attempt(candidate, retry_index, total_retries):
            if self.cooldown_active():
                return None, "rate_limited"
            return None, "stopped"

        try:
            response = self.compute.launch_instance(
                details,
                opc_retry_token=retry_token,
                retry_strategy=oci.retry.NoneRetryStrategy(),
            )
            self._record_non_429_response()
            self._clear_retry_context()
            self._record_launch_result(
                "created",
                candidate,
                retry_index=retry_index,
                source=source,
                now=self.now(),
            )
            self._schedule_next_attempt(self.now())
            return response.data, "created"
        except Exception as exc:
            if is_rate_limited(exc):
                # Do not call existing_instance after a 429. The global
                # cooldown blocks both read and write API calls.
                self._clear_retry_context()
                self.activate_rate_limit(exc, "launch_instance")
                self._record_launch_result(
                    "rate_limited",
                    candidate,
                    retry_index=retry_index,
                    source=source,
                    now=self.now(),
                )
                return None, "rate_limited"

            if is_capacity_error(exc):
                self._clear_retry_context()
                self._record_non_429_response()
                self._record_launch_result(
                    "out_of_capacity",
                    candidate,
                    retry_index=retry_index,
                    source=source,
                    now=self.now(),
                )
                self._schedule_next_attempt(self.now())
                return None, "out_of_capacity"

            existing = None
            if is_ambiguous_error(exc):
                # OCI may have accepted a request even when the client
                # received a timeout/5xx. This is the only post-error
                # duplicate check before scheduling the same request again.
                try:
                    existing = self.existing_instance()
                except RateLimitedError:
                    existing = None
                except (TemporaryCloudError, FatalCloudError) as check_exc:
                    LOG.warning(
                        "No pude comprobar la instancia tras un resultado ambiguo: %s",
                        check_exc,
                    )
            self._record_non_429_response()
            if existing:
                self._clear_retry_context()
                self._record_launch_result(
                    "created",
                    candidate,
                    retry_index=retry_index,
                    source=source,
                    now=self.now(),
                )
                self._schedule_next_attempt(self.now())
                LOG.info("La instancia apareció después de un resultado ambiguo.")
                return existing, "created"

            if not is_transient_error(exc):
                self._clear_retry_context()
                self._record_launch_result(
                    "fatal_error",
                    candidate,
                    retry_index=retry_index,
                    source=source,
                    now=self.now(),
                )
                self._schedule_next_attempt(self.now())
                LOG.error(
                    "Error fatal al crear; no volveré a repetir este request: %s",
                    compact_error(exc),
                )
                return None, "fatal_error"

            self._record_launch_result(
                "transient_error",
                candidate,
                retry_index=retry_index,
                source=source,
                now=self.now(),
            )
            LOG.warning(
                "Error transitorio al crear (%d/%d): %s",
                retry_index + 1,
                total_retries,
                compact_error(exc),
            )
            if retry_index + 1 >= total_retries:
                self._clear_retry_context()
                self._schedule_next_attempt(self.now())
                return None, "transient_error"

            if self.cooldown_active():
                return None, "rate_limited"

            now = self.now()
            retry_at = now + self._retry_delay(retry_index)
            last_attempt = self._runtime_time("last_real_attempt") or now
            retry_at = max(
                retry_at,
                last_attempt + self.settings.min_launch_gap_seconds,
            )
            self.runtime["pending_retry"] = {
                "candidate": {
                    "availability_domain": candidate.availability_domain,
                    "fault_domain": candidate.fault_domain,
                },
                "opc_retry_token": retry_token,
                "retry_index": retry_index + 1,
                "total_retries": total_retries,
                "source": source if source != "retry" else "direct",
            }
            self._set_runtime_time("next_attempt_allowed", retry_at)
            self.save_runtime()
            LOG.info(
                "Reintentaré el mismo candidato en un ciclo posterior después de %.0f s "
                "con el mismo token de idempotencia.",
                max(0.0, retry_at - now),
            )
            return None, "transient_error"

    def _launch_allowed(self, *, capacity_hint: bool = False) -> bool:
        now = self.now()
        if self.stop_event.is_set() or self.cooldown_active(now):
            return False
        if self.settings.max_attempts and self.attempts >= self.settings.max_attempts:
            return False
        last_attempt = self._runtime_time("last_real_attempt")
        if last_attempt is not None and now - last_attempt < self.settings.min_launch_gap_seconds:
            return False
        if not capacity_hint:
            next_allowed = self._runtime_time("next_attempt_allowed")
            if next_allowed is not None and now < next_allowed:
                return False
        return True

    def _prepare_and_launch(
        self,
        candidate: Candidate,
        *,
        source: str,
        once: bool,
    ) -> bool:
        if not self._launch_allowed(capacity_hint=source == "capacity"):
            return False
        try:
            existing = self.existing_instance()
        except RateLimitedError:
            return False
        except TemporaryCloudError as exc:
            LOG.warning("%s", exc)
            return False
        if existing:
            if not self.settings.state_file.exists():
                self.finish(existing)
            return True

        # A capacity candidate is removed only when it is actually selected.
        # The remaining report candidates stay in runtime.json for later cycles.
        if source == "capacity":
            self._remove_pending_candidate(candidate)
        elif source == "direct":
            candidate = self._consume_direct_candidate(candidate.availability_domain)

        instance, result = self.launch_candidate(
            candidate,
            source=source,
            max_retries=1 if once else None,
        )
        if instance is not None:
            self.finish(instance)
            return True
        if result == "fatal_error":
            raise FatalCloudError("OCI rechazó la creación con un error fatal.")
        if result == "stopped":
            return False
        return False

    def run_cycle(self, *, once: bool = False) -> bool:
        cycle_now = self.now()
        self._set_runtime_time("last_cycle", cycle_now)
        self.save_runtime()

        if self.cooldown_active(cycle_now):
            LOG.info(
                "Cooldown global activo durante %.0f s; no haré comprobaciones ni "
                "solicitudes repetitivas.",
                self._cooldown_remaining(cycle_now),
            )
            return False

        retry_context = self._retry_context()
        if retry_context:
            retry_at = self._runtime_time("next_attempt_allowed")
            if retry_at is None or cycle_now >= retry_at:
                return self._prepare_and_launch(
                    retry_context["candidate"],
                    source="retry",
                    once=once,
                )
            LOG.info(
                "Reintento del mismo candidato pendiente durante %.0f s; no buscaré "
                "candidatos nuevos.",
                max(0.0, retry_at - cycle_now),
            )
            return False

        if not once and self.should_check_existing():
            try:
                existing = self.existing_instance()
            except RateLimitedError:
                return False
            except TemporaryCloudError as exc:
                LOG.warning("%s", exc)
                return False
            if existing:
                if not self.settings.state_file.exists():
                    self.finish(existing)
                return True

        pending = self.candidates(max_capacity_reports=1 if once else None)
        if self.cooldown_active():
            return False
        if pending:
            return self._prepare_and_launch(
                pending[0],
                source="capacity",
                once=once,
            )

        if not self._launch_allowed(capacity_hint=False):
            return False
        if not self.availability_domains:
            raise ConfigurationError("No hay availability domains validados.")
        direct_candidate = self.fallback_candidates(self.availability_domains[0])[0]
        return self._prepare_and_launch(
            direct_candidate,
            source="direct",
            once=once,
        )

    def wait_for_instance(self, instance: Any) -> Any:
        instance_id = attr(instance, "id")
        if not instance_id:
            return instance
        deadline = self.monotonic() + self.settings.instance_wait_seconds
        latest = instance
        while self.monotonic() < deadline and not self.stop_event.is_set():
            try:
                latest = self.compute.get_instance(instance_id).data
            except Exception as exc:
                LOG.warning(
                    "No pude actualizar el estado de la instancia: %s",
                    compact_error(exc),
                )
                self._sleeper(10)
                continue
            state = attr(latest, "lifecycle_state", "UNKNOWN")
            if state in {"RUNNING", "STOPPED", "TERMINATED", "FAILED"}:
                return latest
            self._sleeper(10)
        return latest

    def public_ip(self, instance_id: str) -> Optional[str]:
        try:
            attachments = self.all_results(
                self.compute.list_vnic_attachments,
                compartment_id=self.settings.compartment_id,
                instance_id=instance_id,
            )
            for attachment in attachments:
                vnic_id = attr(attachment, "vnic_id")
                if not vnic_id:
                    continue
                vnic = self.network.get_vnic(vnic_id).data
                address = attr(vnic, "public_ip")
                if address:
                    return address
        except Exception as exc:
            LOG.warning("No pude obtener la IP pública todavía: %s", compact_error(exc))
        return None

    def instance_summary(self, instance: Any) -> dict[str, Any]:
        latest = self.wait_for_instance(instance)
        instance_id = attr(latest, "id", attr(instance, "id", ""))
        ip = self.public_ip(instance_id) if instance_id else None
        summary = {
            "id": instance_id,
            "display_name": attr(latest, "display_name", self.settings.instance_name),
            "lifecycle_state": attr(latest, "lifecycle_state", "UNKNOWN"),
            "region": self.settings.region,
            "availability_domain": attr(latest, "availability_domain", ""),
            "shape": attr(latest, "shape", self.settings.shape),
            "ocpus": self.settings.ocpus,
            "memory_gbs": self.settings.memory_gbs,
            "public_ip": ip,
            "created_at": utc_timestamp(self.now()),
        }
        return summary

    def save_state(self, summary: dict[str, Any]) -> None:
        path = self.settings.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def notify_telegram(self, summary: dict[str, Any]) -> bool:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            LOG.info(
                "Telegram no configurado; conservaré los datos en %s.",
                self.settings.state_file,
            )
            return False
        ip = summary.get("public_ip") or "todavía no asignada"
        text = (
            "OCI Always Free creada ✅\n"
            f"Nombre: {summary.get('display_name')}\n"
            f"Región: {summary.get('region')}\n"
            f"Estado: {summary.get('lifecycle_state')}\n"
            f"OCPU/RAM: {summary.get('ocpus')}/{summary.get('memory_gbs')} GB\n"
            f"IP pública: {ip}\n"
            f"OCID: {summary.get('id')}"
        )
        if summary.get("public_ip"):
            text += f"\nSSH: ssh {self.settings.ssh_user}@{summary['public_ip']}"
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        body = urllib.parse.urlencode(
            {"chat_id": self.settings.telegram_chat_id, "text": text}
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(str(result)[:300])
            LOG.info("Notificación de Telegram enviada.")
            return True
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            LOG.warning("La instancia se creó, pero Telegram falló: %s", exc)
            return False

    def finish(self, instance: Any) -> bool:
        summary = self.instance_summary(instance)
        self.save_state(summary)
        LOG.info("Instancia lista: %s", json.dumps(summary, ensure_ascii=False))
        self.notify_telegram(summary)
        return True

    def discover_output(self) -> dict[str, Any]:
        return {
            "region": self.settings.region,
            "availability_domains": self.availability_domains,
            "image": {
                "id": attr(self.image, "id"),
                "display_name": attr(self.image, "display_name"),
                "operating_system": attr(self.image, "operating_system"),
                "operating_system_version": attr(self.image, "operating_system_version"),
            },
            "subnet": {
                "id": attr(self.subnet, "id"),
                "display_name": attr(self.subnet, "display_name"),
                "vcn_id": attr(self.subnet, "vcn_id"),
                "prohibit_public_ip_on_vnic": attr(
                    self.subnet, "prohibit_public_ip_on_vnic"
                ),
            },
            "target": {
                "shape": self.settings.shape,
                "ocpus": self.settings.ocpus,
                "memory_gbs": self.settings.memory_gbs,
                "instance_name": self.settings.instance_name,
            },
        }

    def _check_delay(self) -> float:
        low = max(
            15,
            self.settings.interval_seconds - self.settings.jitter_seconds,
        )
        high = self.settings.interval_seconds + self.settings.jitter_seconds
        return self._random_between(low, max(low, high))

    def run(self, once: bool) -> int:
        """Run after main has completed the one-time full validation."""

        try:
            try:
                existing = self.existing_instance()
            except RateLimitedError:
                existing = None
            except TemporaryCloudError as exc:
                LOG.warning("%s", exc)
                existing = None
            if existing:
                LOG.info(
                    "Ya existe %s; no crearé otra instancia.",
                    self.settings.instance_name,
                )
                if not self.settings.state_file.exists():
                    self.finish(existing)
                return 0

            if once:
                self.run_cycle(once=True)
                return 0

            while not self.stop_event.is_set():
                try:
                    if self.run_cycle():
                        LOG.info(
                            "Trabajo terminado; no volveré a intentar crear otra instancia."
                        )
                        return 0
                except RateLimitedError:
                    pass
                except TemporaryCloudError as exc:
                    LOG.warning("%s", exc)
                except FatalCloudError as exc:
                    LOG.error("Error no reintentable: %s", exc)
                    return 2
                except ConfigurationError as exc:
                    LOG.error("Configuración inválida: %s", exc)
                    return 2

                if self.settings.max_attempts and self.attempts >= self.settings.max_attempts:
                    LOG.info("Se alcanzó el límite de intentos reales; terminando.")
                    return 0
                delay = self._check_delay()
                LOG.info("Siguiente comprobación en %.0f segundos.", delay)
                self._sleeper(delay)
        except FatalCloudError as exc:
            LOG.error("Error no reintentable: %s", exc)
            return 2
        return 0


@contextmanager
def process_lock(path: Path) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigurationError(
                f"Ya hay otra instancia del script usando {path}."
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Revisa capacidad de OCI y crea una única VM.Standard.A1.Flex."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="valida, comprueba una vez y permite como máximo una solicitud real",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="valida credenciales, imagen y subred sin lanzar una instancia",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="muestra los AD, imagen y subred detectados y termina",
    )
    parser.add_argument(
        "--no-capacity-report",
        action="store_true",
        help="omite el endpoint de capacity report y hace intentos directos espaciados",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=None,
        help="sobrescribe OCI_LOG_LEVEL",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.log_level:
            settings = dataclasses.replace(settings, log_level=args.log_level)
        if args.no_capacity_report:
            settings = dataclasses.replace(settings, capacity_report=False)
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        claimer = Claimer(settings)
        with process_lock(settings.lock_file):
            claimer.install_signal_handlers()
            # Full credential/image/network/domain validation happens once.
            claimer.validate()
            if args.discover:
                print(json.dumps(claimer.discover_output(), indent=2, ensure_ascii=False))
                return 0
            if args.validate_only:
                LOG.info("Validación terminada correctamente; no se creó ninguna instancia.")
                return 0
            return claimer.run(once=args.once)
    except (ConfigurationError, FatalCloudError) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
