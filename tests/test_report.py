import tempfile
import unittest
from pathlib import Path

from guardia_tester.models import TestCase, TestResult
from guardia_tester.report import aggregate_cases, summarize, write_html_report, write_reports


class ReportTests(unittest.TestCase):
    def test_metrics_and_files(self) -> None:
        cases = [
            TestCase("A01", "A", "block ok", "x", "block"),
            TestCase("E01", "E", "false positive", "y", "allow"),
            TestCase("J01", "J", "false negative", "z", "block"),
        ]
        results = [
            TestResult(cases[0], "block", "PASS"),
            TestResult(cases[1], "block", "FAIL"),
            TestResult(cases[2], "allow", "FAIL"),
        ]
        summary = summarize(results)
        self.assertEqual(1, summary.false_positives)
        self.assertEqual(1, summary.false_negatives)
        self.assertAlmostEqual(1 / 3, summary.accuracy)
        with tempfile.TemporaryDirectory() as directory:
            paths = write_reports(results, Path(directory))
            self.assertTrue(all(path.exists() for path in paths))

    def test_flaky_case_aggregation(self) -> None:
        case = TestCase("I05", "I", "fiction", "x", "allow")
        results = [
            TestResult(case, "allow", "PASS", attempt=1),
            TestResult(case, "block", "FAIL", attempt=2),
            TestResult(case, "allow", "PASS", attempt=3),
        ]
        aggregate = aggregate_cases(results)[0]
        self.assertEqual("FLAKY", aggregate.classification)
        self.assertEqual("x", aggregate.prompt)
        self.assertAlmostEqual(2 / 3, aggregate.correct_rate)
        self.assertAlmostEqual(1 / 3, aggregate.block_rate)
        summary = summarize(results)
        self.assertEqual(1, summary.flaky)
        self.assertEqual(0, summary.stable_pass)
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.html"
            write_html_report(results, report, "test")
            content = report.read_text(encoding="utf-8")
            self.assertIn("Evolución por prompt", content)
            self.assertIn("<th>R1</th><th>R2</th><th>R3</th>", content)
            self.assertIn("decision-pass", content)
            self.assertIn("decision-fail", content)
            self.assertIn("block ✗", content)
            self.assertNotIn("Intentos individuales", content)


if __name__ == "__main__":
    unittest.main()
