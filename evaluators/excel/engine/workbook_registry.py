"""Universal Workbook Object Registry (Sprint 2 - Excel Evaluator 2.0)

Single-pass, relationship-aware, memory-safe registry that maps worksheets,
tables, PivotTables, Power Query M-code, Data Model OLAP hierarchies,
charts, and named ranges with lazy-loaded cell access.
"""

import os
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Any, Optional, Tuple, Set


MAX_DECOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB safe limit
MAX_ZIP_ENTRIES = 2000

# OpenXML Namespaces
NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
}


class WorkbookObjectRegistry:
    """Relationship-aware workbook registry providing layout-invariant access to all Excel objects."""

    def __init__(self, original_path: str, recalculated_path: Optional[str] = None):
        if not os.path.exists(original_path):
            raise FileNotFoundError(f"Workbook not found at {original_path}")

        self.original_path = original_path
        self.recalculated_path = recalculated_path or original_path

        # Primary Registry Indices
        self.metadata: Dict[str, Any] = {}
        self.worksheets: Dict[str, Dict[str, Any]] = {}       # name -> sheet info
        self.sheet_file_to_name: Dict[str, str] = {}         # 'xl/worksheets/sheet1.xml' -> name
        self.tables: List[Dict[str, Any]] = []               # list of Excel Table objects
        self.pivot_tables: List[Dict[str, Any]] = []          # list of PivotTable objects
        self.pivot_caches: Dict[str, Dict[str, Any]] = {}     # cacheId -> cache definition
        self.charts: List[Dict[str, Any]] = []                # list of Chart objects
        self.power_queries: List[Dict[str, Any]] = []         # list of Power Query definitions
        self.macro_info: Dict[str, Any] = {"has_macros": False, "macro_names": []}
        self.defined_names: Dict[str, str] = {}              # name -> formula / target
        self.calculation_properties: Dict[str, Any] = {}

        # Lazy-loaded openpyxl workbook instances
        self._wb_orig = None
        self._wb_cached = None
        self._wb_recalc = None

        # Execute single-pass indexing
        self._index_package()

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Package Ingestion & Relationship Indexing (Single Pass)
    # ─────────────────────────────────────────────────────────────────────────
    def _index_package(self):
        """Indexes all relationships, metadata, and objects from the OpenXML ZIP container."""
        total_size = 0
        entry_count = 0

        with zipfile.ZipFile(self.original_path, 'r') as z:
            namelist = z.namelist()
            entry_count = len(namelist)
            if entry_count > MAX_ZIP_ENTRIES:
                raise ValueError(f"Package contains {entry_count} entries, exceeding limit ({MAX_ZIP_ENTRIES})")

            for info in z.infolist():
                total_size += info.file_size
                if total_size > MAX_DECOMPRESSED_BYTES:
                    raise ValueError(f"Decompressed package size exceeds safe limit ({MAX_DECOMPRESSED_BYTES} bytes)")

            # Step 1: Map Workbook Relationships & Worksheets
            rid_to_target = {}
            if "xl/_rels/workbook.xml.rels" in namelist:
                try:
                    wb_rels_elem = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
                    for rel in wb_rels_elem:
                        r_id = rel.attrib.get("Id")
                        target = rel.attrib.get("Target", "")
                        if r_id and target:
                            clean_t = target.lstrip("/").replace("\\", "/")
                            if not clean_t.startswith("xl/"):
                                clean_t = "xl/" + clean_t
                            rid_to_target[r_id] = clean_t
                except Exception:
                    pass

            if "xl/workbook.xml" in namelist:
                try:
                    wb_elem = ET.fromstring(z.read("xl/workbook.xml"))
                    # Parse calculation properties
                    calc_pr = wb_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}calcPr")
                    if calc_pr is not None:
                        self.calculation_properties = {k: v for k, v in calc_pr.attrib.items()}

                    # Parse sheets
                    sheets_elem = wb_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets")
                    if sheets_elem is not None:
                        for idx, s in enumerate(sheets_elem):
                            s_name = s.attrib.get("name", f"Sheet{idx+1}")
                            s_id = s.attrib.get("sheetId", str(idx+1))
                            r_id = s.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
                            state = s.attrib.get("state", "visible")
                            target_file = rid_to_target.get(r_id, f"xl/worksheets/sheet{idx+1}.xml")

                            sheet_entry = {
                                "sheet_id": s_id,
                                "name": s_name,
                                "state": state,
                                "file_path": target_file,
                                "order": idx,
                                "r_id": r_id
                            }
                            self.worksheets[s_name] = sheet_entry
                            self.sheet_file_to_name[target_file] = s_name

                    # Parse defined names
                    def_names_elem = wb_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}definedNames")
                    if def_names_elem is not None:
                        for dn in def_names_elem:
                            dn_name = dn.attrib.get("name")
                            if dn_name:
                                self.defined_names[dn_name] = dn.text or ""
                except Exception:
                    pass

            # Step 2: Index Excel Tables (xl/tables/table*.xml)
            for name in namelist:
                if name.startswith("xl/tables/table") and name.endswith(".xml"):
                    try:
                        t_elem = ET.fromstring(z.read(name))
                        t_id = t_elem.attrib.get("id", "")
                        t_name = t_elem.attrib.get("name", "")
                        t_display = t_elem.attrib.get("displayName", t_name)
                        t_ref = t_elem.attrib.get("ref", "")
                        t_totals = t_elem.attrib.get("totalsRowCount", "0") != "0"

                        row_count = 0
                        if t_ref and ":" in t_ref:
                            r_matches = re.findall(r'[0-9]+', t_ref)
                            if len(r_matches) >= 2:
                                s_r, e_r = int(r_matches[0]), int(r_matches[1])
                                row_count = max(0, e_r - s_r - (1 if t_totals else 0))

                        cols = []
                        cols_elem = t_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}tableColumns")
                        if cols_elem is not None:
                            for c in cols_elem:
                                cols.append(c.attrib.get("name", ""))

                        self.tables.append({
                            "id": t_id,
                            "name": t_name,
                            "display_name": t_display,
                            "range_ref": t_ref,
                            "row_count": row_count,
                            "has_totals_row": t_totals,
                            "columns": cols,
                            "file_path": name
                        })
                    except Exception:
                        pass

            # Step 3: Index Pivot Caches (xl/pivotCache/pivotCacheDefinition*.xml)
            for name in namelist:
                if name.startswith("xl/pivotCache/pivotCacheDefinition") and name.endswith(".xml"):
                    try:
                        pc_elem = ET.fromstring(z.read(name))
                        cache_id_match = re.search(r'pivotCacheDefinition(\d+)\.xml', name)
                        c_id = cache_id_match.group(1) if cache_id_match else name

                        record_count = pc_elem.attrib.get("recordCount", "0")
                        fields = []
                        cache_fields = pc_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}cacheFields")
                        if cache_fields is not None:
                            for cf in cache_fields:
                                fields.append(cf.attrib.get("name", ""))

                        src_elem = pc_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}cacheSource")
                        src_type = src_elem.attrib.get("type", "") if src_elem is not None else ""
                        ws_src = pc_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}worksheetSource")
                        src_sheet = ws_src.attrib.get("sheet", "") if ws_src is not None else ""
                        src_ref = ws_src.attrib.get("ref", "") if ws_src is not None else ""
                        src_name = ws_src.attrib.get("name", "") if ws_src is not None else ""

                        self.pivot_caches[c_id] = {
                            "cache_id": c_id,
                            "record_count": int(record_count) if record_count.isdigit() else 0,
                            "fields": fields,
                            "source_type": src_type,
                            "source_sheet": src_sheet,
                            "source_ref": src_ref,
                            "source_name": src_name,
                            "file_path": name
                        }
                    except Exception:
                        pass

            # Step 4: Index Pivot Tables (xl/pivotTables/pivotTable*.xml)
            # Map sheet relationships to pivot tables
            pt_to_sheet = {}
            for name in namelist:
                if name.startswith("xl/worksheets/_rels/sheet") and name.endswith(".xml.rels"):
                    sheet_xml = name.replace("/_rels/", "/").replace(".xml.rels", ".xml")
                    sheet_human = self.sheet_file_to_name.get(sheet_xml, "")
                    try:
                        s_rels = ET.fromstring(z.read(name))
                        for r in s_rels:
                            tgt = r.attrib.get("Target", "")
                            if "pivotTable" in tgt:
                                pt_file = "xl/" + tgt.replace("../", "").lstrip("/")
                                pt_to_sheet[pt_file] = sheet_human
                    except Exception:
                        pass

            for name in namelist:
                if name.startswith("xl/pivotTables/pivotTable") and name.endswith(".xml"):
                    try:
                        pt_elem = ET.fromstring(z.read(name))
                        pt_name = pt_elem.attrib.get("name", "")
                        cache_id = pt_elem.attrib.get("cacheId", "")
                        loc_elem = pt_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}location")
                        loc_ref = loc_elem.attrib.get("ref", "") if loc_elem is not None else ""

                        # Extract row/col/data fields
                        pivot_fields = []
                        pf_elem = pt_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}pivotFields")
                        if pf_elem is not None:
                            for pf in pf_elem:
                                axis = pf.attrib.get("axis", "")
                                pivot_fields.append({"axis": axis, "name": pf.attrib.get("name", "")})

                        row_fields = []
                        rf_elem = pt_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}rowFields")
                        if rf_elem is not None:
                            for rf in rf_elem:
                                row_fields.append(rf.attrib.get("x", ""))

                        data_fields = []
                        df_elem = pt_elem.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}dataFields")
                        if df_elem is not None:
                            for df in df_elem:
                                data_fields.append({
                                    "name": df.attrib.get("name", ""),
                                    "fld": df.attrib.get("fld", ""),
                                    "subtotal": df.attrib.get("subtotal", "sum")
                                })

                        sheet_assigned = pt_to_sheet.get(name, list(self.worksheets.keys())[0] if self.worksheets else "Sheet1")
                        cache_meta = self.pivot_caches.get(str(cache_id), {})
                        self.pivot_tables.append({
                            "name": pt_name,
                            "cache_id": cache_id,
                            "sheet": sheet_assigned,
                            "location_ref": loc_ref,
                            "fields": pivot_fields,
                            "row_fields": row_fields,
                            "data_fields": data_fields,
                            "record_count": cache_meta.get("record_count", 0),
                            "source_sheet": cache_meta.get("source_sheet", ""),
                            "source_ref": cache_meta.get("source_ref", ""),
                            "source_name": cache_meta.get("source_name", ""),
                            "file_path": name
                        })
                    except Exception:
                        pass

            # Step 5: Index Charts (xl/charts/chart*.xml)
            # Map drawings to sheets
            drawing_to_sheet = {}
            for name in namelist:
                if name.startswith("xl/worksheets/_rels/sheet") and name.endswith(".xml.rels"):
                    sheet_xml = name.replace("/_rels/", "/").replace(".xml.rels", ".xml")
                    sheet_human = self.sheet_file_to_name.get(sheet_xml, "")
                    try:
                        s_rels = ET.fromstring(z.read(name))
                        for r in s_rels:
                            if "drawing" in r.attrib.get("Type", ""):
                                tgt = "xl/" + r.attrib.get("Target", "").replace("../", "").lstrip("/")
                                drawing_to_sheet[tgt] = sheet_human
                    except Exception:
                        pass

            # Map chart files to sheets
            chart_to_sheet = {}
            for name in namelist:
                if name.startswith("xl/drawings/_rels/drawing") and name.endswith(".xml.rels"):
                    draw_xml = name.replace("/_rels/", "/").replace(".xml.rels", ".xml")
                    sheet_human = drawing_to_sheet.get(draw_xml, "")
                    try:
                        d_rels = ET.fromstring(z.read(name))
                        for r in d_rels:
                            if "chart" in r.attrib.get("Type", ""):
                                c_tgt = "xl/" + r.attrib.get("Target", "").replace("../", "").lstrip("/")
                                chart_to_sheet[c_tgt] = sheet_human
                    except Exception:
                        pass

            for name in namelist:
                if name.startswith("xl/charts/chart") and name.endswith(".xml"):
                    try:
                        c_elem = ET.fromstring(z.read(name))
                        ch_title = ""
                        for t in c_elem.iter():
                            if (t.tag.endswith("}v") or t.tag.endswith("}t")) and t.text and t.text.strip():
                                ch_title = t.text.strip()
                                break

                        # Identify chart type and series references
                        ch_type = "Chart"
                        num_refs = []
                        cat_refs = []
                        for node in c_elem.iter():
                            t_tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag
                            if t_tag in ["barChart", "columnChart", "lineChart", "pieChart", "doughnutChart", "scatterChart", "areaChart", "radarChart"]:
                                ch_type = t_tag.replace("Chart", "").capitalize() + "Chart"
                            if t_tag in ["numRef", "strRef"]:
                                f_node = node.find(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}f")
                                if f_node is not None and f_node.text:
                                    ref_str = f_node.text.strip()
                                    if t_tag == "numRef":
                                        num_refs.append(ref_str)
                                    else:
                                        cat_refs.append(ref_str)

                        series_count = sum(1 for el in c_elem.iter() if el.tag.endswith("}ser"))
                        sheet_assigned = chart_to_sheet.get(name, list(self.worksheets.keys())[0] if self.worksheets else "Sheet1")

                        self.charts.append({
                            "type": ch_type,
                            "title": ch_title,
                            "series_count": series_count,
                            "seriesCount": series_count,
                            "sheet": sheet_assigned,
                            "num_refs": num_refs,
                            "cat_refs": cat_refs,
                            "file_path": name
                        })
                    except Exception:
                        pass

            # Step 6: Index Power Query M-Code & Connections
            for name in namelist:
                if name.startswith("customXml/item") and name.endswith(".xml"):
                    try:
                        c_xml = z.read(name).decode("utf-8", errors="ignore")
                        if "section Section1" in c_xml or "shared #" in c_xml or "let" in c_xml:
                            q_matches = re.findall(r'shared\s+#?"([^"]+)"\s*=\s*(let[\s\S]*?in\s*[^;]+);', c_xml)
                            if not q_matches:
                                q_matches = re.findall(r'shared\s+([A-Za-z0-9_]+)\s*=\s*(let[\s\S]*?in\s*[^;]+);', c_xml)

                            for q_name, q_code in q_matches:
                                self.power_queries.append({
                                    "query_name": q_name.strip(),
                                    "m_code": q_code.strip()[:1000],
                                    "is_valid_syntax": "let" in q_code and "in" in q_code,
                                    "file_path": name
                                })
                    except Exception:
                        pass

            if self.power_queries:
                self.macro_info["has_power_query"] = True
                self.macro_info["query_names"] = [q["query_name"] for q in self.power_queries]

            # Step 7: Index VBA Macros
            if "xl/vbaProject.bin" in namelist:
                self.macro_info["has_macros"] = True
                try:
                    vba_bytes = z.read("xl/vbaProject.bin")
                    matches = re.findall(rb'(?:Sub|Function)\s+([a-zA-Z0-9_]+)', vba_bytes)
                    self.macro_info["macro_names"] = sorted(list(set(m.decode("ascii", errors="ignore") for m in matches)))
                except Exception:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # 2. Lazy OpenPyXL Workbook Loader (Cell-Level Access)
    # ─────────────────────────────────────────────────────────────────────────
    def get_openpyxl_workbooks(self):
        """Lazily load openpyxl workbook instances on demand."""
        import openpyxl

        if self._wb_orig is None:
            self._wb_orig = openpyxl.load_workbook(self.original_path, data_only=False)

        if self._wb_cached is None:
            self._wb_cached = openpyxl.load_workbook(self.original_path, data_only=True)

        if self._wb_recalc is None:
            if os.path.exists(self.recalculated_path):
                self._wb_recalc = openpyxl.load_workbook(self.recalculated_path, data_only=True)
            else:
                self._wb_recalc = self._wb_cached

        return self._wb_orig, self._wb_cached, self._wb_recalc

    def get_cell_triple_state(self, sheet_name: str, coord: str) -> Dict[str, Any]:
        """Returns the formula expression, saved cached value, and newly recalculated value for a cell."""
        wb_orig, wb_cached, wb_recalc = self.get_openpyxl_workbooks()

        s_orig = wb_orig[sheet_name] if sheet_name in wb_orig.sheetnames else wb_orig.active
        s_cached = wb_cached[sheet_name] if sheet_name in wb_cached.sheetnames else wb_cached.active
        s_recalc = wb_recalc[sheet_name] if sheet_name in wb_recalc.sheetnames else wb_recalc.active

        f_expr = s_orig[coord].value
        c_val = s_cached[coord].value
        r_val = s_recalc[coord].value

        return {
            "formula_expr": f_expr if isinstance(f_expr, str) and f_expr.startswith("=") else None,
            "cached_val": c_val,
            "recalculated_val": r_val,
            "is_formula": isinstance(f_expr, str) and f_expr.startswith("=")
        }

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Layout-Invariant Search & Semantic Resolvers
    # ─────────────────────────────────────────────────────────────────────────
    def resolve_sheet(self, target_name: Optional[str] = None, anchor_hint: Optional[str] = None) -> Optional[str]:
        """Resolves worksheet by exact name, case-insensitive, fuzzy match, or semantic content hint."""
        if not self.worksheets:
            return None

        # Tier 1: Target sheet specified exactly
        if target_name and target_name in self.worksheets:
            return target_name

        # Tier 2: Case-insensitive match
        if target_name:
            t_lower = target_name.strip().lower()
            for s_name in self.worksheets:
                if s_name.strip().lower() == t_lower:
                    return s_name

        # Tier 3: Substring / Fuzzy match
        if target_name:
            t_lower = target_name.strip().lower()
            for s_name in self.worksheets:
                s_lower = s_name.strip().lower()
                if t_lower in s_lower or s_lower in t_lower:
                    return s_name

        # Tier 4: Fallback to first visible sheet
        for s_name, s_info in self.worksheets.items():
            if s_info.get("state") == "visible":
                return s_name

        return list(self.worksheets.keys())[0]

    def get_table(self, table_name_or_column: str) -> Optional[Dict[str, Any]]:
        """Finds an Excel Table by logical name, display name, or contained column name."""
        t_clean = table_name_or_column.strip().lower()

        # Check by table name
        for t in self.tables:
            if t["name"].lower() == t_clean or t["display_name"].lower() == t_clean:
                return t

        # Check by contained column
        for t in self.tables:
            if any(col.strip().lower() == t_clean for col in t["columns"]):
                return t

        return None

    def get_pivots_for_sheet(self, sheet_name: str) -> List[Dict[str, Any]]:
        """Returns all PivotTable objects belonging to a specific worksheet."""
        s_clean = sheet_name.strip().lower()
        return [pt for pt in self.pivot_tables if pt["sheet"].strip().lower() == s_clean]

    def close(self):
        """Closes openpyxl instances if loaded."""
        try:
            if self._wb_orig:
                self._wb_orig.close()
            if self._wb_cached:
                self._wb_cached.close()
            if self._wb_recalc:
                self._wb_recalc.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
