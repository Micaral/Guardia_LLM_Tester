import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from guardia_tester.api import RequestTemplateError
from guardia_tester.models import TestCase
from guardia_tester.runner import execute_cases


CASES = [
    TestCase("A01", "A", "one", "prompt one", "block"),
    TestCase("E01", "E", "two", "prompt two", "allow"),
]


class StopOnExpiryChecker:
    def __init__(self) -> None:
        self.calls = 0

    async def check(self, prompt: str):
        self.calls += 1
        if self.calls == 3:
            raise RequestTemplateError("El token ha caducado")
        return ("block", "") if "one" in prompt else ("allow", "")


class ReloadingChecker:
    request_file = Path(".auth/example.curl")

    def __init__(self) -> None:
        self.refreshed = False
        self.reloads = 0

    async def check(self, prompt: str):
        if not self.refreshed:
            raise RequestTemplateError("El token ha caducado")
        return ("block", "") if "one" in prompt else ("allow", "")

    def reload_from_file(self) -> None:
        self.refreshed = True
        self.reloads += 1


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_round_is_discarded_when_token_expires(self) -> None:
        checker = StopOnExpiryChecker()
        with tempfile.TemporaryDirectory() as directory:
            results, completed = await execute_cases(
                CASES, checker, Path(directory), False, False, 2, "stop"
            )
        self.assertFalse(completed)
        self.assertEqual(2, len(results))
        self.assertEqual({1}, {result.attempt for result in results})
        self.assertTrue(all(result.status == "PASS" for result in results))

    async def test_expired_token_can_reload_and_retry_same_case(self) -> None:
        checker = ReloadingChecker()
        with tempfile.TemporaryDirectory() as directory, patch("builtins.input", return_value=""):
            results, completed = await execute_cases(
                CASES, checker, Path(directory), False, False, 1, "pause"
            )
        self.assertTrue(completed)
        self.assertEqual(1, checker.reloads)
        self.assertEqual(2, len(results))
        self.assertTrue(all(result.status == "PASS" for result in results))


if __name__ == "__main__":
    unittest.main()

