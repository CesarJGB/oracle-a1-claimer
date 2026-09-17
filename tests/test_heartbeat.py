import json
import os
import pathlib
import shlex
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oci-a1-heartbeat.sh"


class HeartbeatTest(unittest.TestCase):
    def make_command(self, directory, name, body):
        path = directory / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    @staticmethod
    def timestamp(offset=0):
        return (
            datetime.fromtimestamp(time.time() + offset, tz=timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def run_heartbeat(
        self,
        active=True,
        created=False,
        runtime=None,
        logs=None,
        journal_marker=None,
        extra_env=None,
        process_env=None,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = pathlib.Path(temporary.name)
        commands = base / "bin"
        commands.mkdir()
        env_file = base / "claimer.env"
        state_file = base / "instance.json"
        runtime_file = base / "runtime.json"
        capture_file = base / "telegram.txt"

        env_lines = [
            "OCI_IMAGE_OS=Canonical Ubuntu",
            "TELEGRAM_BOT_TOKEN=test-token",
            "TELEGRAM_CHAT_ID=123456",
        ]
        if extra_env:
            for key, value in extra_env.items():
                env_lines.append(f"{key}={value}")
        env_lines.append("")
        env_file.write_text("\n".join(env_lines), encoding="utf-8")
        if created:
            state_file.write_text("{}\n", encoding="utf-8")
        if runtime == "corrupt":
            runtime_file.write_text("{not valid json", encoding="utf-8")
        elif runtime is not None:
            runtime_file.write_text(json.dumps(runtime), encoding="utf-8")

        state = "active" if active else "inactive"
        self.make_command(
            commands,
            "systemctl",
            f"""\
if [[ "$1" == "is-active" ]]; then
  echo "{state}"
  [[ "{state}" == "active" ]]
elif [[ "$1" == "show" ]]; then
  echo "Fri 2026-09-11 22:02:55 CEST"
fi
""",
        )
        log_lines = logs or []
        log_body = "exit 0\n"
        if journal_marker is not None:
            log_body = f'printf called > {shlex.quote(str(journal_marker))}\n' + log_body
        if log_lines:
            log_body = (
                "printf '%s\\n' "
                + " ".join(shlex.quote(line) for line in log_lines)
                + "\n"
                + log_body
            )
        self.make_command(commands, "journalctl", log_body)
        self.make_command(
            commands,
            "curl",
            "printf '%s\\n' \"$@\" > \"${HEARTBEAT_CAPTURE_FILE}\"\n",
        )

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{commands}:{environment['PATH']}",
                "OCI_HEARTBEAT_ENV_FILE": str(env_file),
                "OCI_HEARTBEAT_STATE_FILE": str(state_file),
                "OCI_HEARTBEAT_RUNTIME_FILE": str(runtime_file),
                "OCI_HEARTBEAT_TELEGRAM_API_BASE": "https://telegram.invalid",
                "HEARTBEAT_CAPTURE_FILE": str(capture_file),
            }
        )
        if process_env:
            environment.update(process_env)
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        captured = capture_file.read_text(encoding="utf-8") if capture_file.exists() else ""
        return result, captured

    def limited_runtime(self):
        events = []
        for _index in range(40):
            events.append(
                {"timestamp": self.timestamp(-30), "type": "capacity_report", "result": "empty"}
            )
        for _index in range(10):
            events.append(
                {"timestamp": self.timestamp(-20), "type": "launch", "result": "out_of_capacity"}
            )
        for _index in range(6):
            events.append(
                {"timestamp": self.timestamp(-10), "type": "launch", "result": "rate_limited"}
            )
        return {
            "format_version": 1,
            "service_started_at": self.timestamp(-(20 * 3600 + 3 * 60)),
            "next_attempt_allowed": self.timestamp(600),
            "cooldown_until": self.timestamp(300),
            "consecutive_429": 1,
            "recent_events": events,
        }

    def test_structured_active_message_is_exact_and_does_not_read_journal(self):
        marker = pathlib.Path(tempfile.mktemp(prefix="heartbeat-journal-"))
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        result, captured = self.run_heartbeat(
            active=True,
            runtime=self.limited_runtime(),
            journal_marker=marker,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("🟢 Oracle A1 Claimer funcionando", captured)
        self.assertIn("⏱ Activo: 20 h 3 min", captured)
        self.assertIn("📍 Monterrey · 2 OCPU · 12 GB", captured)
        self.assertIn("🔎 Revisiones de capacidad: 40", captured)
        self.assertIn("🚀 Solicitudes reales de creación: 16", captured)
        self.assertIn("📭 Sin capacidad: 10", captured)
        self.assertIn("⚠️ Limitadas por Oracle: 6 (37.5 %)", captured)
        self.assertIn("❌ Otros errores: 0", captured)
        self.assertIn("🟠 API limitada: ritmo reducido automáticamente", captured)
        self.assertRegex(captured, r"Próximo intento real: [0-9]+:[0-9]{2} [ap]\. m\.")
        self.assertFalse(marker.exists())

    def test_structured_stable_message(self):
        runtime = self.limited_runtime()
        runtime["recent_events"] = [
            {"timestamp": self.timestamp(-4000), "type": "launch", "result": "rate_limited"}
        ]
        runtime["cooldown_until"] = self.timestamp(-10)
        runtime["consecutive_429"] = 0

        result, captured = self.run_heartbeat(runtime=runtime)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("🚀 Solicitudes reales de creación: 0", captured)
        self.assertIn("⚠️ Limitadas por Oracle: 0 (0.0 %)", captured)
        self.assertIn("🟢 API estable", captured)

    def test_corrupt_runtime_falls_back_to_logs(self):
        logs = [
            "Revisión de capacidad en AD-1.",
            "Revisión de capacidad en AD-1.",
            "Solicitud real de creación #1: AD-1 / FD1.",
            "Resultado de solicitud real: out_of_capacity (AD-1 / FD1; reintento 1).",
            "Solicitud real de creación #2: AD-1 / FD2.",
            "Resultado de solicitud real: rate_limited (AD-1 / FD2; reintento 1).",
        ]
        result, captured = self.run_heartbeat(
            runtime="corrupt",
            logs=logs,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("🔎 Revisiones de capacidad: 2", captured)
        self.assertIn("🚀 Solicitudes reales de creación: 2", captured)
        self.assertIn("📭 Sin capacidad: 1", captured)
        self.assertIn("⚠️ Limitadas por Oracle: 1 (50.0 %)", captured)
        self.assertIn("❌ Otros errores: 0", captured)

    def test_inactive_service_sends_separate_alert(self):
        result, captured = self.run_heartbeat(active=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("🔴 Oracle A1 Claimer detenido", captured)
        self.assertIn("Estado detectado: inactive", captured)
        self.assertNotIn("OCID", captured)

    def test_created_instance_skips_heartbeat(self):
        result, captured = self.run_heartbeat(active=False, created=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured, "")

    def test_image_os_with_spaces_is_never_executed(self):
        result, _captured = self.run_heartbeat(runtime=self.limited_runtime())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("command not found", result.stderr)

    def test_heartbeat_includes_adaptive_interval_and_fresh_candidates(self):
        runtime = self.limited_runtime()
        runtime["adaptive_direct_interval_seconds"] = 195
        runtime["pending_candidates"] = [
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-2",
                "observed_at": self.timestamp(-30),
                "available_count": 1,
            }
        ]
        result, captured = self.run_heartbeat(runtime=runtime)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("⏱ Intervalo directo actual: 195 s", captured)
        self.assertIn("⚡ Candidatos capacity frescos: 1", captured)

    def test_candidate_ttl_default_180s(self):
        runtime = self.limited_runtime()
        runtime["pending_candidates"] = [
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-1",
                "observed_at": self.timestamp(-150),
            },
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-2",
                "observed_at": self.timestamp(-210),
            },
        ]
        result, captured = self.run_heartbeat(runtime=runtime)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("⚡ Candidatos capacity frescos: 1", captured)

    def test_candidate_ttl_custom_greater_value(self):
        runtime = self.limited_runtime()
        runtime["pending_candidates"] = [
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-1",
                "observed_at": self.timestamp(-250),
            },
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-2",
                "observed_at": self.timestamp(-350),
            },
        ]
        result, captured = self.run_heartbeat(
            runtime=runtime,
            extra_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": "300"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("⚡ Candidatos capacity frescos: 1", captured)

    def test_candidate_ttl_custom_smaller_value(self):
        runtime = self.limited_runtime()
        # Con TTL=120, un candidato de hace 150 s ya no es fresco
        runtime["pending_candidates"] = [
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-1",
                "observed_at": self.timestamp(-150),
            },
        ]
        result, captured = self.run_heartbeat(
            runtime=runtime,
            extra_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": "120"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("⚡ Candidatos capacity frescos", captured)

        # Con uno de 60 s y uno de 150 s, solo se cuenta el de 60 s
        runtime["pending_candidates"].append(
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-2",
                "observed_at": self.timestamp(-60),
            }
        )
        result2, captured2 = self.run_heartbeat(
            runtime=runtime,
            extra_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": "120"},
        )
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertIn("⚡ Candidatos capacity frescos: 1", captured2)

    def test_candidate_ttl_empty_or_missing(self):
        runtime = self.limited_runtime()
        runtime["pending_candidates"] = [
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-1",
                "observed_at": self.timestamp(-150),
            },
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-2",
                "observed_at": self.timestamp(-210),
            },
        ]
        # Variable vacía en claimer.env -> usa default 180s
        result_empty_file, captured_empty_file = self.run_heartbeat(
            runtime=runtime,
            extra_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": ""},
        )
        self.assertEqual(result_empty_file.returncode, 0, result_empty_file.stderr)
        self.assertIn("⚡ Candidatos capacity frescos: 1", captured_empty_file)

        # Variable vacía en process_env -> usa default 180s
        result_empty_proc, captured_empty_proc = self.run_heartbeat(
            runtime=runtime,
            process_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": ""},
        )
        self.assertEqual(result_empty_proc.returncode, 0, result_empty_proc.stderr)
        self.assertIn("⚡ Candidatos capacity frescos: 1", captured_empty_proc)

    def test_candidate_ttl_invalid_values_fallback_safely(self):
        runtime = self.limited_runtime()
        runtime["pending_candidates"] = [
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-1",
                "observed_at": self.timestamp(-150),
            },
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-2",
                "observed_at": self.timestamp(-210),
            },
        ]
        invalid_cases = ["invalid_text", "0", "-60", "  "]
        for invalid_val in invalid_cases:
            with self.subTest(invalid_val=invalid_val):
                result, captured = self.run_heartbeat(
                    runtime=runtime,
                    extra_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": invalid_val},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("⚡ Candidatos capacity frescos: 1", captured)

    def test_candidate_ttl_process_env_overrides_env_file(self):
        runtime = self.limited_runtime()
        # Candidato de hace 250 s
        runtime["pending_candidates"] = [
            {
                "availability_domain": "AD-1",
                "fault_domain": "FAULT-DOMAIN-1",
                "observed_at": self.timestamp(-250),
            },
        ]
        # env_file dice 120s (no fresco), pero process_env dice 300s (fresco)
        result, captured = self.run_heartbeat(
            runtime=runtime,
            extra_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": "120"},
            process_env={"OCI_CAPACITY_CANDIDATE_TTL_SECONDS": "300"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("⚡ Candidatos capacity frescos: 1", captured)


if __name__ == "__main__":
    unittest.main()
