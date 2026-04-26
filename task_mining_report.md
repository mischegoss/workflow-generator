# Task Taxonomy Mining Report

Workflows analyzed: 623
Atomic tasks: 36
Composite tasks: 25

## Atomic tasks (by corpus frequency)

| Task | Occurrences | Canonical activities | Phrase count |
|---|---|---|---|
| `branch_decision` | 8010 | IfElseBranchActivity, ReturnValue, IfElseActivity | 7 |
| `set_variable` | 2062 | MemorySet, MultiMemorySet | 5 |
| `parallel_execution` ⚠ | 1632 | UserGroup, ParallelActivity | 0 |
| `read_table_cell` | 1566 | GetCellValue, GetRows, GetColumns, GetColumnName | 4 |
| `display_value` | 1356 | DisplayValue, DisplayMultiValue | 4 |
| `count_table_rows` | 748 | GetRowsCount, GetColumnsCount | 4 |
| `string_operations` | 745 | ReplaceString, Contains, Split, IsEmpty, Length | 6 |
| `terminate` | 686 | ExitWhile, TerminateWorkflow | 4 |
| `modify_table` | 621 | SetCellValue, DeleteMemoryTableRows, MemoryTableUnion, AddMemoryTableRow, RotateTable | 5 |
| `sequence_control` ⚠ | 518 | SequenceActivity | 0 |
| `iterate_rows` | 486 | WhileActivity | 6 |
| `query_database` | 410 | TSQLQuery, TSQLStatement, MySQLQuery, DB2Query, OracleQuery | 7 |
| `invoke_subworkflow` ⚠ | 363 | RunWorkflow, WorkflowCounter | 0 |
| `create_table` | 358 | CreateMemoryTable | 3 |
| `read_file` | 345 | JsonToTable, ReadXLS, ReadFile, JSONtoTableAdvanced, ConvertTextToTable | 11 |
| `filter_table` | 301 | ResultSetFilter, MemoryTableGetUniqueRows, RemoveEmptyRowsAndColumnsFromTable | 6 |
| `date_operations` | 244 | GetDate, DateDifference, AddDate, GetUNIXTimestamp | 9 |
| `query_itsm` | 235 | SNUpdateRecord, SNGetRecord, SNCreateRecord, JiraGetIssue, MFSMAXAddComment | 8 |
| `run_script` | 233 | PowerShell, PowerShellScript, Executor | 4 |
| `read_structured_input` ⚠ | 221 | StartJsonSession, StartXMLSession, FolderList | 0 |
| `send_email` | 210 | SendEmail, SMTPSendEmail | 6 |
| `goto` ⚠ | 200 | GoTo, Continue | 0 |
| `query_api` | 151 | HTTPRequest, CrawlWebsiteExtractText | 7 |
| `query_system_state` | 133 | Ping, GetInstalledSoftware, FolderExist, ServiceStatus, Memory | 12 |
| `wait` | 126 | Wait | 4 |
| `write_file` | 124 | WriteFile, WriteXLS, WriteCSV, FTPDeleteFile | 4 |
| `convert_table` | 120 | ConvertToHTMLTable, ConvertTableToJSON, TabletoXML, DatatableifyHTML | 4 |
| `send_notification` | 114 | SelfServiceResponse, NewEvent, DisplayIncident | 4 |
| `password_operations` ⚠ | 95 | ConvertPasswordToPlaintext, PasswordGenerator | 0 |
| `server_action` | 77 | FolderCreate, SetFolderPermissions, ServerRestart, ServiceStart, ServiceStop | 6 |
| `math_operations` | 71 | FunctionCalculator, RandomNumberGenerator | 4 |
| `clean_memory` ⚠ | 68 | MemoryClean | 0 |
| `manage_ad_account` | 42 | ADAddtoGroup, ADDisableAccount, ADCreateAccount, ADRemoveFromGroup, ADDeleteAccount | 5 |
| `query_directory` | 20 | ADUserExists, ADListGroup | 7 |
| `sort_table` | 20 | SortTable | 4 |
| `lock` ⚠ | 9 | LockExecutor | 0 |

⚠ = no prompt phrases yet — needs human curation.

## Composite tasks (top 25 by corpus support)

| Task | Support | Support % | Suggested scaffold |
|---|---|---|---|
| Count Table Rows → Read Table Cell | 304 | 48.8% | p019 |
| Set Variable → Display Value | 248 | 39.8% | p015 |
| Set Variable → Set Variable | 235 | 37.7% | p011 |
| Read Table Cell → Display Value | 210 | 33.7% | p002 |
| Set Variable → Read Table Cell | 209 | 33.5% | p002 |
| Count Table Rows → Display Value | 201 | 32.3% | p013 |
| Set Variable → Count Table Rows | 199 | 31.9% | p011 |
| Read Table Cell → Read Table Cell | 197 | 31.6% | p002 |
| Display Value → Display Value | 194 | 31.1% | p013 |
| Read Table Cell → Set Variable | 192 | 30.8% | p002 |
| Set Variable → Set Variable → Display Value | 181 | 29.1% | p015 |
| Count Table Rows → Set Variable | 180 | 28.9% | p011 |
| Count Table Rows → Read Table Cell → Display Value | 176 | 28.3% | p019 |
| Read Table Cell → Count Table Rows | 172 | 27.6% | p019 |
| Set Variable → Display Value → Display Value | 168 | 27.0% | p015 |
| Set Variable → Count Table Rows → Read Table Cell | 166 | 26.6% | p019 |
| Create Table → Read Table Cell | 164 | 26.3% | p002 |
| Set Variable → Read Table Cell → Display Value | 164 | 26.3% | p015 |
| Set Variable → Set Variable → Set Variable | 164 | 26.3% | p011 |
| Read File → Read Table Cell | 163 | 26.2% | p012 |
| Count Table Rows → Count Table Rows | 162 | 26.0% | p026 |
| Create Table → Count Table Rows | 162 | 26.0% | p020 |
| Display Value → Set Variable | 162 | 26.0% | p015 |
| Set Variable → Count Table Rows → Display Value | 158 | 25.4% | p015 |
| Read File → Count Table Rows | 149 | 23.9% | p023 |

## Unmatched activities (need family assignment)

These activities appeared in the corpus but aren't in any family.
Add them to the appropriate family in ACTIVITY_FAMILIES and re-run.

| Activity | Count |
|---|---|
| `GetCellValueAdvanced` | 188 |
| `NewIncident` | 57 |
| `SingleSSHCommand` | 52 |
| `MatchRegularExpression` | 50 |
| `Counter` | 48 |
| `Terminate` | 45 |
| `ConvertToPlainText` | 37 |
| `JiraGenericCommand` | 37 |
| `ExcelWrite` | 35 |
| `XMLEvaluateXpathExpression` | 34 |
| `VMPowerCLI` | 33 |
| `RenameMemoryTableColumn` | 33 |
| `XMLEditNode` | 30 |
| `DeleteFile` | 28 |
| `AddMemoryTableColumn` | 27 |
| `CleanTable` | 24 |
| `ReplaceStringAdvanced` | 23 |
| `DeleteMemoryTableColumns` | 23 |
| `ADLDAPQuery` | 23 |
| `SendSMS` | 22 |
| `JiraUpdateIssue` | 22 |
| `ADGetProperty` | 20 |
| `ADSetProperty` | 20 |
| `VMExists` | 20 |
| `XMLElementstoTable` | 18 |
| `RegistryQuery` | 18 |
| `SNGetCatalogVariables` | 17 |
| `FormatDate` | 16 |
| `VMPowerState` | 15 |
| `FileCopy` | 14 |
| ...594 more... | |

## Top family co-occurrences (sliding window)

Families that appear together in workflows. Useful for spotting 
composite tasks the PrefixSpan miner may have missed.

| Family A | Family B | Co-occurrences |
|---|---|---|
| count_table_rows | read_table_cell | 287 |
| display_value | set_variable | 197 |
| read_table_cell | set_variable | 189 |
| count_table_rows | set_variable | 162 |
| count_table_rows | create_table | 151 |
| display_value | read_table_cell | 150 |
| read_file | read_table_cell | 144 |
| set_variable | set_variable | 142 |
| create_table | read_table_cell | 126 |
| count_table_rows | read_file | 126 |
| count_table_rows | filter_table | 120 |
| read_table_cell | string_operations | 116 |
| count_table_rows | display_value | 113 |
| filter_table | read_table_cell | 109 |
| set_variable | string_operations | 108 |
| display_value | display_value | 103 |
| display_value | string_operations | 103 |
| read_file | set_variable | 102 |
| query_database | set_variable | 95 |
| read_file | read_structured_input | 95 |

## Next steps for the reviewer

1. Open `task_taxonomy_draft.json`.
2. For every task with `needs_review: true`, decide if the task is valid.
3. Write a clear one-sentence `description` for every task.
4. Add or refine `prompt_phrases` — these are what users will actually type.
5. For composite tasks, verify `suggested_scaffold_pattern_id` matches the intent.
6. Delete `needs_review` field once each task is checked.
7. Rename file from `task_taxonomy_draft.json` → `task_taxonomy.json`.
8. Same for `task_match_phrases_draft.json` → `task_match_phrases.json`.

Unmatched activities from the table above should be added to the 
`ACTIVITY_FAMILIES` dict in `mine_task_taxonomy.py` and the script re-run.