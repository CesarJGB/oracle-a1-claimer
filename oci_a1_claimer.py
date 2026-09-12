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
import fcntl
import json
import logging
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
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import oci  # type: ignore
except ImportError:  # Keep --help and local unit tests usable without the SDK.
    oci = None  # type: ignore


LOG = logging.getLogger("oci-a1-claimer")


class ConfigurationError(RuntimeError):
    """The local configuration is incomplete or inconsistent."""


class FatalCloudError(RuntimeError):
    """OCI returned an error that should not be retried automatically."""


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


def compact_error(exc: BaseException) -> str:
    """Return a useful, non-secret error summary for logs and Telegram."""

    status = attr(exc, "status", "?")
    code = attr(exc, "code", type(exc).__name__)
    message = attr(exc, "message", str(exc)) or str(exc)
    message = " ".join(str(message).split())
    return f"HTTP {status} / {code}: {message[:350]}"


def is_capacity_error(exc: BaseException) -> bool:
    text = " ".join(
        str(part or "")
        for part in (attr(exc, "code", ""), attr(exc, "message", ""), str(exc))
    ).lower()
    markers = (
        "out of host capacity",
        "out_of_host_capacity",
        "outofhostcapacity",
        "capacity is unavailable",
        "capacity unavailable",
        "not enough capacity",
    )
    return any(marker in text for marker in markers)


def is_transient_error(exc: BaseException) -> bool:
    status = attr(exc, "status", None)
    code = str(attr(exc, "code", "")).lower()
    if status in {408, 429, 500, 502, 503, 504}:
        return True
    if code in {"internalerror", "serviceunavailable", "toomanyrequests", "timeout"}:
        return True
    return type(exc).__name__ in {"RequestException", "TimeoutError", "ConnectionError"}


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
    create_retries: int
    create_retry_delay: int
    instance_wait_seconds: int
    max_attempts: int
    state_file: Path
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
            direct_fallback_every=parse_int("OCI_DIRECT_FALLBACK_EVERY", 10, 1),
            interval_seconds=parse_int("OCI_INTERVAL_SECONDS", 60, 15),
            jitter_seconds=parse_int("OCI_JITTER_SECONDS", 15, 0),
            create_retries=parse_int("OCI_CREATE_RETRIES", 3, 1),
            create_retry_delay=parse_int("OCI_CREATE_RETRY_DELAY", 10, 1),
            instance_wait_seconds=parse_int("OCI_INSTANCE_WAIT_SECONDS", 300, 30),
            max_attempts=parse_int("OCI_MAX_ATTEMPTS", 0, 0),
            state_file=state_file,
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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = threading.Event()
        self.identity: Any = None
        self.compute: Any = None
        self.network: Any = None
        self.oci_config: dict[str, Any] = {}
        self.tenancy_id: Optional[str] = None
        self.availability_domains: list[str] = []
        self.image: Any = None
        self.subnet: Any = None
        self.ssh_key: Optional[str] = None
        self.capacity_report_enabled = settings.capacity_report
        self.report_empty_cycles = 0
        self.attempts = 0

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
        self.availability_domains = [attr(item, "name") for item in domains if attr(item, "name")]
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

        pattern = re.compile(self.settings.image_name_regex) if self.settings.image_name_regex else None
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
        if not (self.ssh_key.startswith("ssh-") or self.ssh_key.startswith("ecdsa-") or self.ssh_key.startswith("sk-")):
            raise ConfigurationError(
                "La clave SSH no parece estar en formato OpenSSH (.pub)."
            )

    def validate(self) -> None:
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

    def existing_instance(self) -> Any:
        try:
            instances = self.all_results(
                self.compute.list_instances,
                compartment_id=self.settings.compartment_id,
                display_name=self.settings.instance_name,
            )
        except Exception as exc:
            raise FatalCloudError(
                f"No pude comprobar si ya existe la instancia: {compact_error(exc)}"
            ) from exc

        terminal_states = {"TERMINATED", "DELETED", "FAILED"}
        for instance in instances:
            if attr(instance, "lifecycle_state") not in terminal_states:
                return instance
        return None

    def fallback_candidates(self, availability_domain: str) -> list[Candidate]:
        fault_domains = self.settings.fault_domains
        if not fault_domains:
            # OCI normally accepts these names; capacity-report results take
            # precedence whenever that API is available.
            fault_domains = ("FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3")
        candidates: list[Candidate] = [Candidate(availability_domain, None)]
        candidates.extend(
            Candidate(availability_domain, fault_domain) for fault_domain in fault_domains
        )
        return candidates

    def capacity_candidates(self, availability_domain: str) -> Optional[list[Candidate]]:
        """Return candidates from the capacity report, or None if unavailable."""

        if not self.capacity_report_enabled:
            return None
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
            # Some tenancies/SDK versions do not expose this endpoint. Direct
            # launch is still valid, so fall back instead of stopping forever.
            LOG.warning(
                "No pude usar el capacity report; continuaré con intentos directos: %s",
                compact_error(exc),
            )
            self.capacity_report_enabled = False
            return None

        candidates: list[Candidate] = []
        for availability in attr(report, "shape_availabilities", []) or []:
            status = str(attr(availability, "availability_status", "")).upper()
            count = attr(availability, "available_count", None)
            if status == "AVAILABLE" and (count is None or count > 0):
                candidates.append(
                    Candidate(
                        availability_domain,
                        attr(availability, "fault_domain", None),
                    )
                )

        # Remove duplicate candidates while preserving the API's order.
        unique: list[Candidate] = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        if unique:
            self.report_empty_cycles = 0
            return unique

        self.report_empty_cycles += 1
        statuses = [
            f"{attr(item, 'fault_domain', 'auto')}={attr(item, 'availability_status', 'UNKNOWN')}"
            for item in (attr(report, "shape_availabilities", []) or [])
        ]
        LOG.info(
            "Sin capacidad reportada en %s (%s).",
            availability_domain,
            ", ".join(statuses) or "sin detalle",
        )
        if self.report_empty_cycles % self.settings.direct_fallback_every == 0:
            LOG.info("Haré un intento directo de respaldo en este ciclo.")
            return None
        return []

    def candidates(self) -> list[Candidate]:
        all_candidates: list[Candidate] = []
        for availability_domain in self.availability_domains:
            from_report = self.capacity_candidates(availability_domain)
            if from_report is None:
                all_candidates.extend(self.fallback_candidates(availability_domain))
            else:
                all_candidates.extend(from_report)
        return all_candidates

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

    def launch_candidate(self, candidate: Candidate) -> tuple[Optional[Any], str]:
        """Try one placement; keep one idempotency token across transient retries."""

        details = self.launch_details(candidate)
        retry_token = uuid.uuid4().hex
        for retry_index in range(self.settings.create_retries):
            try:
                response = self.compute.launch_instance(
                    details,
                    opc_retry_token=retry_token,
                    retry_strategy=oci.retry.NoneRetryStrategy(),
                )
                return response.data, "created"
            except Exception as exc:
                if is_capacity_error(exc):
                    LOG.info(
                        "Sin capacidad en %s / %s.",
                        candidate.availability_domain,
                        candidate.fault_domain or "automático",
                    )
                    return None, "capacity"

                # A timeout can mean OCI created the VM but the response was
                # lost. Check the deterministic name before sending another
                # request, and reuse the same retry token for the same request.
                try:
                    existing = self.existing_instance()
                except FatalCloudError:
                    existing = None
                if existing:
                    LOG.info("La instancia apareció después de un error de red.")
                    return existing, "created"

                if not is_transient_error(exc):
                    raise FatalCloudError(
                        f"OCI rechazó la creación de la instancia: {compact_error(exc)}"
                    ) from exc
                LOG.warning(
                    "Error transitorio al crear (%d/%d): %s",
                    retry_index + 1,
                    self.settings.create_retries,
                    compact_error(exc),
                )
                if retry_index + 1 < self.settings.create_retries:
                    delay = min(
                        60,
                        self.settings.create_retry_delay * (2**retry_index),
                    )
                    self.stop_event.wait(delay)
                    if self.stop_event.is_set():
                        return None, "stopped"
        return None, "transient"

    def wait_for_instance(self, instance: Any) -> Any:
        instance_id = attr(instance, "id")
        if not instance_id:
            return instance
        deadline = time.monotonic() + self.settings.instance_wait_seconds
        latest = instance
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                latest = self.compute.get_instance(instance_id).data
            except Exception as exc:
                LOG.warning("No pude actualizar el estado de la instancia: %s", compact_error(exc))
                self.stop_event.wait(10)
                continue
            state = attr(latest, "lifecycle_state", "UNKNOWN")
            if state in {"RUNNING", "STOPPED", "TERMINATED", "FAILED"}:
                return latest
            self.stop_event.wait(10)
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
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
            LOG.info("Telegram no configurado; conservaré los datos en %s.", self.settings.state_file)
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

    def run_cycle(self) -> bool:
        existing = self.existing_instance()
        if existing:
            existing_state = attr(existing, "lifecycle_state", "UNKNOWN")
            if existing_state in {"TERMINATING", "DELETING"}:
                LOG.info(
                    "%s está %s; esperaré a que termine antes de volver a intentar.",
                    self.settings.instance_name,
                    existing_state,
                )
                return False
            LOG.info("Ya existe %s; no crearé otra instancia.", self.settings.instance_name)
            if not self.settings.state_file.exists():
                self.finish(existing)
            return True

        if self.settings.max_attempts and self.attempts >= self.settings.max_attempts:
            LOG.info("Se alcanzó OCI_MAX_ATTEMPTS=%d.", self.settings.max_attempts)
            return False

        candidates = self.candidates()
        if not candidates:
            return False

        for candidate in candidates:
            if self.stop_event.is_set():
                return False
            self.attempts += 1
            LOG.info(
                "Intento %d: %s / %s",
                self.attempts,
                candidate.availability_domain,
                candidate.fault_domain or "automático",
            )
            instance, result = self.launch_candidate(candidate)
            if instance is not None:
                self.finish(instance)
                return True
            if result == "stopped":
                return False
        return False

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

    def run(self, once: bool) -> int:
        self.install_signal_handlers()
        self.validate()

        if once:
            self.run_cycle()
            return 0

        while not self.stop_event.is_set():
            try:
                if self.run_cycle():
                    LOG.info("Trabajo terminado; no volveré a intentar crear otra instancia.")
                    return 0
            except FatalCloudError as exc:
                LOG.error("Error no reintentable: %s", exc)
                return 2
            except ConfigurationError as exc:
                LOG.error("Configuración inválida: %s", exc)
                return 2
            if self.settings.max_attempts and self.attempts >= self.settings.max_attempts:
                LOG.info("Se alcanzó el límite de intentos; terminando.")
                return 0
            low = max(15, self.settings.interval_seconds - self.settings.jitter_seconds)
            high = self.settings.interval_seconds + self.settings.jitter_seconds
            delay = random.randint(low, max(low, high))
            LOG.info("Siguiente comprobación en %d segundos.", delay)
            self.stop_event.wait(delay)
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
        help="valida y realiza un solo ciclo; no queda en polling continuo",
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
        help="omite el endpoint de capacity report y hace intentos directos",
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
