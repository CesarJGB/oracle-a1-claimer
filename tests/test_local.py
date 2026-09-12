import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("oci_a1_claimer", ROOT / "oci_a1_claimer.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LocalHelpersTest(unittest.TestCase):
    def test_parse_bool(self):
        self.assertTrue(MODULE.parse_bool("yes"))
        self.assertFalse(MODULE.parse_bool("0", True))
        self.assertTrue(MODULE.parse_bool(None, True))

    def test_parse_csv(self):
        self.assertEqual(
            MODULE.parse_csv("FAULT-DOMAIN-1, FAULT-DOMAIN-2"),
            ("FAULT-DOMAIN-1", "FAULT-DOMAIN-2"),
        )
        self.assertEqual(MODULE.parse_csv(""), ())

    def test_capacity_error_detection(self):
        class FakeError:
            code = "InternalError"
            message = "Out of host capacity"

        self.assertTrue(MODULE.is_capacity_error(FakeError()))

    def test_non_capacity_error_detection(self):
        class FakeError:
            code = "NotAuthorizedOrNotFound"
            message = "The caller is not authorized"

        self.assertFalse(MODULE.is_capacity_error(FakeError()))


if __name__ == "__main__":
    unittest.main()
