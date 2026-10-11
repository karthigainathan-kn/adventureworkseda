#!/usr/bin/env python3
"""Read-only local SQL Server EDA report generator for this workspace."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / "dab-config.json"
REPORT_DIR = ROOT / "reports" / "eda"
SENSITIVE = re.compile(
    r"password|hash|salt|credit.?card|card.?number|ssn|social.?security|"
    r"birth.?date|date.?of.?birth|email|phone|address|first.?name|last.?name|"
    r"middle.?name|demographic|medical|health|diagnos|patient|gender",
    re.IGNORECASE,
)
WATERMARK = re.compile(r"created|modified|updated|ingest|load|rowversion|timestamp", re.I)
MEASURES = re.compile(r"price|cost|qty|quantity|total|rate|weight|amount|hours|days|percentage", re.I)
CATEGORY = re.compile(r"category|status|class|color|size|type$|flag$|code$|shift|country|region|province|territory|gender|unit.?measure|group$", re.I)
SAFE_CATEGORY_VALUES = re.compile(r"^gender$|unit.?measure|country|region|province|territory|group$", re.I)
CATEGORY_ENUM_CAP = 250
CATEGORY_TOP_LIMIT = 20
GRAIN_NOTES = {
    "Person.Person": "One row per person/business entity. PersonID is the declared key; one person can have multiple email, phone, or address-link rows.",
    "Person.Address": "One row per postal address. AddressID is the declared key; addresses can be linked to many business entities.",
    "Person.BusinessEntityAddress": "One row per business-entity/address association, with an address type. This is a link table, not one row per address.",
    "Person.EmailAddress": "One row per email-address record for a person; a person can have multiple email rows.",
    "Person.PersonPhone": "One row per person and phone-number-type association; a person can have multiple phone rows.",
    "Person.CountryRegion": "One reference row per country/region code.",
    "Person.StateProvince": "One reference row per state/province; linked to a country/region.",
    "Sales.Customer": "One row per customer account. CustomerID is the key; PersonID may be nullable. A customer can have multiple sales orders.",
    "Sales.SalesOrderHeader": "One row per sales order header. SalesOrderID is the key; CustomerID links the order to its customer.",
    "Sales.SalesOrderDetail": "One row per sales-order line item. The order may have many detail rows; SalesOrderID links each line to its header.",
    "Sales.SalesOrderHeaderSalesReason": "One row per sales-order/reason association; an order can have multiple reason rows.",
    "Sales.PersonCreditCard": "One row per person/card association. The card value itself is deliberately not profiled or exported.",
    "HumanResources.Employee": "One row per employee, keyed by the employee's BusinessEntityID. An employee can have multiple pay-history and department-history rows.",
    "HumanResources.EmployeePayHistory": "One row per employee pay-rate event/effective date; an employee can have multiple history rows.",
    "HumanResources.EmployeeDepartmentHistory": "One row per employee department/shift assignment period; an employee can have multiple history rows.",
    "Production.TransactionHistory": "One row per inventory transaction event, keyed by TransactionID.",
    "Production.TransactionHistoryArchive": "One archived inventory-transaction row per archive key; treat as historical/archive grain.",
    "Production.BillOfMaterials": "One row per bill-of-materials component/effective record, keyed by BillOfMaterialsID; one assembly can contain multiple components.",
    "Purchasing.PurchaseOrderHeader": "One row per purchase order header; a purchase order can have multiple detail lines.",
    "Purchasing.PurchaseOrderDetail": "One row per purchase-order line item, linked to its order header.",
    "Purchasing.ProductVendor": "One row per product/vendor association; a product or vendor can participate in multiple associations.",
    "Sales.CountryRegionCurrency": "One row per country/region and currency association.",
    "Production.ProductProductPhoto": "One row per product/photo association; either side can appear in multiple associations.",
    "Production.ProductModelProductDescriptionCulture": "One row per product-model, description, and culture association.",
}
DISTINCT_TYPES = {
    "bigint", "binary", "bit", "char", "date", "datetime", "datetime2",
    "datetimeoffset", "decimal", "float", "int", "money", "nchar", "numeric",
    "nvarchar", "real", "smalldatetime", "smallint", "smallmoney", "time",
    "tinyint", "uniqueidentifier", "varbinary", "varchar",
}
NUMERIC_TYPES = {
    "bigint", "bit", "decimal", "float", "int", "money", "numeric", "real",
    "smallint", "smallmoney", "tinyint",
}
DATE_TYPES = {"date", "datetime", "datetime2", "datetimeoffset", "smalldatetime"}


def bracket(value: str) -> str:
    return "[" + value.replace("]", "]]" ) + "]"


def run_query(sql: str) -> list[list[str]]:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    connection = config["data-source"]["connection-string"]
    values = dict(re.findall(r"([^;=]+)=([^;]*)", connection))
    database = values.get("Database") or values.get("Initial Catalog")
    user = values.get("User Id") or values.get("UID")
    password = values.get("Password") or values.get("PWD")
    if not database or not user or not password:
        raise RuntimeError("dab-config.json does not contain a SQL database, user, and password")

    environment = os.environ.copy()
    environment["SQLCMDPASSWORD"] = password
    command = [
        "docker", "exec", "-e", "SQLCMDPASSWORD", "sqlserver", "/opt/mssql-tools18/bin/sqlcmd",
        "-S", "localhost", "-d", database, "-U", user, "-C", "-I", "-b",
        "-h", "-1", "-W", "-w", "65535", "-s", "|", "-Q",
        "SET NOCOUNT ON; " + sql,
    ]
    completed = subprocess.run(command, env=environment, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    rows = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("(") or line.startswith("Sqlcmd:"):
            continue
        rows.append([value.strip() for value in line.split("|")])
    return rows


def run_union(selects: list[str], max_chars: int = 28000) -> list[list[str]]:
    output = []
    batch = []
    batch_size = 0
    for select in selects:
        extra = len(select) + (len(" UNION ALL ") if batch else 0)
        if batch and batch_size + extra > max_chars:
            output.extend(run_query(" UNION ALL ".join(batch)))
            batch = []
            batch_size = 0
        batch.append(select)
        batch_size += len(select) + (len(" UNION ALL ") if len(batch) > 1 else 0)
    if batch:
        output.extend(run_query(" UNION ALL ".join(batch)))
    return output


def number(value: str | None) -> int | None:
    if value in (None, "", "NULL"):
        return None
    return int(value)


def s(value: str | None) -> str:
    return "" if value in (None, "NULL") else value


def qname(schema: str, name: str) -> str:
    return f"{bracket(schema)}.{bracket(name)}"


def build_workbook(path: Path, sheets: list[tuple[str, list[list[object]]]]) -> None:
    def col_name(index: int) -> str:
        output = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            output = chr(65 + remainder) + output
        return output

    content_types = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                     '<Default Extension="xml" ContentType="application/xml"/>',
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    workbook_sheets = []
    workbook_rels = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for sheet_number, (sheet_name, rows) in enumerate(sheets, 1):
            content_types.append(
                f'<Override PartName="/xl/worksheets/sheet{sheet_number}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
            workbook_sheets.append(f'<sheet name="{escape(sheet_name)}" sheetId="{sheet_number}" r:id="rId{sheet_number}"/>')
            workbook_rels.append(
                f'<Relationship Id="rId{sheet_number}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{sheet_number}.xml"/>'
            )
            max_columns = max((len(row) for row in rows), default=1)
            last_ref = f"{col_name(max_columns)}{max(1, len(rows))}"
            columns = "".join(
                f'<col min="{index}" max="{index}" width="{min(54, max(12, max((len(str(row[index-1])) for row in rows if len(row) >= index), default=12) + 2))}" customWidth="1"/>'
                for index in range(1, max_columns + 1)
            )
            row_xml = []
            for row_number, row in enumerate(rows, 1):
                cells = []
                for column_number, value in enumerate(row, 1):
                    reference = f"{col_name(column_number)}{row_number}"
                    if value is None or value == "":
                        cells.append(f'<c r="{reference}"/>')
                    elif isinstance(value, bool):
                        cells.append(f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>')
                    elif isinstance(value, (int, float)) and not (isinstance(value, float) and not math.isfinite(value)):
                        cells.append(f'<c r="{reference}"><v>{value}</v></c>')
                    else:
                        text = str(value)[:32767]
                        if text.startswith(("=", "+", "-", "@")):
                            text = "'" + text
                        style = ' s="1"' if row_number == 1 else ' s="2"'
                        cells.append(
                            f'<c r="{reference}" t="inlineStr"{style}>'
                            f'<is><t xml:space="preserve">{escape(text)}</t></is></c>'
                        )
                row_xml.append(f'<row r="{row_number}" ht="36" customHeight="1">{"".join(cells)}</row>')
            filter_xml = f'<autoFilter ref="A1:{last_ref}"/>' if len(rows) > 1 else ""
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                f'<cols>{columns}</cols><sheetData>{"".join(row_xml)}</sheetData>{filter_xml}</worksheet>'
            )
            archive.writestr(f"xl/worksheets/sheet{sheet_number}.xml", xml)
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' + "".join(content_types) + "</Types>")
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + "".join(workbook_sheets) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(workbook_rels) + '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("xl/styles.xml", '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="10"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF234E70"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def main() -> int:
    if not CONFIG.exists():
        raise RuntimeError(f"Missing local config: {CONFIG}")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-UTC")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    connection = config["data-source"]["connection-string"]
    database_match = re.search(r"(?:Database|Initial Catalog)=([^;]+)", connection, re.I)
    database = database_match.group(1) if database_match else "database"

    object_rows = run_query("""
        SELECT s.name, o.name, o.type_desc
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
        WHERE o.is_ms_shipped=0 AND o.type IN ('U','V','P')
        ORDER BY CASE o.type WHEN 'U' THEN 1 WHEN 'V' THEN 2 ELSE 3 END,s.name,o.name
    """)
    objects = [{"schema": r[0], "name": r[1], "type": r[2]} for r in object_rows]
    tables_views = [o for o in objects if o["type"] in ("USER_TABLE", "VIEW")]
    tables = [o for o in objects if o["type"] == "USER_TABLE"]
    views = [o for o in objects if o["type"] == "VIEW"]
    procedures = [o for o in objects if "PROCEDURE" in o["type"]]

    columns_rows = run_query("""
        SELECT s.name,o.name,o.type_desc,c.name,ty.name,c.max_length,c.precision,c.scale,
               c.is_nullable,c.is_identity,c.is_computed,
               CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id WHERE i.object_id=c.object_id AND i.is_primary_key=1 AND ic.column_id=c.column_id AND ic.key_ordinal>0) THEN 1 ELSE 0 END,
               CASE WHEN dc.object_id IS NULL THEN 0 ELSE 1 END,
               CASE WHEN ty.name IN ('timestamp','rowversion') THEN 1 ELSE 0 END,
               COALESCE(bt.name,ty.name)
        FROM sys.columns c JOIN sys.objects o ON o.object_id=c.object_id
        JOIN sys.schemas s ON s.schema_id=o.schema_id JOIN sys.types ty ON ty.user_type_id=c.user_type_id
        LEFT JOIN sys.types bt ON bt.user_type_id=ty.system_type_id AND bt.is_user_defined=0
        LEFT JOIN sys.default_constraints dc ON dc.object_id=c.default_object_id
        WHERE o.is_ms_shipped=0 AND o.type IN ('U','V')
        ORDER BY s.name,o.name,c.column_id
    """)
    columns = []
    columns_by_object = defaultdict(list)
    for r in columns_rows:
        col = {"schema": r[0], "object": r[1], "object_type": r[2], "name": r[3], "type": r[4],
               "max_length": number(r[5]), "precision": number(r[6]), "scale": number(r[7]),
               "nullable": r[8] == "1", "identity": r[9] == "1", "computed": r[10] == "1",
               "pk": r[11] == "1", "default": r[12] == "1", "rowversion": r[13] == "1", "base_type": r[14]}
        columns.append(col)
        columns_by_object[(col["schema"], col["object"])].append(col)

    params_rows = run_query("""
        SELECT s.name,p.name,pr.name,p.parameter_id,ty.name,p.max_length,p.precision,p.scale,p.is_output,p.is_nullable
        FROM sys.parameters p JOIN sys.procedures pr ON pr.object_id=p.object_id
        JOIN sys.schemas s ON s.schema_id=pr.schema_id JOIN sys.types ty ON ty.user_type_id=p.user_type_id
        WHERE pr.is_ms_shipped=0 AND p.parameter_id>0 ORDER BY s.name,pr.name,p.parameter_id
    """)
    fk_rows = run_query("""
        SELECT fk.name,ps.name,pt.name,pc.name,rs.name,rt.name,rc.name,fkc.constraint_column_id,fk.delete_referential_action_desc,fk.update_referential_action_desc
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id
        JOIN sys.tables pt ON pt.object_id=fk.parent_object_id JOIN sys.schemas ps ON ps.schema_id=pt.schema_id
        JOIN sys.columns pc ON pc.object_id=pt.object_id AND pc.column_id=fkc.parent_column_id
        JOIN sys.tables rt ON rt.object_id=fk.referenced_object_id JOIN sys.schemas rs ON rs.schema_id=rt.schema_id
        JOIN sys.columns rc ON rc.object_id=rt.object_id AND rc.column_id=fkc.referenced_column_id
        ORDER BY ps.name,pt.name,fk.name,fkc.constraint_column_id
    """)
    fk_groups = defaultdict(list)
    for r in fk_rows:
        fk_groups[r[0]].append(r)
    relationships = []
    for name, rows in fk_groups.items():
        first = rows[0]
        relationships.append({"name": name, "from_schema": first[1], "from_table": first[2],
                              "to_schema": first[4], "to_table": first[5],
                              "from_columns": [r[3] for r in rows], "to_columns": [r[6] for r in rows],
                              "delete": first[8], "update": first[9]})

    unique_rows = run_query("""
        SELECT s.name,t.name,i.name,i.is_primary_key,i.is_unique_constraint,
               STRING_AGG(c.name,',') WITHIN GROUP (ORDER BY ic.key_ordinal),i.is_disabled
        FROM sys.indexes i JOIN sys.tables t ON t.object_id=i.object_id
        JOIN sys.schemas s ON s.schema_id=t.schema_id JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id AND ic.key_ordinal>0
        JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
        WHERE i.is_unique=1 AND i.index_id>0 GROUP BY s.name,t.name,i.name,i.is_primary_key,i.is_unique_constraint,i.is_disabled
        ORDER BY s.name,t.name,i.name
    """)
    check_rows = run_query("""
        SELECT s.name,t.name,cc.name,cc.is_disabled,cc.is_not_trusted
        FROM sys.check_constraints cc JOIN sys.tables t ON t.object_id=cc.parent_object_id
        JOIN sys.schemas s ON s.schema_id=t.schema_id ORDER BY s.name,t.name,cc.name
    """)

    count_selects = [
        f"SELECT N'{o['schema'].replace(chr(39), chr(39)*2)}',N'{o['name'].replace(chr(39), chr(39)*2)}',COUNT_BIG(*) FROM {qname(o['schema'], o['name'])}"
        for o in tables_views
    ]
    count_rows = run_union(count_selects)
    counts = {(r[0], r[1]): number(r[2]) for r in count_rows}

    stat_selects = []
    for obj in tables_views:
        obj_cols = columns_by_object[(obj["schema"], obj["name"])]
        if not obj_cols:
            continue
        aggregate = ["COUNT_BIG(*) AS total_rows"]
        value_rows = []
        for index, col in enumerate(obj_cols):
            ident = bracket(col["name"])
            aggregate.append(f"COALESCE(SUM(CONVERT(bigint,CASE WHEN {ident} IS NULL THEN 1 ELSE 0 END)),0) AS n{index}")
            if col["base_type"].lower() in DISTINCT_TYPES and col["max_length"] != -1:
                aggregate.append(f"COUNT_BIG(DISTINCT {ident}) AS d{index}")
                distinct_value = f"a.d{index}"
            else:
                aggregate.append(f"CONVERT(bigint,NULL) AS d{index}")
                distinct_value = f"a.d{index}"
            value_rows.append(f"(N'{col['name'].replace(chr(39), chr(39)*2)}',a.n{index},{distinct_value})")
        table_name = qname(obj["schema"], obj["name"])
        stat_selects.append(
            f"SELECT N'{obj['schema']}',N'{obj['name']}',v.col,a.total_rows,v.nulls,v.distincts FROM (SELECT {','.join(aggregate)} FROM {table_name}) a CROSS APPLY (VALUES {','.join(value_rows)}) v(col,nulls,distincts)"
        )
    quality_rows = run_union(stat_selects)
    quality = []
    for r in quality_rows:
        total, nulls, distinct = number(r[3]) or 0, number(r[4]) or 0, number(r[5])
        quality.append({"schema": r[0], "object": r[1], "column": r[2], "rows": total,
                        "nulls": nulls, "null_pct": round(nulls * 100 / total, 3) if total else 0,
                        "distinct": distinct})

    date_selects = []
    numeric_selects = []
    category_selects = []
    for obj in tables_views:
        obj_cols = columns_by_object[(obj["schema"], obj["name"])]
        temporal = [c for c in obj_cols if c["base_type"].lower() in DATE_TYPES and not re.search(r"birth.?date|date.?of.?birth|\bdob\b", c["name"], re.I)]
        for col in temporal:
            ident = bracket(col["name"])
            date_selects.append(
                f"SELECT N'{obj['schema']}',N'{obj['name']}',N'{col['name']}',COUNT_BIG(*),COUNT_BIG({ident}),CONVERT(nvarchar(40),MIN({ident}),126),CONVERT(nvarchar(40),MAX({ident}),126) FROM {qname(obj['schema'],obj['name'])}"
            )
        measure_cols = [c for c in obj_cols if c["base_type"].lower() in NUMERIC_TYPES and not c["pk"] and not SENSITIVE.search(c["name"] + " " + obj["name"]) and not re.search(r"(^|_)id$|id$|rowguid", c["name"], re.I)]
        if measure_cols:
            aggregate = []
            value_rows = []
            for index, col in enumerate(measure_cols):
                ident = bracket(col["name"])
                aggregate_value = f"CONVERT(int,{ident})" if col["base_type"].lower() == "bit" else ident
                aggregate.extend([f"MIN({aggregate_value}) AS lo{index}", f"MAX({aggregate_value}) AS hi{index}", f"AVG(TRY_CONVERT(float,{ident})) AS av{index}"])
                value_rows.append(f"(N'{col['name'].replace(chr(39), chr(39)*2)}',a.lo{index},a.hi{index},a.av{index})")
            numeric_selects.append(
                f"SELECT N'{obj['schema']}',N'{obj['name']}',v.col,v.lo,v.hi,v.av FROM (SELECT {','.join(aggregate)} FROM {qname(obj['schema'],obj['name'])}) a CROSS APPLY (VALUES {','.join(value_rows)}) v(col,lo,hi,av)"
            )
        for col in obj_cols:
            lname = col["name"].lower()
            if col["base_type"].lower() not in {"char", "nchar", "varchar", "nvarchar"} or col["max_length"] == -1:
                continue
            include_values = bool(SAFE_CATEGORY_VALUES.search(lname))
            if not (CATEGORY.search(lname) or include_values) or (SENSITIVE.search(lname + " " + obj["name"]) and not include_values):
                continue
            ident = bracket(col["name"])
            label = col["name"].replace("'", "''")
            buckets_sql = (
                f"SELECT ROW_NUMBER() OVER (ORDER BY category_count DESC,category_value) AS category_rank,category_value,category_count,"
                f"COUNT_BIG(*) OVER() AS distinct_categories,SUM(category_count) OVER() AS total_nonnull "
                f"FROM (SELECT REPLACE(REPLACE(REPLACE(CONVERT(nvarchar(200),{ident}),N'|',N'/'),CHAR(10),N' '),CHAR(13),N' ') AS category_value,"
                f"COUNT_BIG(*) AS category_count FROM {qname(obj['schema'],obj['name'])} WHERE {ident} IS NOT NULL GROUP BY {ident}) buckets"
            )
            category_selects.append(
                f"SELECT N'{obj['schema']}',N'{obj['name']}',N'{label}',category_rank,category_value,category_count,distinct_categories,total_nonnull,N'VALUE' "
                f"FROM ({buckets_sql}) ranked WHERE distinct_categories<={CATEGORY_ENUM_CAP} OR category_rank<={CATEGORY_TOP_LIMIT} "
                f"UNION ALL SELECT N'{obj['schema']}',N'{obj['name']}',N'{label}',{CATEGORY_TOP_LIMIT + 1},N'(Other categories)',SUM(category_count),"
                f"MAX(distinct_categories),MAX(total_nonnull),N'OTHER' FROM ({buckets_sql}) ranked "
                f"WHERE distinct_categories>{CATEGORY_ENUM_CAP} AND category_rank>{CATEGORY_TOP_LIMIT} HAVING SUM(category_count)>0"
            )
    dates_rows = run_union(date_selects) if date_selects else []
    dates = [{"schema": r[0], "object": r[1], "column": r[2], "rows": number(r[3]) or 0,
              "nonnull": number(r[4]) or 0, "min": s(r[5]) if len(r) > 5 else "",
              "max": s(r[6]) if len(r) > 6 else ""} for r in dates_rows]
    numeric_rows = run_union(numeric_selects) if numeric_selects else []
    category_rows = run_union(category_selects) if category_selects else []

    orphan_selects = []
    for rel in relationships:
        mappings = fk_groups[rel["name"]]
        child, parent = qname(rel["from_schema"], rel["from_table"]), qname(rel["to_schema"], rel["to_table"])
        all_present = " AND ".join(f"c.{bracket(r[3])} IS NOT NULL" for r in mappings)
        join = " AND ".join(f"p.{bracket(r[6])}=c.{bracket(r[3])}" for r in mappings)
        orphan_selects.append(
            f"SELECT N'{rel['name']}',N'{rel['from_schema']}.{rel['from_table']} -> {rel['to_schema']}.{rel['to_table']}',COUNT_BIG(*) FROM {child} c WHERE {all_present} AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join})"
        )
    orphan_rows = run_union(orphan_selects) if orphan_selects else []

    cols_by_table = defaultdict(list)
    for col in columns:
        cols_by_table[(col["schema"], col["object"])].append(col)
    table_pks = defaultdict(list)
    for col in columns:
        if col["pk"]:
            table_pks[(col["schema"], col["object"])].append(col["name"])
    fk_by_column = defaultdict(list)
    unique_child_keys = set()
    for unique in unique_rows:
        if unique[6] != "1":
            unique_child_keys.add((unique[0], unique[1], tuple(sorted(unique[5].split(",")))))
    for rel in relationships:
        for source_column, target_column in zip(rel["from_columns"], rel["to_columns"]):
            fk_by_column[(rel["from_schema"], rel["from_table"], source_column)].append(
                f"{rel['to_schema']}.{rel['to_table']}.{target_column} ({rel['name']})"
            )

    def type_label(col: dict) -> str:
        sql_type = col["base_type"].lower()
        length = col["max_length"]
        if sql_type in {"nvarchar", "nchar"} and length not in (-1, None):
            length //= 2
        if sql_type in {"varchar", "char", "varbinary", "binary", "nvarchar", "nchar"}:
            detail = "MAX" if length == -1 else str(length)
            result = f"{sql_type}({detail})"
        elif sql_type in {"decimal", "numeric"}:
            result = f"{sql_type}({col['precision']},{col['scale']})"
        elif sql_type in {"datetime2", "datetimeoffset", "time"}:
            result = f"{sql_type}({col['scale']})"
        else:
            result = sql_type
        if col["type"].lower() != sql_type:
            result += f" (alias type {col['type']})"
        if col["rowversion"]:
            result += "; binary change token, not a date"
        return result

    def type_dimensions(col: dict) -> tuple[object, str, object, object]:
        sql_type = col["base_type"].lower()
        raw_length = col["max_length"]
        length: object = ""
        length_kind = "Not length-based"
        if sql_type in {"char", "nchar", "varchar", "nvarchar", "binary", "varbinary"}:
            if raw_length == -1:
                length = "MAX"
                length_kind = "Variable; MAX"
            else:
                length = raw_length // 2 if sql_type in {"nchar", "nvarchar"} else raw_length
                unit = "characters" if sql_type in {"char", "nchar", "varchar", "nvarchar"} else "bytes"
                length_kind = ("Variable" if sql_type in {"varchar", "nvarchar", "varbinary"} else "Fixed") + f"; {unit}"
        elif sql_type in {"xml", "text", "ntext", "image"}:
            length = "MAX" if raw_length == -1 else raw_length
            length_kind = "Variable; large-object type"
        precision = col["precision"] if sql_type in {"decimal", "numeric"} else ""
        scale = col["scale"] if sql_type in {"decimal", "numeric", "datetime2", "datetimeoffset", "time"} else ""
        return length, length_kind, precision, scale

    def grain_for(obj: dict) -> str:
        full_name = f"{obj['schema']}.{obj['name']}"
        if full_name in GRAIN_NOTES:
            return GRAIN_NOTES[full_name]
        pks = table_pks.get((obj["schema"], obj["name"]), [])
        if pks:
            return f"One row per unique declared key combination ({', '.join(pks)}); business meaning needs owner confirmation."
        return "Row grain is not established: no declared primary key was found."

    headers = ["Section", "Schema", "Object", "Column / key", "SQL type / relationship", "Row grain / plain-language meaning", "Total rows", "Null rows", "Null %", "Distinct non-null", "Finding / pattern", "How to read it / evidence"]
    findings = [headers]
    runtime_limitations = []

    def add(section: str, schema: object = "", obj: object = "", column: object = "", detail: object = "",
            grain: object = "", rows: object = "", nulls: object = "", null_pct: object = "",
            distinct: object = "", finding: object = "", explanation: object = "") -> None:
        findings.append([section, schema, obj, column, detail, grain, rows, nulls, null_pct, distinct, finding, explanation])

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    add("RUN OVERVIEW", "Database", database, finding="AdventureWorks database EDA", explanation=f"Read-only run at {now}; counts and profile metrics are aggregates, no raw person-level rows exported.")
    add("RUN OVERVIEW", finding=f"{len(tables)} tables; {len(views)} views; {len(procedures)} stored procedures; {len(columns)} fields; {len(relationships)} declared foreign keys.", explanation="Tables and views counted; procedure definitions were not executed.")
    add("HOW TO READ THIS SHEET", column="Row grain", finding="What one row represents", explanation="Example: Sales.SalesOrderHeader is one row per order; Sales.SalesOrderDetail is one row per order line, so one order can have many detail rows.")
    add("HOW TO READ THIS SHEET", column="Distinct non-null", finding="Unique values in that column", explanation="For PersonID, this is the number of different non-null person IDs in that table/view. It is not a count of source rows and does not prove uniqueness unless the column is a declared key.")
    add("HOW TO READ THIS SHEET", column="SQL type", finding="Readable SQL notation", explanation="Examples: nvarchar(50) means at most 50 Unicode characters; decimal(19,4) means 19 total digits with 4 after the decimal; NULL allowed is separate from the type.")

    for obj in objects:
        key = (obj["schema"], obj["name"])
        row_count = counts.get(key) if obj["type"] in ("USER_TABLE", "VIEW") else None
        if obj["type"] in ("USER_TABLE", "VIEW"):
            add("OBJECT INVENTORY", obj["schema"], obj["name"], detail=obj["type"], grain=grain_for(obj), rows=row_count,
                finding="Exact COUNT_BIG(*)" if row_count is not None else "Not counted",
                explanation="Views are counted as returned rows; stored procedures are listed but never executed." if obj["type"] == "VIEW" else "Physical user table; row count is exact for this read-only query.")
        else:
            add("OBJECT INVENTORY", obj["schema"], obj["name"], detail="STORED PROCEDURE", finding="Inventoried, not executed",
                explanation="Stored-procedure parameters appear in the column/type section; no procedure body was run.")

    quality_by_field = {(q["schema"], q["object"], q["column"]): q for q in quality}
    for col in columns:
        key = (col["schema"], col["object"])
        profile = quality_by_field.get((col["schema"], col["object"], col["name"]))
        roles = []
        if col["pk"]:
            roles.append("PK")
        references = fk_by_column.get((col["schema"], col["object"], col["name"]), [])
        roles.extend("FK -> " + reference for reference in references)
        role_text = "; ".join(roles) if roles else "No declared PK/FK role"
        count = counts.get(key)
        if profile:
            nulls = profile["nulls"]
            null_pct = profile["null_pct"]
            distinct = profile["distinct"]
        else:
            nulls = null_pct = distinct = None
        note = ""
        if col["name"].lower() == "personid":
            note = "PersonID summary: distinct count is distinct people represented; compare with total rows and nulls to see repeats or missing links."
        elif col["name"].lower() in {"addressid", "countryregioncode", "customerid", "salesorderid"}:
            note = "Key/reference coverage summary; interpret distinct count as the number of represented IDs/codes, not source records."
        if SENSITIVE.search(col["name"] + " " + col["object"]):
            note = (note + " " if note else "") + "Potential sensitive field from metadata name; values were not read or exported."
        add("FIELD PROFILE + TYPE", col["schema"], col["object"], col["name"],
            type_label(col) + ("; NULL allowed" if col["nullable"] else "; NOT NULL") + "; " + role_text,
            grain_for({"schema": col["schema"], "name": col["object"]}), count, nulls, null_pct, distinct,
            "Declared PK/FK" if col["pk"] and references else "Declared PK" if col["pk"] else "Declared FK" if references else "Profiled column",
            note or "Exact row/null/distinct counts; distinct is COUNT_BIG(DISTINCT column) where SQL type supports it.")

    for r in params_rows:
        proc = next((o for o in procedures if o["schema"] == r[0] and o["name"] == r[1]), None)
        grain = "Procedure metadata only; procedure not executed."
        add("PROCEDURE PARAMETER", r[0], r[1], r[2], f"{r[4]} (input/output metadata available)", grain=grain,
            finding="Stored-procedure parameter", explanation="Parameters are inventoried for reference only.")

    for rel in relationships:
        parent = f"{rel['to_schema']}.{rel['to_table']}"
        child = f"{rel['from_schema']}.{rel['from_table']}"
        child_fk_unique = (rel["from_schema"], rel["from_table"], tuple(sorted(rel["from_columns"]))) in unique_child_keys
        cardinality = "one-to-one (child FK columns have a unique index)" if child_fk_unique else "one-to-many (child FK columns are not unique)"
        add("RELATIONSHIP", rel["from_schema"], rel["from_table"], ", ".join(rel["from_columns"]),
            f"FK -> {parent} ({', '.join(rel['to_columns'])})", grain=f"{parent} is the referenced parent; {child} is the child table.",
            finding=f"Declared {cardinality} path", explanation=f"Constraint: {rel['name']}; delete={rel['delete']}; update={rel['update']}.")

    for r in orphan_rows:
        orphan_count = number(r[2]) if len(r) > 2 else None
        add("FK QUALITY", obj=r[1], column=r[0], rows=orphan_count, finding="Orphan rows on declared FK", explanation="0 means no non-null FK values lacked a matching parent in this check.")

    for r in unique_rows:
        add("CONSTRAINT METADATA", r[0], r[1], r[5], detail=r[2], finding="Declared unique index",
            explanation=f"Primary key={r[3] == '1'}; unique constraint={r[4] == '1'}; disabled={r[6] == '1'}.")
    for r in check_rows:
        add("CONSTRAINT METADATA", r[0], r[1], r[2], finding="Declared CHECK constraint",
            explanation=f"Disabled={r[3] == '1'}; untrusted={r[4] == '1'}. Constraint expression was not evaluated separately.")

    for item in quality:
        if item["distinct"] is None:
            continue
        if item["distinct"] == item["rows"] - item["nulls"] and item["rows"] > 1:
            continue
        add("DISTINCT / NULL REVIEW", item["schema"], item["object"], item["column"], rows=item["rows"], nulls=item["nulls"], null_pct=item["null_pct"], distinct=item["distinct"],
            finding="Repeated non-null values or NULLs present", explanation="Review whether repetition/nulls are expected for this field; only a declared key/unique rule makes repetition a confirmed defect.")

    for r in unique_rows:
        add("UNIQUE KEY METADATA", r[0], r[1], r[5], r[2], finding="Declared unique index" if r[4] == "1" else "Declared primary/unique index",
            explanation=f"primary_key={r[3] == '1'}; unique_constraint={r[4] == '1'}; disabled={r[6] == '1'}.")
    for r in check_rows:
        add("CHECK CONSTRAINT METADATA", r[0], r[1], r[2], finding="Declared CHECK constraint",
            explanation=f"disabled={r[3] == '1'}; trusted={r[4] != '1'}. Constraint expression not executed as a separate EDA test.")

    date_by_key = {(d["schema"], d["object"], d["column"]): d for d in dates}
    for obj in tables_views:
        for col in cols_by_table[(obj["schema"], obj["name"])]:
            if col["base_type"].lower() not in DATE_TYPES and not col["rowversion"]:
                continue
            profile = date_by_key.get((obj["schema"], obj["name"], col["name"]))
            is_candidate = bool(WATERMARK.search(col["name"]))
            if col["rowversion"]:
                add("INCREMENTAL LOAD", obj["schema"], obj["name"], col["name"], type_label(col), grain_for(obj), counts.get((obj["schema"], obj["name"])),
                    finding="rowversion candidate; not a date", explanation="Binary change token; confirm retention/consumption and delete capture before relying on it.")
            elif profile:
                null_pct = round((profile["rows"] - profile["nonnull"]) * 100 / profile["rows"], 3) if profile["rows"] else 0
                add("INCREMENTAL LOAD", obj["schema"], obj["name"], col["name"], type_label(col), grain_for(obj), profile["rows"], profile["rows"] - profile["nonnull"], null_pct,
                    finding="Candidate by name" if is_candidate else "Date field; watermark role not established",
                    explanation=f"Min={profile['min'] or 'not available'}; max={profile['max'] or 'not available'}. Update semantics, timezone, late arrivals, and delete capture unverified.")
        date_cols = [c for c in cols_by_table[(obj["schema"], obj["name"])] if c["base_type"].lower() in DATE_TYPES]
        if not any(WATERMARK.search(c["name"]) for c in date_cols):
            add("INCREMENTAL LOAD", obj["schema"], obj["name"], finding="No created/updated/ingestion timestamp candidate", rows=counts.get((obj["schema"], obj["name"])),
                explanation="Candidate search uses names plus date/time types; does not rule out CDC, change tracking, or an undocumented watermark.")

    for r in numeric_rows:
        if len(r) < 5:
            runtime_limitations.append(["Numeric patterns", "One or more numeric aggregate rows had an unexpected SQLCMD shape and were omitted."])
            continue
        average = r[5] if len(r) > 5 and r[5] else "not available (no non-null values)"
        if all(value in ("", "NULL") for value in r[3:]):
            add("ANONYMIZED PATTERN", r[0], r[1], r[2], rows=counts.get((r[0], r[1])), finding="No numeric values to summarize",
                explanation="Table is empty or this column has no non-null numeric values.")
        else:
            add("ANONYMIZED PATTERN", r[0], r[1], r[2], rows=counts.get((r[0], r[1])), finding="Numeric min / max / average",
                explanation=f"{r[3]} / {r[4]} / {average}; aggregate only, no raw values.")
    for r in category_rows:
        if len(r) < 9:
            continue
        category_value = r[4]
        category_label = category_value
        if r[2].lower() == "gender" and category_value in {"M", "F"}:
            category_label = {"M": "Male", "F": "Female"}[category_value] + f" (stored code {category_value})"
        nonnull_total = number(r[7]) or 0
        category_count = number(r[5]) or 0
        category_pct = round(category_count * 100 / nonnull_total, 3) if nonnull_total else 0
        kind_note = "Other is the combined count for categories beyond the displayed top values." if r[8] == "OTHER" else "Distinct category value; aggregate count only, no person-level rows."
        add("CATEGORICAL VALUE FREQUENCY", r[0], r[1], r[2], detail=category_label,
            rows=counts.get((r[0], r[1])), distinct=number(r[6]),
            finding=f"{category_count} rows ({category_pct}% of non-null)",
            explanation=f"Non-null total={nonnull_total}; category rank={r[3]}; {kind_note}")
    linked_pairs = set()
    for rel in relationships:
        left = (rel["from_schema"], rel["from_table"])
        right = (rel["to_schema"], rel["to_table"])
        linked_pairs.add((left, right))
    for left, right in linked_pairs:
        left_dates = {d["column"]: d for d in dates if (d["schema"], d["object"]) == left and WATERMARK.search(d["column"])}
        right_dates = {d["column"]: d for d in dates if (d["schema"], d["object"]) == right and WATERMARK.search(d["column"])}
        for field in sorted(set(left_dates) & set(right_dates)):
            a, b = left_dates[field], right_dates[field]
            add("ANONYMIZED PATTERN", left[0], f"{left[1]} -> {right[1]}", field, finding="FK-linked timestamp coverage comparison",
                explanation=f"Parent min/max={a['min']} .. {a['max']}; child min/max={b['min']} .. {b['max']}. Similar names do not prove identical semantics.")

    for limitation in runtime_limitations:
        add("LIMITATIONS / NOTES", finding=limitation[0], explanation=limitation[1])

    for col in columns:
        if SENSITIVE.search(col["name"] + " " + col["object"]):
            add("PHI / PRIVACY REVIEW", col["schema"], col["object"], col["name"], type_label(col), finding="Potential sensitive field; metadata-name flag only",
                explanation="No field values were read. Review with data owner/privacy lead; not a HIPAA determination.")

    for area, explanation in [
        ("HIPAA scope", "Schema-level risk screen only; does not establish HIPAA applicability/compliance or inspect governance controls."),
        ("Sensitive values", "No raw person-level values or category labels are in this workbook. Category patterns are frequency ranks/counts only."),
        ("Data quality", "Distinct counts are descriptive; repeated values are not defects absent a declared key or business rule. FK checks cover declared relationships only."),
        ("Incremental load", "Timestamp candidates do not prove updates advance them, timezone consistency, late-arrival handling, or delete capture."),
        ("Views and procedures", "Views are counted/profiled through their definitions; stored procedures/parameters are inventoried but procedures are never executed."),
        ("Grain", "Named examples use AdventureWorks object semantics and declared keys/FKs. Other rows state the declared key and mark business meaning for owner confirmation."),
    ]:
        add("LIMITATIONS / NOTES", finding=area, explanation=explanation)

    entity_lines = ["erDiagram"]
    for obj in tables:
        cols = cols_by_table[(obj["schema"], obj["name"])]
        keys = [c for c in cols if c["pk"] or any(c["name"] in rel["from_columns"] and (rel["from_schema"], rel["from_table"]) == (obj["schema"], obj["name"]) for rel in relationships)]
        entity_id = f"{obj['schema']}_{obj['name']}".replace("-", "_")
        entity_lines.append(f"    {entity_id} {{")
        for col in keys:
            marker = " PK" if col["pk"] else " FK"
            safe_type = re.sub(r"[^A-Za-z0-9_]", "_", col["type"])
            entity_lines.append(f"        {safe_type} {col['name']}{marker}")
        entity_lines.append("    }")
    for rel in relationships:
        left = f"{rel['to_schema']}_{rel['to_table']}".replace("-", "_")
        right = f"{rel['from_schema']}_{rel['from_table']}".replace("-", "_")
        entity_lines.append(f"    {left} ||--o{{ {right} : \"{rel['name']}\"")
    mermaid_path = REPORT_DIR / f"{database}-er-diagram.mmd"
    mermaid_content = "\n".join(entity_lines) + "\n"
    mermaid_changed = not mermaid_path.exists() or mermaid_path.read_text(encoding="utf-8") != mermaid_content
    if mermaid_changed:
        mermaid_path.write_text(mermaid_content, encoding="utf-8")

    png_path = REPORT_DIR / f"{database}-er-diagram.png"
    render_error = ""
    swift_script = Path(__file__).with_name("render_erd.swift")
    render_png = not png_path.exists() or mermaid_changed or png_path.stat().st_mtime < mermaid_path.stat().st_mtime
    if render_png:
        try:
            if not shutil.which("swift") or not relationships:
                raise RuntimeError("Swift renderer unavailable or no declared foreign keys to diagram")
            diagram_data = {"tables": [{"schema": o["schema"], "name": o["name"],
                                        "pk": table_pks.get((o["schema"], o["name"]), []),
                                        "fk": sorted({c for rel in relationships if (rel["from_schema"], rel["from_table"]) == (o["schema"], o["name"]) for c in rel["from_columns"]})}
                                       for o in tables], "relationships": relationships}
            with tempfile.TemporaryDirectory(prefix="eda-erd-") as temporary:
                json_path = Path(temporary) / "diagram.json"
                json_path.write_text(json.dumps(diagram_data), encoding="utf-8")
                result = subprocess.run(["swift", str(swift_script), str(json_path), str(png_path)], text=True, capture_output=True)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            if not png_path.exists() or png_path.stat().st_size == 0:
                raise RuntimeError("Renderer returned without a non-empty PNG")
        except Exception as error:
            render_error = str(error)
            if png_path.exists():
                png_path.unlink()

    def rows_for(*sections: str) -> list[list[object]]:
        return [row for row in findings[1:] if row[0] in sections]

    overview = [
        ["Item", "Result", "How to interpret"],
        ["Database", database, f"Read-only EDA completed {now} (UTC)."],
        ["Objects", f"{len(tables)} tables; {len(views)} views; {len(procedures)} stored procedures", "Procedures were inventoried only; none were executed."],
        ["Structure", f"{len(columns)} table/view columns; {len(relationships)} declared foreign keys", "Catalog metadata is from the connected AdventureWorks database."],
        ["Row grain example", "Sales.SalesOrderHeader: one row per order; Sales.SalesOrderDetail: one row per order line", "One order can therefore have multiple detail rows."],
        ["Address example", "Person.Address: one address record; Person.BusinessEntityAddress: one entity/address link", "A link-table row is not itself a new postal address."],
        ["PersonID distinct count", "See Data Quality rows for PersonID in each object", "Distinct non-null PersonID is the number of represented people, not the number of rows."],
        ["SQL type notation", "nvarchar(50); decimal(19,4)", "Character length is in characters; decimal precision is total digits and scale is fractional digits."],
        ["Privacy", "Metadata flagging and anonymized aggregates only", "No raw personal values or category labels are included."],
    ]
    inventory = [["Schema", "Object", "Object type", "Rows", "Count method", "Likely row grain", "Interpretation / confidence"]]
    for row in rows_for("OBJECT INVENTORY"):
        inventory.append([row[1], row[2], row[4], row[6], row[10], row[5], row[11]])

    field_types = [["Schema", "Object", "Column", "SQL type", "Length", "Length kind", "Precision", "Scale", "Nullable?", "Key role", "FK references"]]
    for col in columns:
        refs = fk_by_column.get((col["schema"], col["object"], col["name"]), [])
        role = "PK + FK" if col["pk"] and refs else "PK" if col["pk"] else "FK" if refs else "Not a declared PK/FK"
        length, length_kind, precision, scale = type_dimensions(col)
        field_types.append([col["schema"], col["object"], col["name"], type_label(col), length, length_kind, precision, scale, "Yes" if col["nullable"] else "No", role, "; ".join(refs)])
    field_types.append(["", "", "", "", "", "TYPE GUIDE", "", "", "", "", "Variable types have Variable length kind; nchar/nvarchar lengths are characters; varchar/varbinary lengths are bytes. decimal/numeric precision is total digits and scale is digits after decimal."])

    data_quality = [["Schema", "Object", "Column", "Rows in object", "Null rows", "Null %", "Distinct non-null", "PK/FK status", "Finding", "Interpretation"]]
    for row in rows_for("FIELD PROFILE + TYPE"):
        data_quality.append([row[1], row[2], row[3], row[6], row[7], row[8], row[9], row[10], row[10], row[11]])
    for row in rows_for("DISTINCT / NULL REVIEW", "FK QUALITY", "CONSTRAINT METADATA"):
        data_quality.append([row[1], row[2], row[3], row[6], row[7], row[8], row[9], row[10], row[10], row[11]])
    data_quality.append(["", "", "", "", "", "", "", "METRIC GUIDE", "Distinct non-null", "COUNT_BIG(DISTINCT column). Repeated values are not defects unless a declared key or business rule requires uniqueness."])

    phi_review = [["Schema", "Object", "Column", "SQL type", "Review flag", "Interpretation"]]
    for row in rows_for("PHI / PRIVACY REVIEW"):
        phi_review.append([row[1], row[2], row[3], row[4], row[10], row[11]])
    phi_review.append(["", "", "", "", "Scope note", "Name/type-based risk screen only; it does not establish HIPAA applicability or compliance. No field values were read."])

    incremental = [["Schema", "Object", "Date/time column", "SQL type", "Rows", "Null rows", "Null %", "Finding", "Evidence / limitations"]]
    for row in rows_for("INCREMENTAL LOAD"):
        incremental.append([row[1], row[2], row[3], row[4], row[6], row[7], row[8], row[10], row[11]])

    patterns = [["Pattern", "Schema", "Object / comparison", "Column", "Category value", "Object rows", "Total non-null values", "Distinct categories", "Category rows", "% of non-null", "Finding / coverage note"]]
    for row in rows_for("ANONYMIZED PATTERN"):
        patterns.append(["Aggregate numeric/date", row[1], row[2], row[3], "", row[6], "", row[9], "", "", f"{row[10]}; {row[11]}"])
    for row in rows_for("CATEGORICAL VALUE FREQUENCY"):
        finding_match = re.match(r"([\d,]+) rows \(([\d.]+)% of non-null\)", str(row[10]))
        category_count = int(finding_match.group(1).replace(",", "")) if finding_match else None
        category_pct = float(finding_match.group(2)) if finding_match else None
        total_match = re.search(r"Non-null total=([\d,]+)", str(row[11]))
        nonnull_total = int(total_match.group(1).replace(",", "")) if total_match else None
        patterns.append(["Categorical breakdown", row[1], row[2], row[3], row[4], row[6], nonnull_total, row[9], category_count, category_pct, row[11]])

    relationship_sheet = [["FK constraint", "From schema", "From table", "From column", "To schema", "To table", "To column", "Cardinality", "Delete action", "Update action"]]
    for rel in relationships:
        child_fk_unique = (rel["from_schema"], rel["from_table"], tuple(sorted(rel["from_columns"]))) in unique_child_keys
        cardinality = "One-to-one" if child_fk_unique else "One-to-many"
        for mapping in fk_groups[rel["name"]]:
            relationship_sheet.append([rel["name"], rel["from_schema"], rel["from_table"], mapping[3],
                                       rel["to_schema"], rel["to_table"], mapping[6], cardinality,
                                       rel["delete"], rel["update"]])
    for row in rows_for("FK QUALITY"):
        relationship_sheet.append([row[3], row[1], row[2], "", "", "", "", "Orphan check", "", ""])

    limitations = [["Area", "Explanation"]]
    for row in rows_for("LIMITATIONS / NOTES"):
        limitations.append([row[10], row[11]])

    workbook_path = REPORT_DIR / f"{database}-eda-findings-{timestamp}.xlsx"
    sheets = [("Overview", overview), ("Object Inventory", inventory), ("Fields and Types", field_types),
              ("Data Quality", data_quality), ("PHI Review", phi_review), ("Incremental Load", incremental),
              ("Patterns", patterns), ("Relationships", relationship_sheet), ("Limitations", limitations)]
    build_workbook(workbook_path, sheets)
    for old_workbook in REPORT_DIR.glob(f"{database}-eda-findings-*.xlsx"):
        if old_workbook != workbook_path:
            old_workbook.unlink()
    archive_path = REPORT_DIR / f"{database}-eda-download-{datetime.now(timezone.utc).strftime('%Y%m%d')}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(workbook_path, workbook_path.name)
        archive.write(mermaid_path, mermaid_path.name)
        if png_path.exists():
            archive.write(png_path, png_path.name)
    print(f"Database: {database}")
    print(f"Objects: {len(tables)} tables, {len(views)} views, {len(procedures)} stored procedures")
    print(f"Columns: {len(columns)}; foreign keys: {len(relationships)}")
    print(f"Workbook: {workbook_path} ({workbook_path.stat().st_size} bytes)")
    print(f"Workbook sheets: {len(sheets)}; field profiles: {len(columns)}")
    print(f"Mermaid: {mermaid_path} ({mermaid_path.stat().st_size} bytes)")
    print(f"ER PNG: {png_path} ({png_path.stat().st_size} bytes)" if png_path.exists() else f"ER PNG unavailable: {render_error}")
    print(f"Download bundle: {archive_path} ({archive_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"EDA failed: {error}", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(1)