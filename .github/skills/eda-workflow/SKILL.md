---
name: eda-workflow
description: 'Run a structured, read-only exploratory data analysis workflow against the connected database. Use for database EDA, table profiling, SQL data types, row counts, HIPAA or PHI risk review, data quality, distributions, patterns, keys, relationships, ER diagrams, Excel findings, or PNG ER diagrams.'
argument-hint: 'Optional focus, schema, tables, or analysis question'
user-invocable: true
---

# Database EDA Workflow

Run the stages below in order. Use the active database MCP tools and their returned metadata; do not assume a table, column, key, relationship, or object type that the tools have not confirmed. State the connected database and the scope of the analysis at the start. Capture the analysis question, included/excluded schemas and objects, run timestamp, and known business definitions. For each important table, record its likely row grain (what one row represents), evidence for that interpretation, and any unresolved ambiguity.

## Safety and Scope

- Treat the workflow as read-only. Use discovery, read, and aggregate tools only. Never create, update, delete, or execute database objects.
- Do not retrieve or reproduce raw values from fields or tables that may contain personal, health, credential, payment-card, or other sensitive data. Use names, types, counts, and aggregate results for risk review.
- Do not claim that a database is HIPAA compliant or non-compliant. Report observed potential PHI/PII, evidence, gaps, and recommended review; compliance requires organizational, legal, and operational controls beyond schema inspection.
- Keep queries bounded. Prefer counts and aggregates; limit any non-sensitive row sample to a few records and only when useful to resolve a specific question.
- If a capability or object is not exposed by MCP, say so explicitly. Do not silently skip it or substitute guesses.

## Workflow

### 1. Data Profiling and Object Inventory

1. Call `describe_entities` to discover entities. Request name-only discovery first when there are many entities, then request details in manageable groups.
2. Record the database name, schema, entity name, source type, and every available field's SQL data type. Include length for character/binary types, precision and scale for decimal types, nullability, and declared primary-key status when metadata provides them. Do not infer unavailable type details.
3. For every exposed table or view that supports row reads, use `aggregate_records` with `function: count` and `field: "*"` to get a row count. If the aggregation tool cannot count that source type, use a bounded read and report that as a limitation rather than presenting an estimate as exact.
4. Inventory stored procedures separately. Do not execute them as part of profiling. If the MCP server does not expose procedures or catalog metadata, report that the procedure inventory is unavailable through the current connection.
5. Summarize discovered object counts by source type and include row counts for each profiled table/view. Say whether the inventory is complete for the database or only for MCP-exposed entities.
6. Identify potential creation, modification, ingestion, or change-tracking columns from confirmed date/time types and metadata. Names such as `CreatedDate`, `ModifiedDate`, `UpdatedAt`, `IngestedAt`, or `rowversion` are search clues only; verify the actual type and meaning where metadata supports it. For each plausible timestamp column, calculate minimum and maximum values, null count/percentage, and latest value where safe and supported. Record tables with no apparent incremental-load column and distinguish candidate watermarks from proven change tracking. A timestamp alone does not prove that deletes are captured or that updates always advance it.

### 2. HIPAA and PHI Risk Review

1. Review schema names, entity names, field names, types, descriptions, and relationships for potential identifiers and health-related information. Consider direct identifiers, quasi-identifiers, contact details, dates, account identifiers, and health or treatment terms.
2. Flag likely sensitive entities and fields with the reason they warrant review. Distinguish confirmed schema evidence from a name-based suspicion.
3. Do not select, sample, or return sensitive field values. Avoid unneeded access to credentials, password hashes, payment-card information, or other high-risk data; their existence can be reported from metadata only.
4. Report that this is a technical schema-level risk screen, not a HIPAA audit or legal determination. Note missing evidence, such as access controls, encryption, retention, audit logs, consent, and operational safeguards, when relevant.

### 3. Data Quality

1. Identify available primary-key and foreign-key metadata from entity descriptions or an explicitly available read-only schema/catalog tool. Record composite keys accurately. Do not infer a key merely because a column is named `ID` or appears unique in a sample.
2. For relevant fields, profile null counts and percentages. Use supported count aggregates when their semantics are clear (for example, compare total-row count with non-null field count). If the MCP tools cannot count non-null values or filter nulls reliably, report the gap instead of approximating.
3. Profile distinct counts and duplicate risk with distinct-count aggregates where supported. A distinct count is not proof of a declared unique constraint.
4. Check referential integrity only when FK metadata is available and a safe read-only method can verify it. Distinguish declared relationships from orphan checks that were actually run.
5. Check duplicates for declared keys and reasonable candidate keys or key combinations. Only label a duplicate as a defect when a constraint or documented business rule supports uniqueness; otherwise report it as a candidate-key observation. Include counts of duplicate groups and affected rows when supported.
6. Check relevant cross-field consistency rules (for example, start date <= end date, nonnegative quantities, or status/date agreement) only when supported by declared constraints or supplied business definitions. Label any exploratory rule as a hypothesis, not a confirmed defect.
7. Report each finding with the field/table, metric, result, and method or limitation.

### 4. Data Patterns

1. Select useful numeric, date/time, categorical, and identifier fields based on metadata and the user's focus.
2. Use bounded aggregates for numeric ranges, averages and supported percentiles/quantiles; date ranges and time coverage; and category frequencies. Keep grouping result sizes controlled; avoid grouping high-cardinality identifiers unless there is a specific reason.
3. For timestamp candidates, compare min/max ranges, latest dates, null rates, and apparent update/creation ordering across tables in the same confirmed relationship or business domain. Check whether child and parent tables have compatible time coverage and flag gaps, stale tables, implausible future dates, or inconsistent watermark behavior. Do not equate similar column names with a shared meaning; label any unconfirmed cross-table comparison as exploratory.
4. Compare related measures or categories only when the fields and join/relationship path are supported by metadata. Do not infer causation from descriptive patterns.
5. Summarize notable skew, outliers, rare categories, suspicious ranges, temporal gaps, cross-table date inconsistencies, and candidate follow-up questions. Mark findings as exploratory, not validated business rules.

### 5. Relationships and ER Diagram

1. Build relationships from declared FK metadata or explicit relationship metadata returned by the database tools. Never infer an FK solely from similarly named columns.
2. If relationships are available, create a Mermaid `erDiagram` using confirmed key fields and cardinalities. Save the Mermaid source and render the diagram as a PNG. Use an available Mermaid renderer such as `mmdc`; do not claim a PNG was generated unless the file exists and is non-empty.
3. If only some relationships are exposed, label the diagram as partial and list the missing metadata. If no relationship metadata is available, state that an evidence-based ER diagram cannot yet be produced and identify the required read-only catalog access. Do not create a diagram from guessed relationships.

## Output Artifacts

- Create `reports/eda/` in the workspace for generated deliverables. Use timestamps for the workbook; keep one stable ERD PNG/Mermaid filename and update it only if relationships change.
- For this workspace's local Docker SQL Server, the read-only report helper is [run_local_sql_eda.py](./scripts/run_local_sql_eda.py). Run it with `python3 .github/skills/eda-workflow/scripts/run_local_sql_eda.py` only after confirming `dab-config.json` targets the intended database and the `sqlserver` container is running. It uses the local `sqlcmd` in the container, reads credentials from the ignored local config without printing them, writes timestamped artifacts under `reports/eda/`, removes superseded EDA workbooks so only the latest remains, and refreshes a ZIP download bundle. For other database targets, prefer their active MCP connection and do not assume this local helper applies.
- Save non-diagram findings in `<database>-eda-findings-<timestamp>.xlsx` using separate, filterable tabs: `Overview`, `Object Inventory`, `Fields and Types`, `Data Quality`, `PHI Review`, `Incremental Load`, `Patterns`, `Relationships`, and `Limitations`.
- Keep `Fields and Types` focused on field name, readable SQL type, variable/fixed length, length, precision, scale, nullability, and declared PK/FK role/reference. Examples: `nvarchar(50)` means up to 50 Unicode characters; `decimal(19,4)` means 19 total digits and 4 fractional digits. Do not add unrelated computed/default flags. Identify `rowversion` as a binary change token, not a date.
- In `Object Inventory`, explain row grain with concrete examples where known: one customer row versus many order headers, one order header versus many order-line rows, one address record versus one business-entity/address link, or one person versus multiple phone/email rows. For other objects state the declared key combination and mark semantic meaning as unconfirmed.
- In `Data Quality`, include table/view row count, null count/percentage, and distinct non-null count for each field when supported. Explain that `PersonID` distinct count is represented people, not rows; show this and similar coverage for AddressID, CountryRegionCode, CustomerID, and SalesOrderID.
- In `Incremental Load`, record each date/time candidate's verified type, min/max, null rate, related-table comparisons, and limitations. Do not recommend a watermark as production-ready without confirming update semantics, late-arriving data, timezone handling, and delete capture.
- For every selected categorical field, show the total non-null count, number of distinct categories, each category value, category row count, and percentage of the non-null total. Include all categories when there are at most 250; for larger domains show the top 20 plus one combined `Other categories` row and disclose that cap. Include country, state/province, region/territory, gender, status, and unit-of-measure columns when present. For known codes such as Gender `M`/`F`, show the readable label and stored code, noting codebook interpretation. Keep results aggregate-only: never export person-level row examples, direct identifiers, free-text values, or sensitive values from high-risk columns. Suppress small groups if disclosure risk is material.
- Keep each tab useful for scanning: freeze header rows, add filters, wrap long explanations, and size columns for readability. Store method and confidence/limitations in the relevant tab.
- Never place raw PHI/PII, credentials, password hashes, payment-card values, or other sensitive row values in the workbook. Metadata labels and aggregate metrics may be included when appropriate.
- Save the ER diagram at the stable path `reports/eda/<database>-er-diagram.png` and Mermaid source beside it as `reports/eda/<database>-er-diagram.mmd`. Compare the Mermaid source before rendering; reuse the existing PNG when relationships have not changed. Do not create timestamped ERD image copies on routine EDA runs. If relationships are unavailable, record that limitation in the workbook instead of producing a speculative diagram.
- Prefer an already available `.xlsx` writer and Mermaid renderer. If either is missing, check for an approved existing alternative; do not silently substitute CSV/Markdown for Excel or Mermaid source for PNG. Ask before installing new dependencies, and report any artifact that could not be generated.

## Final Report

Present results in the same stage order: object inventory and row counts; HIPAA/PHI risk screen; data quality; patterns; relationships/ER diagram. The Relationships tab must use separate From schema/table/column and To schema/table/column fields, with one row per FK column mapping (including composite keys). The multi-tab Excel workbook is the detailed findings deliverable; the PNG is the ER diagram deliverable. In chat, summarize high-signal findings and link to generated artifacts. Include:

- Database and analysis scope, including the time or method for counts when available.
- A clear distinction between completed checks, suspected risks, and blocked/unavailable checks.
- The tables, views, and procedures discovered and whether MCP exposure limits completeness.
- High-signal findings and practical next steps, without raw sensitive values.
- A PNG ER diagram only when its relationships are supported by metadata, with a partial diagram clearly labeled when appropriate.