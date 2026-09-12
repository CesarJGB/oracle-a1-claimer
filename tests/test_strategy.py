import importlib.util
import json
import pathlib
import stat
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("oci_a1_claimer_strategy", ROOT / "oci_a1_claimer.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class Obj:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeClock:
    def __init__(self, value=1_700_000_000.0):
        self.value = float(value)

    def time(self):
        return self.value

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class MidpointRandom:
    def uniform(self, lower, upper):
        return (lower + upper) / 2


class FakeError(Exception):
    def __init__(self, message="fake error", status=None, code="Error", headers=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.headers = headers or {}


class FakeCompute:
    def __init__(self, launch_results=None, capacity_results=None, clock=None):
        self.launch_results = list(launch_results or [])
        self.capacity_results = list(capacity_results or [])
        self.clock = clock
        self.launch_calls = []
        self.launch_times = []
        self.capacity_calls = 0
        self.list_instances_calls = 0
        self.instances = []

    def list_instances(self, **_kwargs):
        self.list_instances_calls += 1
        return Obj(data=list(self.instances))

    def create_compute_capacity_report(self, _details):
        self.capacity_calls += 1
        if self.capacity_results:
            result = self.capacity_results.pop(0)
        else:
            result = Obj(shape_availabilities=[])
        if isinstance(result, BaseException):
            raise result
        return Obj(data=result)

    def launch_instance(self, details, **kwargs):
        self.launch_calls.append((details, kwargs))
        if self.clock is not None:
            self.launch_times.append(self.clock.time())
        result = self.launch_results.pop(0) if self.launch_results else FakeError(
            "Out of host capacity", code="InternalError"
        )
        if isinstance(result, BaseException):
            raise result
        return Obj(data=result)


class FakePagination:
    @staticmethod
    def list_call_get_all_results(call, **kwargs):
        return call(**kwargs)


class FakeModels:
    class _Model:
        def __init__(self, **values):
            self.__dict__.update(values)

    CreateComputeCapacityReportDetails = _Model
    CreateCapacityReportShapeAvailabilityDetails = _Model
    CapacityReportInstanceShapeConfig = _Model
    InstanceSourceViaImageDetails = _Model
    CreateVnicDetails = _Model
    LaunchInstanceShapeConfigDetails = _Model
    LaunchInstanceDetails = _Model


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.original_oci = MODULE.oci
        MODULE.oci = types.SimpleNamespace(
            core=types.SimpleNamespace(models=FakeModels),
            pagination=FakePagination,
            retry=types.SimpleNamespace(NoneRetryStrategy=lambda: object()),
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)

    def tearDown(self):
        MODULE.oci = self.original_oci

    def settings(self, **overrides):
        values = {
            "region": "mx-monterrey-1",
            "profile": "DEFAULT",
            "config_file": self.base / "oci-config",
            "compartment_id": "compartment-test",
            "subnet_id": "subnet-test",
            "vcn_id": None,
            "image_id": "image-test",
            "image_os": "Oracle Linux",
            "image_os_version": None,
            "image_name_regex": None,
            "shape": "VM.Standard.A1.Flex",
            "ocpus": 2.0,
            "memory_gbs": 12.0,
            "boot_volume_gbs": 50,
            "instance_name": "a1-test",
            "ssh_public_key_path": None,
            "ssh_public_key": "ssh-ed25519 AAAA-test",
            "assign_public_ip": True,
            "fault_domains": (),
            "capacity_report": False,
            "direct_fallback_every": 10,
            "interval_seconds": 60,
            "jitter_seconds": 0,
            "direct_attempt_interval_seconds": 240,
            "direct_attempt_jitter_seconds": 0,
            "min_launch_gap_seconds": 60,
            "create_retries": 3,
            "create_retry_delay": 10,
            "instance_wait_seconds": 30,
            "existing_check_interval_seconds": 900,
            "max_attempts": 0,
            "state_file": self.base / "instance.json",
            "runtime_file": self.base / "runtime.json",
            "lock_file": self.base / "instance.lock",
            "telegram_bot_token": None,
            "telegram_chat_id": None,
            "ssh_user": "opc",
            "log_level": "INFO",
        }
        values.update(overrides)
        return MODULE.Settings(**values)

    def claimer(self, compute=None, clock=None, **settings_overrides):
        clock = clock or FakeClock()
        instance = MODULE.Claimer(
            self.settings(**settings_overrides),
            clock=clock,
            rng=MidpointRandom(),
            sleeper=lambda seconds: clock.advance(seconds),
        )
        instance.availability_domains = ["AD-1"]
        instance.tenancy_id = "tenancy-test"
        instance.image = Obj(id="image-test", display_name="test-image")
        instance.subnet = Obj(id="subnet-test", display_name="test-subnet")
        instance.ssh_key = "ssh-ed25519 AAAA-test"
        instance.compute = compute or FakeCompute(clock=clock)
        instance.network = Obj()
        return instance, clock

    @staticmethod
    def available_report(*fault_domains):
        return Obj(
            shape_availabilities=[
                Obj(
                    availability_status="AVAILABLE",
                    available_count=1,
                    fault_domain=fault_domain,
                )
                for fault_domain in fault_domains
            ]
        )

    def run_direct(self, instance, clock, cycles=1, step=241):
        for index in range(cycles):
            instance.run_cycle(once=True)
            if index + 1 < cycles:
                clock.advance(step)

    def test_never_more_than_one_launch_and_preserves_report_candidates(self):
        compute = FakeCompute(
            launch_results=[
                FakeError("Out of host capacity", code="InternalError")
            ]
        )
        instance, _clock = self.claimer(
            compute,
            capacity_report=True,
        )
        compute.capacity_results = [
            self.available_report(
                "FAULT-DOMAIN-1",
                "FAULT-DOMAIN-2",
                "FAULT-DOMAIN-3",
            )
        ]

        instance.run_cycle(once=True)

        self.assertEqual(len(compute.launch_calls), 1)
        pending = instance.runtime["pending_candidates"]
        self.assertEqual(
            [item["fault_domain"] for item in pending],
            ["FAULT-DOMAIN-2", "FAULT-DOMAIN-3"],
        )

    def test_direct_rotation_fd1_fd2_fd3(self):
        compute = FakeCompute(
            launch_results=[
                FakeError("Out of host capacity"),
                FakeError("Out of host capacity"),
                FakeError("Out of host capacity"),
            ]
        )
        instance, clock = self.claimer(compute)

        self.run_direct(instance, clock, cycles=3)

        self.assertEqual(
            [call[0].fault_domain for call in compute.launch_calls],
            [
                "FAULT-DOMAIN-1",
                "FAULT-DOMAIN-2",
                "FAULT-DOMAIN-3",
            ],
        )

    def test_rotation_persists_after_restart_and_honors_explicit_domains(self):
        compute = FakeCompute(launch_results=[FakeError("Out of host capacity")])
        instance, clock = self.claimer(
            compute,
            fault_domains=("CUSTOM-1", "CUSTOM-2"),
        )
        instance.run_cycle(once=True)
        clock.advance(241)

        restarted, _same_clock = self.claimer(
            FakeCompute(launch_results=[FakeError("Out of host capacity")]),
            clock=clock,
            fault_domains=("CUSTOM-1", "CUSTOM-2"),
        )
        self.assertEqual(
            restarted.fallback_candidates("AD-1"),
            [MODULE.Candidate("AD-1", "CUSTOM-2")],
        )
        self.assertNotIn(
            None,
            [candidate.fault_domain for candidate in restarted.fallback_candidates("AD-1")],
        )

    def test_default_interval_produces_about_fifteen_attempts_per_hour(self):
        clock = FakeClock()
        compute = FakeCompute(clock=clock)
        instance, _clock = self.claimer(compute, clock=clock)

        for _cycle in range(60):
            instance.run_cycle(once=True)
            clock.advance(60)

        self.assertEqual(len(compute.launch_calls), 15)
        self.assertEqual(
            len(
                [
                    event
                    for event in instance.events_in_window(now=clock.time())
                    if event["type"] == "launch"
                ]
            ),
            15,
        )

    def test_minimum_launch_gap_wins_over_short_interval(self):
        compute = FakeCompute(
            launch_results=[
                FakeError("Out of host capacity"),
                FakeError("Out of host capacity"),
            ]
        )
        instance, clock = self.claimer(
            compute,
            direct_attempt_interval_seconds=10,
            min_launch_gap_seconds=60,
        )
        instance.run_cycle(once=True)
        clock.advance(59)
        instance.run_cycle(once=True)
        self.assertEqual(len(compute.launch_calls), 1)
        clock.advance(1)
        instance.run_cycle(once=True)
        self.assertEqual(len(compute.launch_calls), 2)

    def test_429_sets_global_cooldown_without_existing_check_after_launch(self):
        compute = FakeCompute(
            launch_results=[FakeError("Too many requests", status=429, code="TooManyRequests")]
        )
        instance, clock = self.claimer(compute)

        instance.run_cycle(once=True)

        self.assertEqual(compute.list_instances_calls, 1)
        self.assertEqual(len(compute.launch_calls), 1)
        self.assertGreater(instance._cooldown_remaining(), 0)
        calls_before = (compute.capacity_calls, compute.list_instances_calls, len(compute.launch_calls))
        clock.advance(30)
        instance.run_cycle(once=True)
        self.assertEqual(
            calls_before,
            (compute.capacity_calls, compute.list_instances_calls, len(compute.launch_calls)),
        )

    def test_retry_after_is_respected(self):
        instance, clock = self.claimer(FakeCompute())
        error = FakeError(
            "Too many requests",
            status=429,
            code="TooManyRequests",
            headers={"retry-after": "120"},
        )

        until = instance.activate_rate_limit(error, "test")

        self.assertEqual(until - clock.time(), 120)

    def test_429_backoff_grows_and_is_capped(self):
        instance, clock = self.claimer(FakeCompute())
        delays = []
        for _index in range(6):
            before = clock.time()
            until = instance.activate_rate_limit(
                FakeError("Too many requests", status=429, code="TooManyRequests"),
                "test",
            )
            delays.append(until - before)
            clock.value = until + 0.1

        self.assertGreaterEqual(delays[0], 60)
        self.assertGreater(delays[1], delays[0])
        self.assertLessEqual(delays[-1], 600)
        self.assertEqual(delays[-1], 600)

    def test_temporary_capacity_report_error_is_retried(self):
        compute = FakeCompute(
            capacity_results=[
                FakeError("temporary outage", status=503, code="ServiceUnavailable"),
                Obj(shape_availabilities=[]),
            ]
        )
        instance, clock = self.claimer(compute, capacity_report=True)

        self.assertIsNone(instance.capacity_candidates("AD-1"))
        self.assertTrue(instance.capacity_report_enabled)
        retry_at = MODULE.parse_timestamp(instance.runtime["capacity_report_backoff_until"])
        clock.value = retry_at
        self.assertEqual(instance.capacity_candidates("AD-1"), [])
        self.assertEqual(compute.capacity_calls, 2)

    def test_unsupported_capacity_endpoint_uses_one_candidate_fallback(self):
        compute = FakeCompute(
            capacity_results=[
                FakeError("endpoint not supported", status=404, code="NotSupported")
            ],
            launch_results=[FakeError("Out of host capacity")],
        )
        instance, _clock = self.claimer(compute, capacity_report=True)

        instance.run_cycle(once=True)

        self.assertFalse(instance.capacity_report_enabled)
        self.assertEqual(instance.runtime["capacity_report_enabled"], False)
        self.assertEqual(len(compute.launch_calls), 1)
        self.assertEqual(len(instance.fallback_candidates("AD-1")), 1)

    def test_existing_instance_is_not_checked_on_each_empty_cycle(self):
        compute = FakeCompute(
            capacity_results=[Obj(shape_availabilities=[]) for _ in range(10)],
            launch_results=[FakeError("Out of host capacity")],
        )
        instance, clock = self.claimer(
            compute,
            capacity_report=True,
            direct_attempt_interval_seconds=1000,
        )

        for _cycle in range(8):
            instance.run_cycle(once=True)
            clock.advance(60)

        self.assertEqual(compute.capacity_calls, 8)
        self.assertEqual(compute.list_instances_calls, 1)
        self.assertEqual(len(compute.launch_calls), 1)

    def test_timeout_reuses_same_idempotency_token_for_retry(self):
        compute = FakeCompute(
            launch_results=[
                TimeoutError("request timed out"),
                FakeError("Out of host capacity"),
            ]
        )
        instance, clock = self.claimer(compute, create_retries=2)

        instance.run_cycle()
        retry_at = MODULE.parse_timestamp(instance.runtime["next_attempt_allowed"])
        clock.value = retry_at
        instance.run_cycle()

        self.assertEqual(len(compute.launch_calls), 2)
        self.assertEqual(
            compute.launch_calls[0][1]["opc_retry_token"],
            compute.launch_calls[1][1]["opc_retry_token"],
        )
        self.assertEqual(
            [event["result"] for event in instance.runtime["recent_events"] if event["type"] == "launch"],
            ["transient_error", "out_of_capacity"],
        )

    def test_metrics_categories_sum_exactly(self):
        instance, clock = self.claimer(FakeCompute())
        candidate = MODULE.Candidate("AD-1", "FAULT-DOMAIN-1")
        for result in ("created", "out_of_capacity", "rate_limited", "transient_error", "fatal_error"):
            instance._record_launch_result(
                result,
                candidate,
                retry_index=0,
                source="direct",
            )
            clock.advance(1)

        launches = [
            event
            for event in instance.events_in_window(now=clock.time())
            if event["type"] == "launch"
        ]
        counts = {result: sum(event["result"] == result for event in launches) for result in MODULE.LAUNCH_RESULTS}
        self.assertEqual(len(launches), sum(counts.values()))
        self.assertEqual(len(launches), 5)

    def test_sliding_window_is_closed_at_sixty_minutes(self):
        instance, clock = self.claimer(FakeCompute())
        candidate = MODULE.Candidate("AD-1", "FAULT-DOMAIN-1")
        instance._record_launch_result("out_of_capacity", candidate, retry_index=0, source="direct")
        clock.advance(3600)
        instance._record_launch_result("rate_limited", candidate, retry_index=0, source="direct")

        self.assertEqual(len(instance.events_in_window(now=clock.time())), 2)
        clock.advance(0.1)
        self.assertEqual(len(instance.events_in_window(now=clock.time())), 1)

    def test_missing_or_corrupt_runtime_uses_safe_state_without_touching_instance(self):
        settings = self.settings()
        instance = MODULE.Claimer(settings, clock=FakeClock(), rng=MidpointRandom())
        self.assertTrue(settings.runtime_file.exists())
        settings.state_file.write_text("successful-instance-sentinel\n", encoding="utf-8")
        settings.runtime_file.write_text("{not valid json", encoding="utf-8")

        recovered = MODULE.Claimer(settings, clock=FakeClock(), rng=MidpointRandom())

        self.assertEqual(recovered.runtime["fault_domain_index"], 0)
        self.assertEqual(
            settings.state_file.read_text(encoding="utf-8"),
            "successful-instance-sentinel\n",
        )
        self.assertEqual(stat.S_IMODE(settings.runtime_file.stat().st_mode), 0o600)

    def test_once_allows_at_most_one_real_launch_request(self):
        compute = FakeCompute(
            launch_results=[TimeoutError("request timed out"), Obj(id="should-not-be-used")]
        )
        instance, _clock = self.claimer(compute, create_retries=3)
        instance.validated = True

        result = instance.run(once=True)

        self.assertEqual(result, 0)
        self.assertEqual(len(compute.launch_calls), 1)
        self.assertEqual(
            len([event for event in instance.runtime["recent_events"] if event["type"] == "launch"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
