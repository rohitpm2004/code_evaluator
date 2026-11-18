"""
Sprint 6 Automated Test Suite: 50-Workbook Benchmark Certification & Ground Truth

Validates the full 50-workbook benchmark suite across all cohorts:
- 20 Positive workbooks (correct formulas, alternative lookups, charts, pivots, joins)
- 20 Negative workbooks (hardcoded values, calculation errors, broken lookups, stale caches, wrong charts)
- 10 Edge cases (shifted cells, whitespace, case insensitivity, non-100 max scores, partial caps)

Certifies:
- 100.0% Agreement Rate with teacher ground truth
- 0.0% False Positive Rate (no incorrect student work awarded unearned marks)
- 0.0% False Negative Rate (no valid student submissions penalized)
- Zero score drift across repeated regression runs
- Evaluation latency SLA bounds (< 500ms average per submission)
"""

import os
import sys
import unittest
import tempfile
import shutil

# Add engine to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../engine")))
from benchmark_harness import ExcelBenchmarkHarness  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]


class TestSprint6BenchmarkCertification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = ExcelBenchmarkHarness()
        cls.benchmark_results = cls.harness.run_benchmark_suite()

    @classmethod
    def tearDownClass(cls):
        cls.harness.cleanup()

    def test_full_50_benchmark_catalog_execution(self):
        """Certifies that all 50 benchmarks execute successfully and achieve 100% agreement."""
        res = self.benchmark_results
        self.assertEqual(res["total_benchmarks"], 50)
        self.assertEqual(res["passed"], 50)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(res["agreement_rate_pct"], 100.0)

    def test_positive_cohort_accuracy(self):
        """Certifies that all 20 positive cases pass and achieve 0.0% False Negative Rate."""
        res = self.benchmark_results
        positive_results = [r for r in res["results"] if r["cohort"] == "positive"]
        self.assertEqual(len(positive_results), 20)
        for r in positive_results:
            self.assertTrue(r["is_match"], f"Positive scenario '{r['id']}' failed: expected {r['expected_score']}, got {r['actual_score']}")
            self.assertEqual(r["actual_score"], r["expected_score"])
        self.assertEqual(res["false_negative_rate_pct"], 0.0)

    def test_negative_cohort_detection(self):
        """Certifies that all 20 negative cases properly detect defects with 0.0% False Positive Rate."""
        res = self.benchmark_results
        negative_results = [r for r in res["results"] if r["cohort"] == "negative"]
        self.assertEqual(len(negative_results), 20)
        for r in negative_results:
            self.assertTrue(r["is_match"], f"Negative scenario '{r['id']}' failed: expected {r['expected_score']}, got {r['actual_score']}")
            self.assertEqual(r["actual_score"], r["expected_score"])
        self.assertEqual(res["false_positive_rate_pct"], 0.0)

    def test_edge_case_cohort_resilience(self):
        """Certifies that all 10 edge cases (shifted cells, whitespace, non-100 scales) pass without error."""
        res = self.benchmark_results
        edge_results = [r for r in res["results"] if r["cohort"] == "edge_case"]
        self.assertEqual(len(edge_results), 10)
        for r in edge_results:
            self.assertTrue(r["is_match"], f"Edge scenario '{r['id']}' failed: expected {r['expected_score']}, got {r['actual_score']}")
            self.assertEqual(r["actual_score"], r["expected_score"])

    def test_benchmark_metrics_sla(self):
        """Certifies that benchmark latency is well within production SLA bounds (< 500ms avg)."""
        res = self.benchmark_results
        self.assertLess(res["avg_latency_ms"], 500.0, f"Average latency {res['avg_latency_ms']}ms exceeded 500ms SLA.")
        self.assertEqual(res["score_drift_count"], 0)

    def test_zero_score_drift_regression(self):
        """Certifies zero score drift by repeating execution and verifying deterministic results."""
        with ExcelBenchmarkHarness() as temp_harness:
            repeat_results = temp_harness.run_benchmark_suite()
            self.assertEqual(repeat_results["passed"], 50)
            self.assertEqual(repeat_results["score_drift_count"], 0)
            for r1, r2 in zip(self.benchmark_results["results"], repeat_results["results"]):
                self.assertEqual(r1["actual_score"], r2["actual_score"], f"Score drift in {r1['id']}: {r1['actual_score']} != {r2['actual_score']}")


if __name__ == "__main__":
    unittest.main()

