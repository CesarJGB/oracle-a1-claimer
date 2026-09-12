#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="${OCI_HEARTBEAT_ENV_FILE:-/etc/oci-a1-claimer/claimer.env}"
STATE_FILE="${OCI_HEARTBEAT_STATE_FILE:-/var/lib/oci-a1-claimer/instance.json}"
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

# No se carga claimer.env con `source`: valores como
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

for command_name in systemctl journalctl curl; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Falta el comando ${command_name}." >&2
    exit 1
  fi
done

service_state="$(systemctl is-active "${CLAIMER_UNIT}" 2>/dev/null || true)"
service_since="$(
  systemctl show "${CLAIMER_UNIT}" --property=ActiveEnterTimestamp --value \
    2>/dev/null || true
)"

if [[ "${service_state}" == "active" ]]; then
  logs_last_hour="$(
    journalctl --unit="${CLAIMER_UNIT}" --since="1 hour ago" \
      --no-pager --output=cat 2>/dev/null || true
  )"

  count_log_lines() {
    local pattern="$1"
    grep -Ec "${pattern}" <<<"${logs_last_hour}" || true
  }

  checks="$(count_log_lines 'Sin capacidad reportada')"
  direct_attempts="$(count_log_lines 'Intento [0-9]+:')"
  no_capacity="$(count_log_lines 'Sin capacidad en')"
  rate_limits="$(count_log_lines 'HTTP 429')"

  [[ -n "${service_since}" ]] || service_since="fecha no disponible"

  message="🟢 Oracle A1 Claimer activo"
  message+=$'\n\n⏱ Servicio activo desde:\n'
  message+="${service_since}"
  message+=$'\n\n🔎 Comprobaciones última hora:\n'
  message+="${checks}"
  message+=$'\n🚀 Intentos directos última hora:\n'
  message+="${direct_attempts}"
  message+=$'\n📫 Intentos sin capacidad: '
  message+="${no_capacity}"
  message+=$'\n⚠️ Rate limits 429: '
  message+="${rate_limits}"
  message+=$'\n\n🎯 Objetivo: 2 OCPU / 12 GB'
  message+=$'\n📍 Región: mx-monterrey-1'
  message+=$'\n\nEstado: buscando capacidad.'
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
