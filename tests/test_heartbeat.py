import os
import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oci-a1-heartbeat.sh"


class HeartbeatTest(unittest.TestCase):
    def make_command(self, directory, name, body):
        path = directory / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_heartbeat(self, active=True, created=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = pathlib.Path(temporary.name)
        commands = base / "bin"
        commands.mkdir()
        env_file = base / "claimer.env"
        state_file = base / "instance.json"
        capture_file = base / "telegram.txt"

        env_file.write_text(
            textwrap.dedent(
                """\
                OCI_IMAGE_OS=Canonical Ubuntu
                TELEGRAM_BOT_TOKEN=test-token
                TELEGRAM_CHAT_ID=123456
                """
            ),
            encoding="utf-8",
        )
        if created:
            state_file.write_text("{}\n", encoding="utf-8")

        state = "active" if active else "inactive"
        self.make_command(
            commands,
            "systemctl",
            textwrap.dedent(
                f"""\
                if [[ "$1" == "is-active" ]]; then
                  echo "{state}"
                  [[ "{state}" == "active" ]]
                elif [[ "$1" == "show" ]]; then
                  echo "Fri 2026-09-11 22:02:55 CEST"
                fi
                """
            ),
        )
        self.make_command(
            commands,
            "journalctl",
            textwrap.dedent(
                """\
                printf '%s\n' \\
                  'Sin capacidad reportada en AD-1 (sin detalle).' \\
                  'Sin capacidad reportada en AD-1 (sin detalle).' \\
                  'Intento 1: AD-1 / automático' \\
                  'Sin capacidad en AD-1 / automático.' \\
                  'Intento 2: AD-1 / FAULT-DOMAIN-1' \\
                  'Error transitorio al crear (1/3): HTTP 429 / TooManyRequests: limited'
                """
            ),
        )
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
                "OCI_HEARTBEAT_TELEGRAM_API_BASE": "https://telegram.invalid",
                "HEARTBEAT_CAPTURE_FILE": str(capture_file),
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        captured = capture_file.read_text(encoding="utf-8") if capture_file.exists() else ""
        return result, captured

    def test_active_message_counts_last_hour(self):
        result, captured = self.run_heartbeat(active=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("🟢 Oracle A1 Claimer activo", captured)
        self.assertIn("Comprobaciones última hora:\n2", captured)
        self.assertIn("Intentos directos última hora:\n2", captured)
        self.assertIn("Intentos sin capacidad: 1", captured)
        self.assertIn("Rate limits 429: 1", captured)

    def test_inactive_service_sends_alert(self):
        result, captured = self.run_heartbeat(active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("🔴 Oracle A1 Claimer detenido", captured)
        self.assertIn("Estado detectado: inactive", captured)

    def test_created_instance_skips_heartbeat(self):
        result, captured = self.run_heartbeat(active=False, created=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured, "")


if __name__ == "__main__":
    unittest.main()
