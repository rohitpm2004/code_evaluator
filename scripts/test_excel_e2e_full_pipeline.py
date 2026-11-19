"""
End-to-end full pipeline verification for excel_runner.py CLI and inspect_and_evaluate.
Creates an actual workbook, recalculates/runs assertions, and checks the JSON output contract.
"""
import os
import sys
import json
import tempfile
import subprocess
import openpyxl

def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        orig_path = os.path.join(tmpdir, "submission.xlsx")
        recalc_path = os.path.join(tmpdir, "recalculated.xlsx")
        spec_path = os.path.join(tmpdir, "spec.json")

        # 1. Create workbook
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Summary"

        # Headers
        ws["A1"] = "Company"
        ws["B1"] = "Revenue"
        ws["C1"] = "Tax"

        # Data rows
        ws["A2"] = "Acme Crop" # Typo for Acme Corp
        ws["B2"] = 1000
        ws["C2"] = "=B2*0.1"

        ws["A3"] = "Beta Tech"
        ws["B3"] = 2000
        ws["C3"] = "=B3*0.1"

        ws["A4"] = "Gamma Inc"
        ws["B4"] = 3000
        ws["C4"] = 300 # Static instead of formula

        wb.save(orig_path)
        wb.save(recalc_path)

        # 2. Create Spec
        spec = {
            "rubric": {
                "criteria": [
                    {"name": "Company Name", "weight": 30},
                    {"name": "Tax Formulas", "weight": 40},
                    {"name": "Revenue Values", "weight": 30}
                ]
            },
            "testCases": [
                {
                    "id": "tc1_typo",
                    "criterion": "Company Name",
                    "type": "value_check",
                    "cell": "A2",
                    "expectedValue": "Acme Corp" # Single typo transposition
                },
                {
                    "id": "tc2_formula",
                    "criterion": "Tax Formulas",
                    "type": "formula_range_check",
                    "range": "C2:C4",
                    "formula": "=B2*0.1"
                },
                {
                    "id": "tc3_val",
                    "criterion": "Revenue Values",
                    "type": "value_check",
                    "cell": "B4",
                    "expectedValue": 3000
                }
            ]
        }
        with open(spec_path, "w") as f:
            json.dump(spec, f)

        # 3. Execute excel_runner.py via subprocess CLI (exactly how evaluatorService.js calls it)
        runner_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "evaluators", "excel", "engine", "excel_runner.py")
        )
        cmd = [sys.executable, runner_script, "--original", orig_path, "--recalculated", recalc_path, "--spec", spec_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if proc.returncode != 0:
            print("STDERR:", proc.stderr)
            raise RuntimeError(f"Runner failed with return code {proc.returncode}")

        report = json.loads(proc.stdout)

        # 4. Assertions on the emitted JSON report
        assert report["success"] is True, "Report success should be True"
        assert "detection_errors" in report, "detection_errors must be in report"
        assert isinstance(report["detection_errors"], list), "detection_errors must be a list"

        # Check typo tolerance result
        tc1 = next(a for a in report["assertion_results"] if a["id"] == "tc1_typo")
        assert tc1["passed"] is True, "Typo 'Acme Crop' vs 'Acme Corp' should pass"
        assert tc1.get("typo_tolerated") is True, "typo_tolerated flag should be True"

        # Check formula partial credit
        tc2 = next(a for a in report["assertion_results"] if a["id"] == "tc2_formula")
        # 2 out of 3 cells are formulas, 1 is static 300
        assert tc2["passed"] is False, "Only 2/3 cells passed formulas"
        assert tc2["score"] == round(2/3, 4), f"Expected proportional score {round(2/3, 4)}, got {tc2['score']}"
        assert tc2["passedCells"] == 2, f"Expected 2 passed cells, got {tc2['passedCells']}"

        # Check value match
        tc3 = next(a for a in report["assertion_results"] if a["id"] == "tc3_val")
        assert tc3["passed"] is True, "Revenue 3000 should pass"

        # Total score calculation:
        # Company Name: 30 awarded
        # Tax Formulas: 40 * (2/3) = 26.67 awarded
        # Revenue Values: 30 awarded
        # Expected total = 30 + 26.67 + 30 = 86.67
        expected_score = round(30 + (40 * (2/3)) + 30, 2)
        assert abs(report["score"] - expected_score) <= 0.05, f"Expected score ~{expected_score}, got {report['score']}"

        print(f"E2E Verification SUCCESS! Final Score: {report['score']} / 100")
        print("  - Typo Tolerance: VERIFIED (Acme Crop -> Acme Corp with typo_tolerated=True)")
        print(f"  - Proportional Formula Credit: VERIFIED ({tc2['passedCells']}/{tc2['totalCells']} cells -> {tc2['score']} factor without cliff)")
        print(f"  - Detection Errors field: VERIFIED ({len(report['detection_errors'])} errors logged)")

if __name__ == "__main__":
    main()
