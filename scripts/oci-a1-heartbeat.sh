#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${OCI_HEARTBEAT_ENV_FILE:-/etc/oci-a1-claimer/claimer.env}"
STATE_FILE="${OCI_HEARTBEAT_STATE_FILE:-/var/lib/oci-a1-claimer/instance.json}"
RUNTIME_FILE="${OCI_HEARTBEAT_RUNTIME_FILE:-/var/lib/oci-a1-claimer/runtime.json}"
CLAIMER_UNIT="${OCI_HEARTBEAT_CLAIMER_UNIT:-oci-a1-claimer.service}"
TELEGRAM_API_BASE="${OCI_HEARTBEAT_TELEGRAM_API_BASE:-https://api.telegram.org}"

# La notificación final del claimer sustituye al heartbeat cuando la instancia
# ya fue creada. Esto evita mensajes horarios duplicados después del éxito.
if [[ -f "${STATE_FILE}" ]]; then
  exit 0
fi

if [[ ! -r "${ENV_FILE}" ]]; then
  echo "No puedo leer ${ENV_FILE}." >&2
  exit 1
fi

# No se carga claimer.env con source: valores como
# OCI_IMAGE_OS=Canonical Ubuntu no son sintaxis válida de shell. Solo se leen
# de forma pasiva las dos variables que necesita este script.
read_setting() {
  local key="$1"
  awk -v wanted="${key}" '
    $0 ~ "^[[:space:]]*" wanted "[[:space:]]*=" {
      value = $0
      sub("^[[:space:]]*" wanted "[[:space:]]*=[[:space:]]*", "", value)
      sub("[[:space:]]+$", "", value)
      if ((value ~ /^\".*\"$/) || (value ~ /^\047.*\047$/)) {
        value = substr(value, 2, length(value) - 2)
      }
      result = value
    }
    END { if (result != "") print result }
  ' "${ENV_FILE}"
}

TELEGRAM_BOT_TOKEN="$(read_setting TELEGRAM_BOT_TOKEN)"
TELEGRAM_CHAT_ID="$(read_setting TELEGRAM_CHAT_ID)"

if [[ -z "${TELEGRAM_BOT_TOKEN}" || -z "${TELEGRAM_CHAT_ID}" ]]; then
  echo "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en ${ENV_FILE}." >&2
  exit 1
fi

for command_name in systemctl journalctl curl python3; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Falta el comando ${command_name}." >&2
    exit 1
  fi
done

service_state="$(systemctl is-active "${CLAIMER_UNIT}" 2>/dev/null || true)"

if [[ "${service_state}" == "active" ]]; then
  service_since="$(
    systemctl show "${CLAIMER_UNIT}" --property=ActiveEnterTimestamp --value \
      2>/dev/null || true
  )"
  logs_last_hour=""

  # Runtime JSON is authoritative. Only invoke journalctl during the
  # transition or after a damaged runtime file, preserving compatibility with
  # installations upgraded in place.
  if [[ ! -s "${RUNTIME_FILE}" ]] || ! python3 -c '
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as stream:
    value = json.load(stream)
if not isinstance(value, dict) or value.get("format_version") not in (1, 2):
    raise SystemExit(1)
' "${RUNTIME_FILE}" 2>/dev/null; then
    logs_last_hour="$(
      journalctl --unit="${CLAIMER_UNIT}" --since="1 hour ago" \
        --no-pager --output=short-iso 2>/dev/null || true
    )"
  fi

  message="$(
    python3 -c '
import json
import re
import sys
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

runtime_path = sys.argv[1]
service_since = sys.argv[2]
logs = sys.stdin.read()
now = time.time()
runtime = None

try:
    with open(runtime_path, "r", encoding="utf-8") as stream:
        candidate = json.load(stream)
    if isinstance(candidate, dict) and candidate.get("format_version") in (1, 2):
        runtime = candidate
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    runtime = None

def timestamp(value):
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

def system_timestamp(value):
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None

def duration(seconds):
    total_minutes = max(0, int(seconds // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days} d {hours} h {minutes} min"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"

def visible_time(value):
    moment = timestamp(value)
    if moment is None:
        return "no programado"
    zone = ZoneInfo("America/Monterrey") if ZoneInfo else timezone.utc
    local = datetime.fromtimestamp(moment, zone)
    hour = local.hour % 12 or 12
    suffix = "a. m." if local.hour < 12 else "p. m."
    return f"{hour}:{local.minute:02d} {suffix}"

def fallback_metrics(text):
    revisions = len(re.findall(r"Revisión de capacidad", text))
    checks = revisions or len(re.findall(r"Sin capacidad reportada", text))
    launch_lines = re.findall(
        r"Resultado de solicitud real:\s*(created|out_of_capacity|rate_limited|transient_error|fatal_error)",
        text,
    )
    if not launch_lines:
        direct_attempts = len(re.findall(r"Intento [0-9]+:", text))
        no_capacity = len(re.findall(r"Sin capacidad en", text))
        rate_limited = len(re.findall(r"HTTP 429", text))
        transient = len(re.findall(r"Error transitorio al crear", text))
        fatal = len(re.findall(r"Error fatal al crear", text))
        requests = direct_attempts
        no_capacity = min(no_capacity, requests)
        rate_limited = min(rate_limited, max(0, requests - no_capacity))
        other = min(
            transient + fatal,
            max(0, requests - no_capacity - rate_limited),
        )
        created = max(0, requests - no_capacity - rate_limited - other)
    else:
        requests = len(launch_lines)
        created = launch_lines.count("created")
        no_capacity = launch_lines.count("out_of_capacity")
        rate_limited = launch_lines.count("rate_limited")
        other = launch_lines.count("transient_error") + launch_lines.count("fatal_error")
    return checks, requests, created, no_capacity, rate_limited, other, None

if runtime is None:
    (
        checks,
        requests,
        created,
        no_capacity,
        rate_limited,
        other,
        next_allowed,
    ) = fallback_metrics(logs)
    started = system_timestamp(service_since)
    api_limited = rate_limited > 0
else:
    events = runtime.get("recent_events", [])
    cutoff = now - 3600
    recent = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        event_time = timestamp(event.get("timestamp"))
        if event_time is not None and cutoff <= event_time <= now:
            recent.append(event)
    capacity_events = [
        event for event in recent
        if event.get("type") == "capacity_report"
    ]
    launch_events = [
        event for event in recent
        if event.get("type") == "launch"
        and event.get("result") in {
            "created", "out_of_capacity", "rate_limited",
            "transient_error", "fatal_error",
        }
    ]
    checks = len(capacity_events)
    requests = len(launch_events)
    no_capacity = sum(event.get("result") == "out_of_capacity" for event in launch_events)
    rate_limited = sum(event.get("result") == "rate_limited" for event in launch_events)
    other = sum(
        event.get("result") in {"transient_error", "fatal_error"}
        for event in launch_events
    )
    next_allowed = runtime.get("next_attempt_allowed")
    started = timestamp(runtime.get("service_started_at")) or system_timestamp(service_since)
    cooldown = timestamp(runtime.get("cooldown_until"))
    scheduled = timestamp(next_allowed)
    if cooldown is not None and (scheduled is None or cooldown > scheduled):
        next_allowed = cooldown
    try:
        consecutive_429 = int(runtime.get("consecutive_429", 0) or 0)
    except (TypeError, ValueError):
        consecutive_429 = 0
    api_limited = (
        any(event.get("result") == "rate_limited" for event in recent)
        or (cooldown is not None and cooldown > now)
        or consecutive_429 > 0
    )

# Every completed launch_instance call has exactly one category.
if runtime is not None:
    created = sum(event.get("result") == "created" for event in launch_events)
other = requests - created - no_capacity - rate_limited
percentage = (100.0 * rate_limited / requests) if requests else 0.0

extra_status = []
if runtime is not None:
    adaptive_interval = runtime.get("adaptive_direct_interval_seconds")
    if adaptive_interval is not None:
        try:
            extra_status.append(f"⏱ Intervalo directo actual: {int(adaptive_interval)} s")
        except (TypeError, ValueError):
            pass
    pending_list = runtime.get("pending_candidates", [])
    if isinstance(pending_list, list):
        fresh_count = 0
        for cand in pending_list:
            if isinstance(cand, dict):
                obs = timestamp(cand.get("observed_at"))
                if obs is not None and (now - obs) <= 180:
                    fresh_count += 1
        if fresh_count > 0:
            extra_status.append(f"⚡ Candidatos capacity frescos: {fresh_count}")

extra_block = ("\n" + "\n".join(extra_status)) if extra_status else ""

active_for = duration(now - started) if started is not None else "tiempo no disponible"
api_line = (
    "🟠 API limitada: ritmo reducido automáticamente"
    if api_limited
    else "🟢 API estable"
)
message = (
    "🟢 Oracle A1 Claimer funcionando\n"
    f"⏱ Activo: {active_for}\n"
    "📍 Monterrey · 2 OCPU · 12 GB\n"
    "Últimos 60 minutos\n"
    f"🔎 Revisiones de capacidad: {checks}\n"
    f"🚀 Solicitudes reales de creación: {requests}\n"
    f"📭 Sin capacidad: {no_capacity}\n"
    f"⚠️ Limitadas por Oracle: {rate_limited} ({percentage:.1f} %)\n"
    f"❌ Otros errores: {other}\n"
    f"{api_line}"
    f"{extra_block}\n"
    "Estado: esperando capacidad; no necesitas hacer nada.\n"
    f"Próximo intento real: {visible_time(next_allowed)}"
)
print(message)
' "${RUNTIME_FILE}" "${service_since}" <<<"${logs_last_hour}"
  )"
else
  message="🔴 Oracle A1 Claimer detenido"
  message+=$'\n\nEl servicio '
  message+="${CLAIMER_UNIT}"
  message+=$' no está activo.'
  message+=$'\nEstado detectado: '
  message+="${service_state:-desconocido}"
  message+=$'\n\nRevisa el VPS con:'
  message+=$'\nsudo systemctl status oci-a1-claimer'
fi

curl --fail --silent --show-error --max-time 20 \
  --request POST \
  "${TELEGRAM_API_BASE}/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${message}" \
  >/dev/null
