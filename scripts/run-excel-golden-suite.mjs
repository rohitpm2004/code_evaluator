import fs from 'fs';
import fsp from 'fs/promises';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { exec } from 'child_process';
import util from 'util';

const execPromise = util.promisify(exec);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, '..');

const pythonBin = process.platform === 'win32' ? 'python' : 'python3';
const runnerScript = path.resolve(rootDir, 'evaluators/excel/engine/excel_runner.py');

console.log('================================================================');
console.log('🚀 EXCEL EVALUATOR COMPREHENSIVE 15-SCENARIO GOLDEN TEST SUITE');
console.log('================================================================\n');

let passCount = 0;
let failCount = 0;

function assert(condition, testName, details = '') {
  if (condition) {
    console.log(`✅ PASS: ${testName}`);
    passCount++;
  } else {
    console.error(`❌ FAIL: ${testName} - ${details}`);
    failCount++;
  }
}

async function run() {
  // -------------------------------------------------------------
  // TEST SUITE 1: API & Security Validation
  // -------------------------------------------------------------
  console.log('--- Suite 1: Ingestion & Security Validation ---');
  const { evaluate } = await import('../controller/evaluatorController.js');

  let mockStatus = 200;
  let mockJson = null;
  const mockRes = {
    status(c) { mockStatus = c; return this; },
    json(d) { mockJson = d; return this; }
  };

  await evaluate({ body: { type: 'excel', fileUrl: 'https://example.com/file.csv' } }, mockRes);
  assert(mockStatus === 400 && mockJson?.error?.includes('.xlsx'), 'Reject non-.xlsx file extension');

  mockStatus = 200;
  mockJson = null;
  await evaluate({ body: { type: 'excel', fileUrl: 'https://example.com/legacy_model.xls' } }, mockRes);
  assert(mockStatus === 400 && mockJson?.error?.includes('.xls'), 'Reject legacy binary .xls file extension');

  mockStatus = 200;
  mockJson = null;
  await evaluate({ body: { type: 'excel' } }, mockRes);
  assert(mockStatus === 400 && mockJson?.error?.includes('required'), 'Reject missing fileUrl/filePath');

  // -------------------------------------------------------------
  // TEST SUITE 2: Queue Manager Configuration
  // -------------------------------------------------------------
  console.log('\n--- Suite 2: Queue Infrastructure & Concurrency ---');
  const queueManager = (await import('../config/queueManager.js')).default;
  const queueTypes = queueManager.getQueueTypes ? queueManager.getQueueTypes() : Object.keys(queueManager.queues || {});
  assert(queueTypes.includes('excel'), 'excel queue registered in queueManager');

  const excelCfg = queueManager.getConfig('excel');
  assert(excelCfg?.concurrency >= 2, `excel queue concurrency slot configured (${excelCfg?.concurrency || 2})`);

  // -------------------------------------------------------------
  // TEST SUITE 3: Synthetic Golden Workbooks Generator
  // -------------------------------------------------------------
  console.log('\n--- Suite 3: Generating 15 Golden Datasets ---');

  const fixturesDir = path.resolve(rootDir, 'test/fixtures/excel');
  if (!fs.existsSync(fixturesDir)) {
    fs.mkdirSync(fixturesDir, { recursive: true });
  }

  const generatorScript = `
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import BarChart, Reference
import os

fixtures_dir = r"${fixturesDir.replace(/\\/g, '\\\\')}"

# 1. Golden 1: Perfect Table & SUM formula
wb1 = openpyxl.Workbook()
ws1 = wb1.active
ws1.title = "Sales"
ws1.append(["Product", "Quantity", "Price", "Total"])
for col in ["A1", "B1", "C1", "D1"]:
    ws1[col].font = Font(bold=True)
    ws1[col].fill = PatternFill(start_color="90EE90", fill_type="solid")
ws1.append(["Apples", 20, 35, "=B2*C2"])
ws1.append(["Bananas", 15, 20, "=B3*C3"])
ws1.append(["Oranges", 10, 50, "=B4*C4"])
ws1.append(["Grand Total", "", "", "=SUM(D2:D4)"])
ws1["C2"].number_format = "$#,##0.00"
ws1["C3"].number_format = "$#,##0.00"
ws1["C4"].number_format = "$#,##0.00"
wb1.save(os.path.join(fixtures_dir, "golden_01_perfect.xlsx"))

# 2. Golden 2: Static Literal Cheat (="128450" and hardcoded values)
wb2 = openpyxl.Workbook()
ws2 = wb2.active
ws2.title = "Sales"
ws2.append(["Product", "Quantity", "Price", "Total"])
ws2.append(["Apples", 20, 35, 700])
ws2.append(["Bananas", 15, 20, 300])
ws2.append(["Grand Total", "", "", '="1000"'])
wb2.save(os.path.join(fixtures_dir, "golden_02_cheat.xlsx"))

# 3. Golden 3: Missing Rows & Empty Cells (Uma Scenario)
wb3 = openpyxl.Workbook()
ws3 = wb3.active
ws3.title = "Sales"
ws3.append(["Product", "Quantity", "Price"])
ws3.append(["Apples", 20, 35])
ws3.append(["Bananas", 15, 20])
ws3.append(["Oranges", 12, None])
ws3.append(["Mangoes", 10, None])
wb3.save(os.path.join(fixtures_dir, "golden_03_incomplete.xlsx"))

# 4. Golden 4: Shifted Anchor Rows (Table starts at row 5)
wb4 = openpyxl.Workbook()
ws4 = wb4.active
ws4.title = "Dashboard"
ws4.cell(row=5, column=2, value="Net Profit")
ws4.cell(row=5, column=3, value=25000)
wb4.save(os.path.join(fixtures_dir, "golden_04_shifted_anchor.xlsx"))

# 5. Golden 5: Fuzzy Tab Name ("2026 Summary Report")
wb5 = openpyxl.Workbook()
ws5 = wb5.active
ws5.title = "2026 Summary Report"
ws5.append(["Revenue", 50000])
wb5.save(os.path.join(fixtures_dir, "golden_05_fuzzy_tab.xlsx"))

# 6. Golden 6: VLOOKUP Exact Match across sheets
wb6 = openpyxl.Workbook()
ws6_orders = wb6.active
ws6_orders.title = "Orders"
ws6_orders.append(["Item", "Qty", "UnitPrice", "Total"])
ws6_orders.append(["Apples", 10, "=VLOOKUP(A2, Rates!$A$2:$B$4, 2, FALSE)", "=B2*C2"])
ws6_orders.append(["Bananas", 20, "=VLOOKUP(A3, Rates!$A$2:$B$4, 2, FALSE)", "=B3*C3"])
ws6_rates = wb6.create_sheet(title="Rates")
ws6_rates.append(["Product", "Price"])
ws6_rates.append(["Apples", 2.50])
ws6_rates.append(["Bananas", 1.20])
ws6_rates.append(["Oranges", 3.00])
wb6.save(os.path.join(fixtures_dir, "golden_06_vlookup_exact.xlsx"))

# 7. Golden 7: Modern XLOOKUP
wb7 = openpyxl.Workbook()
ws7_inv = wb7.active
ws7_inv.title = "Inventory"
ws7_inv.append(["SKU", "Price"])
ws7_inv.append(["SKU-101", "=XLOOKUP(A2, Catalog!$A$2:$A$4, Catalog!$B$2:$B$4, 0)"])
ws7_inv.append(["SKU-102", "=XLOOKUP(A3, Catalog!$A$2:$A$4, Catalog!$B$2:$B$4, 0)"])
ws7_cat = wb7.create_sheet(title="Catalog")
ws7_cat.append(["SKU", "Price"])
ws7_cat.append(["SKU-101", 150])
ws7_cat.append(["SKU-102", 275])
ws7_cat.append(["SKU-103", 80])
wb7.save(os.path.join(fixtures_dir, "golden_07_xlookup_modern.xlsx"))

# 8. Golden 8: Nested IF Logic
wb8 = openpyxl.Workbook()
ws8 = wb8.active
ws8.title = "Grading"
ws8.append(["Score", "Grade"])
ws8.append([95, '=IF(A2>=90, "A", IF(A2>=70, "B", "C"))'])
ws8.append([75, '=IF(A3>=90, "A", IF(A3>=70, "B", "C"))'])
ws8.append([50, '=IF(A4>=90, "A", IF(A4>=70, "B", "C"))'])
wb8.save(os.path.join(fixtures_dir, "golden_08_nested_ifs.xlsx"))

# 9. Golden 9: IFERROR Divide-by-Zero Protection
wb9 = openpyxl.Workbook()
ws9 = wb9.active
ws9.title = "Ratios"
ws9.append(["Revenue", "Units", "AvgPrice"])
ws9.append([1000, 10, "=IFERROR(A2/B2, 0)"])
ws9.append([2000, 0, "=IFERROR(A3/B3, 0)"])
wb9.save(os.path.join(fixtures_dir, "golden_09_iferror_divzero.xlsx"))

# 10. Golden 10: Unhandled Errors (Cell Error Isolation)
wb10 = openpyxl.Workbook()
ws10 = wb10.active
ws10.title = "Errors"
ws10.append(["ValA", "ValB", "Result"])
ws10.append([10, 0, "=A2/B2"])
ws10.append([50, 2, "=A3*B3"])
wb10.save(os.path.join(fixtures_dir, "golden_10_unhandled_errors.xlsx"))

# 11. Golden 11: Multi-Sheet 3D Consolidation
wb11 = openpyxl.Workbook()
ws11_tot = wb11.active
ws11_tot.title = "Total"
ws11_tot.append(["Category", "Q1_Sum"])
ws11_tot.append(["Sales", "=Jan!B2 + Feb!B2 + Mar!B2"])
ws11_jan = wb11.create_sheet(title="Jan")
ws11_jan.append(["Category", "Amount"])
ws11_jan.append(["Sales", 500])
ws11_feb = wb11.create_sheet(title="Feb")
ws11_feb.append(["Category", "Amount"])
ws11_feb.append(["Sales", 700])
ws11_mar = wb11.create_sheet(title="Mar")
ws11_mar.append(["Category", "Amount"])
ws11_mar.append(["Sales", 800])
wb11.save(os.path.join(fixtures_dir, "golden_11_multisheet_3d.xlsx"))

# 12. Golden 12: Mixed Case & Whitespace in Formulas
wb12 = openpyxl.Workbook()
ws12 = wb12.active
ws12.title = "Irregular"
ws12.append(["Val"])
ws12.append([10])
ws12.append([20])
ws12.append([30])
ws12.append(["= sum( A2 : A4 )"])
ws12.append(["=average( A2:A4 )"])
wb12.save(os.path.join(fixtures_dir, "golden_12_mixed_case_spaces.xlsx"))

# 13. Golden 13: Advanced Cheat Evasion (=45000+0, static arithmetic without cell refs)
wb13 = openpyxl.Workbook()
ws13 = wb13.active
ws13.title = "Cheats"
ws13.append(["Item", "Total"])
ws13.append(["Product A", "=45000+0"])
ws13.append(["Product B", "=1200*1"])
wb13.save(os.path.join(fixtures_dir, "golden_13_advanced_cheats.xlsx"))

# 14. Golden 14: Native OpenXML Chart
wb14 = openpyxl.Workbook()
ws14 = wb14.active
ws14.title = "Analytics"
ws14.append(["Team", "Score"])
ws14.append(["Team Alpha", 85])
ws14.append(["Team Beta", 92])
ws14.append(["Team Gamma", 78])
chart14 = BarChart()
chart14.type = "col"
chart14.style = 10
chart14.title = "Team Performance"
data14 = Reference(ws14, min_col=2, min_row=1, max_row=4)
cats14 = Reference(ws14, min_col=1, min_row=2, max_row=4)
chart14.add_data(data14, titles_from_data=True)
chart14.set_categories(cats14)
ws14.add_chart(chart14, "D2")
wb14.save(os.path.join(fixtures_dir, "golden_14_native_charts.xlsx"))

# 15. Golden 15: Large Dataset (1,000 Rows)
wb15 = openpyxl.Workbook()
ws15 = wb15.active
ws15.title = "Transactions"
ws15.append(["ID", "Qty", "Price", "Subtotal"])
for i in range(1, 1001):
    ws15.append([i, 5, 20, f"=B{i+1}*C{i+1}"])
ws15.append(["Total", "", "", "=SUM(D2:D1001)"])
wb15.save(os.path.join(fixtures_dir, "golden_15_large_dataset.xlsx"))

print("All 15 golden fixtures generated successfully.")
`;

  const genScriptPath = path.resolve(fixturesDir, 'generate_fixtures.py');
  fs.writeFileSync(genScriptPath, generatorScript, 'utf8');

  try {
    await execPromise(`"${pythonBin}" "${genScriptPath}"`);
    console.log('✅ Generated 15 comprehensive golden fixtures');
  } catch (genErr) {
    console.error('Failed to generate fixtures:', genErr.message);
  }

  async function runRunner(orig, recalc, specObj) {
    const tmpSpec = path.join(os.tmpdir(), `test-spec-${Date.now()}-${Math.random().toString(36).substring(7)}.json`);
    await fsp.writeFile(tmpSpec, JSON.stringify(specObj), 'utf8');
    try {
      const { stdout } = await execPromise(`"${pythonBin}" "${runnerScript}" --original "${orig}" --recalculated "${recalc}" --spec "${tmpSpec}"`);
      return JSON.parse(stdout);
    } finally {
      await fsp.unlink(tmpSpec).catch(() => {});
    }
  }

  console.log('\n--- Suite 4: Executing Golden Verification Scenarios ---');

  // --- TEST 3A: Golden 1 Perfect Submission ---
  const g1Path = path.resolve(fixturesDir, 'golden_01_perfect.xlsx');
  const spec1 = {
    rubric: {
      criteria: [
        { name: "Data Formatting & Structure", weight: 40 },
        { name: "Formulas & Dynamic Calculations", weight: 60 }
      ]
    },
    testCases: [
      { criterion: "Data Formatting & Structure", type: "format_check", range: "A1:D1", bold: true },
      { criterion: "Formulas & Dynamic Calculations", type: "formula_range_check", range: "D2:D4", mustBeFormula: true }
    ]
  };
  const res1 = await runRunner(g1Path, g1Path, spec1);
  assert(res1.score === 100, `Golden 1 (Perfect submission) scores 100/100 (got ${res1.score})`);
  assert(res1.detected_formulas.length > 0, 'Golden 1 detected formulas');

  // --- TEST 3B: Golden 2 Cheat Submission ---
  const g2Path = path.resolve(fixturesDir, 'golden_02_cheat.xlsx');
  const spec2 = {
    rubric: {
      criteria: [
        { name: "Formulas & Dynamic Calculations", weight: 100 }
      ]
    },
    testCases: [
      { criterion: "Formulas & Dynamic Calculations", type: "formula_range_check", range: "D2:D3", mustBeFormula: true }
    ]
  };
  const res2 = await runRunner(g2Path, g2Path, spec2);
  assert(res2.rubric_breakdown["Formulas & Dynamic Calculations"] === 0, `Golden 2 (Static literal cheat) awards 0 for formulas (got ${res2.rubric_breakdown["Formulas & Dynamic Calculations"]})`);

  // --- TEST 3C: Golden 3 Incomplete Table (Uma Scenario) ---
  const g3Path = path.resolve(fixturesDir, 'golden_03_incomplete.xlsx');
  const spec3 = {
    rubric: {
      criteria: [
        { name: "Data Formatting & Structure", weight: 60 },
        { name: "Formulas & Dynamic Calculations", weight: 40 }
      ]
    },
    testCases: []
  };
  const res3 = await runRunner(g3Path, g3Path, spec3);
  assert(res3.score < 60, `Golden 3 (Incomplete prices & no formulas) is penalized (< 60, got ${res3.score})`);
  assert(res3.calculation_warnings.some(w => w.includes('blank')), 'Golden 3 flags missing table cells in warnings');

  // --- TEST 3D: Golden 4 Shifted Anchor Matching ---
  const g4Path = path.resolve(fixturesDir, 'golden_04_shifted_anchor.xlsx');
  const spec4 = {
    rubric: { criteria: [{ name: "Accuracy", weight: 100 }] },
    testCases: [
      { criterion: "Accuracy", type: "value_check", anchor: "Net Profit", direction: "right", expectedValue: 25000 }
    ]
  };
  const res4 = await runRunner(g4Path, g4Path, spec4);
  assert(res4.score === 100, `Golden 4 (Shifted anchor match) finds offset cell and awards 100/100 (got ${res4.score})`);

  // --- TEST 3E: Golden 5 Fuzzy Tab Name Resolution ---
  const g5Path = path.resolve(fixturesDir, 'golden_05_fuzzy_tab.xlsx');
  const spec5 = {
    rubric: { criteria: [{ name: "Summary", weight: 100 }] },
    testCases: [
      { criterion: "Summary", type: "value_check", sheet: "Summary", cell: "B1", expectedValue: 50000 }
    ]
  };
  const res5 = await runRunner(g5Path, g5Path, spec5);
  assert(res5.score === 100, `Golden 5 (Fuzzy tab matching 'Summary' -> '2026 Summary Report') awards 100/100 (got ${res5.score})`);

  // --- TEST 3F: Golden 6 VLOOKUP Exact Match across sheets ---
  const g6Path = path.resolve(fixturesDir, 'golden_06_vlookup_exact.xlsx');
  const spec6 = {
    rubric: { criteria: [{ name: "Lookup Functions", weight: 100 }] },
    testCases: [
      { criterion: "Lookup Functions", type: "function_check", sheet: "Orders", range: "C2:C3", acceptedFunctions: ["VLOOKUP"] }
    ]
  };
  const res6 = await runRunner(g6Path, g6Path, spec6);
  assert(res6.score === 100, `Golden 6 (VLOOKUP exact match) verified required lookup function (got ${res6.score})`);

  // --- TEST 3G: Golden 7 Modern XLOOKUP ---
  const g7Path = path.resolve(fixturesDir, 'golden_07_xlookup_modern.xlsx');
  const spec7 = {
    rubric: { criteria: [{ name: "Modern Lookups", weight: 100 }] },
    testCases: [
      { criterion: "Modern Lookups", type: "function_check", sheet: "Inventory", range: "B2:B3", acceptedFunctions: ["XLOOKUP"] }
    ]
  };
  const res7 = await runRunner(g7Path, g7Path, spec7);
  assert(res7.score === 100, `Golden 7 (Modern XLOOKUP) verified modern formula token (got ${res7.score})`);

  // --- TEST 3H: Golden 8 Nested IF Logic ---
  const g8Path = path.resolve(fixturesDir, 'golden_08_nested_ifs.xlsx');
  const spec8 = {
    rubric: { criteria: [{ name: "Conditional Logic", weight: 100 }] },
    testCases: [
      { criterion: "Conditional Logic", type: "formula_range_check", sheet: "Grading", range: "B2:B4", mustBeFormula: true, acceptedFunctions: ["IF"] }
    ]
  };
  const res8 = await runRunner(g8Path, g8Path, spec8);
  assert(res8.score === 100, `Golden 8 (Nested IF logic) parsed multi-branch formula (got ${res8.score})`);

  // --- TEST 3I: Golden 9 IFERROR Protection ---
  const g9Path = path.resolve(fixturesDir, 'golden_09_iferror_divzero.xlsx');
  const spec9 = {
    rubric: { criteria: [{ name: "Error Handling", weight: 100 }] },
    testCases: [
      { criterion: "Error Handling", type: "function_check", sheet: "Ratios", range: "C2:C3", acceptedFunctions: ["IFERROR"] }
    ]
  };
  const res9 = await runRunner(g9Path, g9Path, spec9);
  assert(res9.score === 100, `Golden 9 (IFERROR protection) verified error wrapper (got ${res9.score})`);

  // --- TEST 3J: Golden 10 Unhandled Error Isolation ---
  const g10Path = path.resolve(fixturesDir, 'golden_10_unhandled_errors.xlsx');
  const spec10 = {
    rubric: { criteria: [{ name: "Calculations", weight: 100 }] },
    testCases: [
      { criterion: "Calculations", type: "formula_range_check", sheet: "Errors", range: "C2:C3", mustBeFormula: true }
    ]
  };
  const res10 = await runRunner(g10Path, g10Path, spec10);
  assert(res10.detected_formulas.length > 0, 'Golden 10 isolated cell formulas without crashing');

  // --- TEST 3K: Golden 11 Multi-Sheet 3D Consolidation ---
  const g11Path = path.resolve(fixturesDir, 'golden_11_multisheet_3d.xlsx');
  const spec11 = {
    rubric: { criteria: [{ name: "Multi-Sheet", weight: 100 }] },
    testCases: [
      { criterion: "Multi-Sheet", type: "formula_range_check", sheet: "Total", range: "B2", mustBeFormula: true }
    ]
  };
  const res11 = await runRunner(g11Path, g11Path, spec11);
  assert(res11.score === 100, `Golden 11 (Multi-sheet 3D references) evaluated successfully (got ${res11.score})`);

  // --- TEST 3L: Golden 12 Mixed Case & Whitespace in Formulas ---
  const g12Path = path.resolve(fixturesDir, 'golden_12_mixed_case_spaces.xlsx');
  const spec12 = {
    rubric: { criteria: [{ name: "Functions", weight: 100 }] },
    testCases: [
      { criterion: "Functions", type: "function_check", sheet: "Irregular", range: "A5", acceptedFunctions: ["SUM"] },
      { criterion: "Functions", type: "function_check", sheet: "Irregular", range: "A6", acceptedFunctions: ["AVERAGE"] }
    ]
  };
  const res12 = await runRunner(g12Path, g12Path, spec12);
  assert(res12.score === 100, `Golden 12 (Mixed casing & extra spaces) normalized and verified (got ${res12.score})`);

  // --- TEST 3M: Golden 13 Anti-Cheat Static Arithmetic Evasion ---
  const g13Path = path.resolve(fixturesDir, 'golden_13_advanced_cheats.xlsx');
  const spec13 = {
    rubric: { criteria: [{ name: "Dynamic Formulas", weight: 100 }] },
    testCases: [
      { criterion: "Dynamic Formulas", type: "formula_range_check", sheet: "Cheats", range: "B2:B3", mustBeFormula: true }
    ]
  };
  const res13 = await runRunner(g13Path, g13Path, spec13);
  assert(res13.score === 0, `Golden 13 (Anti-cheat triggered on =45000+0 with no cell refs) awarded 0 (got ${res13.score})`);

  // --- TEST 3N: Golden 14 Native OpenXML Chart Detection ---
  const g14Path = path.resolve(fixturesDir, 'golden_14_native_charts.xlsx');
  const spec14 = {
    rubric: { criteria: [{ name: "Visualizations", weight: 100 }] },
    testCases: [
      { criterion: "Visualizations", type: "chart_check", sheet: "Analytics", chartType: "bar", title: "Team Performance" }
    ]
  };
  const res14 = await runRunner(g14Path, g14Path, spec14);
  assert(res14.score === 100, `Golden 14 (Native OpenXML Chart detection) verified chart type & title (got ${res14.score})`);

  // --- TEST 3O: Golden 15 Large Dataset 1000 Rows Throughput & Performance ---
  const g15Path = path.resolve(fixturesDir, 'golden_15_large_dataset.xlsx');
  const spec15 = {
    rubric: { criteria: [{ name: "Performance", weight: 100 }] },
    testCases: [
      { criterion: "Performance", type: "formula_range_check", sheet: "Transactions", range: "D2:D1001", mustBeFormula: true }
    ]
  };
  const startT = Date.now();
  const res15 = await runRunner(g15Path, g15Path, spec15);
  const elapsedMs = Date.now() - startT;
  assert(res15.score === 100, `Golden 15 (1,000 Rows processed) scored 100/100 in ${elapsedMs}ms`);
  assert(elapsedMs < 5000, `Golden 15 executed under 5-second budget (${elapsedMs}ms)`);

  // -------------------------------------------------------------
  // TEST SUITE 5: Controlled Formula-Behavior Mutations
  // -------------------------------------------------------------
  console.log('\n--- Suite 5: Controlled Scenario Mutations (formula_behavior_check) ---');
  const specBehavior = {
    rubric: { criteria: [{ name: "Dynamic Model", weight: 100 }] },
    testCases: [
      {
        criterion: "Dynamic Model",
        type: "formula_behavior_check",
        sheet: "Sales",
        inputs: [{ cell: "B2", value: 50 }],
        expectedOutputs: [{ cell: "D2", expectedValue: 1750 }]
      }
    ]
  };

  const resBehav = await runRunner(g1Path, g1Path, specBehavior);
  assert(resBehav.assertion_results.some(a => a.type === 'formula_behavior_check'), 'Executed formula_behavior_check assertion');

  // -------------------------------------------------------------
  // Summary Report
  // -------------------------------------------------------------
  console.log('\n================================================================');
  console.log(`🏁 AUTOMATED 15-SCENARIO GOLDEN SUITE FINISHED: ${passCount} PASSED, ${failCount} FAILED`);
  console.log('================================================================\n');

  if (failCount > 0) {
    process.exit(1);
  } else {
    process.exit(0);
  }
}

run().catch(err => {
  console.error('Fatal Test Runner Exception:', err);
  process.exit(1);
});
