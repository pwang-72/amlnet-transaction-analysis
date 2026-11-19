# AMLNet — transaction replay and investigation-case automation

An extension of the [AMLNet transaction analysis](../README.md), using the **already-cleaned** transactions rather than repeating the original data audit. Historical CSV records are released in batches, ingested into BigQuery, and flagged records are sent to Dataverse for review in Power Apps. The original `amlnet` dataset and published dashboard remain unchanged.

## Workflow

[automation workflow](workflow.svg)

For each ingested batch, `ingest_batch.py` also sends its original fraud/money-laundering-labelled records to `Power Automate Cloud (HTTP) → Dataverse → Power Apps`.

- BigQuery `MERGE` inserts only previously unseen transaction IDs.
- In Dataverse, `Transaction ID` is a unique alternative key. The cloud flow **creates a case only if it does not already exist**. Replays do not overwrite `Case Status` or `Investigation Notes`.
- Labels are taken from the dataset; this example **does not detect fraud**, score new transactions, or run Power BI in DirectQuery mode.

## Files

```text
automation/
├── replay_transactions.py       # Emit timestamp-ordered CSV batches
├── ingest_batch.py              # BigQuery MERGE + flagged-case HTTP delivery
├── sql/setup_replay.bqsql       # Replay table and account-network view
├── power-platform/
│   ├── AMLNet_Batch_Processing.txt       # Exported PAD actions (portable paths)
│   └── AMLNetInvestigation_1_0_0_2.zip # Unmanaged solution: app, table, flow
├── .gitignore                   # Exclude local CSVs, logs, and secrets
└── README.md
```

Runtime-only folders: `data/`, `incoming/`, `processed/`, `logs/`. They are generated or maintained locally and are **not** committed. Create `processed/` before running PAD; `replay_transactions.py` creates `incoming/` and `logs/` as needed. Place the cleaned source CSV under `data/`.

## Configuration

1. **BigQuery.** Use the existing cleaned `amlnet.transactions` table (23 columns), with a Google Cloud project and Application Default Credentials. Execute [`sql/setup_replay.bqsql`](sql/setup_replay.bqsql) once in the **EU** BigQuery location. This creates an initially empty `amlnet_replay.transactions` table and its `flagged_account_connections` view. Install Python dependencies with `pip install pandas google-cloud-bigquery`.
2. **Power Automate Desktop.** The exported actions are in [`power-platform/AMLNet_Batch_Processing.txt`](power-platform/AMLNet_Batch_Processing.txt). Import/copy them into the PAD designer as appropriate. **Before running**, replace the two `C:\PATH_TO\...` placeholders: `PythonExe` must point to the local `amlnet` Python executable, and `InitialDirectory` to the folder containing these scripts. At runtime, choose that project folder (`SelectedFolder`). PAD polls `incoming/replay_batch_*.csv`, executes the existing ingestion command below, and moves a batch to `processed/` only when its exit code is 0. This is the existing `AMLNet_Batch_Processing` flow, not an additional desktop flow. The published text preserves the tested action sequence and removes the author's absolute Windows account path.
3. **Power Apps, Dataverse, and cloud flow.** Import the **unmanaged** solution [`power-platform/AMLNetInvestigation_1_0_0_2.zip`](power-platform/AMLNetInvestigation_1_0_0_2.zip) into a suitable Power Platform environment. It contains the `AMLNet Case Review` model-driven app and site map, the `AMLNet Investigation Cases` table with its fields, forms and unique transaction-ID key, the `AMLNet - Flagged Case Intake` HTTP cloud flow, and its Microsoft Dataverse connection reference. Rebind the Dataverse connection to an authorized account in the target environment, enable the flow, and obtain **a new** HTTP POST URL from that environment. Imported solutions do not export case records, authenticated sessions, or an active signed endpoint. Platform-managed dependencies (App Framework Infra Extensions and App Module Assets) must be present in a target environment.

   The `AMLNet - Flagged Case Intake` flow receives a POST with an array property `CasesJson`. Its actions are: **When an HTTP request is received → Parse JSON → Apply to each → Find case (Dataverse List rows) → Condition → Add a new row (only when absent) → Response 200 (after the loop)**. Use the existing Dataverse table `AMLNet Investigation Cases`. `Parse JSON` content is the **expression** `triggerBody()?['CasesJson']`, with its schema set to an array of transaction objects. `Apply to each` uses **Parse JSON Body**, not `Find case`'s results.
   HTTP trigger body schema (the `CasesJson` value is an array, not a quoted JSON string):

   ```json
   {"type":"object","properties":{"CasesJson":{"type":"array"}},"required":["CasesJson"]}
   ```

   The existing **Parse JSON** schema describes an array of objects with string fields `transaction_id`, `transaction_timestamp`, `origin_id`, `destination_id`, `amount`, and boolean fields `label_is_fraud` and `label_is_money_laundering`. Place `Response` **outside** the loop so HTTP 200 is returned only after the Dataverse work finishes.

4. **Case identity.** `Find case` filters by the logical Dataverse field `cr53e_transactionid` and the current transaction ID. Test whether `length(body('Find_case')?['value'])` equals `0`; only in that branch create the case, setting `Case Reference = AML-<transaction_id>` and `Case Status = New`. Map transaction timestamp, origin/destination, amount and two original boolean labels. Do not update existing cases.
5. **HTTP endpoint.** Configure `AMLNET_CASE_FLOW_URL` as a **Windows user environment variable** containing the complete HTTPS trigger URL. The local Python process reads it at runtime. Never place the URL, access tokens, or Google credentials in tracked files. The demonstrated signed URL with an *Anyone* trigger can be invoked by anyone who knows that URL; it is suitable only for controlled synthetic-data demonstrations, not production case management.
6. **Power BI.** Use a **copy** of the original `.pbix`. Change the BigQuery Power Query source references from schema `amlnet` to `amlnet_replay` for the four direct queries, including the account-network query's object kind `View` rather than `Table`. The dependent account-selection query follows those references. Import-mode Power BI requires a refresh to show newly replayed rows. Do not redirect the original published report.

### Existing Power Automate Desktop command

```text
%PythonExe% "%SelectedFolder%\ingest_batch.py" --file "%CurrentItem.FullName%" --sync-cases
```

`PythonExe` is the configured executable in the Python environment containing `google-cloud-bigquery`; its path in this demonstrated Windows installation did not require outer quotes in the PAD DOS action. The nonzero exit code prevents moving files that fail BigQuery ingestion **or** cloud case delivery. Repeating a previously imported batch is safe under the two ID-based checks.

### Replay an existing cleaned CSV excerpt

Run from the local folder containing `replay_transactions.py`:

```powershell
python replay_transactions.py --source data\flagged_window.csv --output incoming --batch-size 25 --interval 10 --max-rows 100
```

The source must be an export with the same columns/order as the cleaned BigQuery table. It can contain flagged and unflagged records. The producer writes each batch atomically so PAD does not ingest a partially written file. Reusing batch filenames requires clearing or archiving older files in `incoming`/`processed` to avoid move conflicts.

### Known scope

This implementation has been demonstrated with **successful HTTP-triggered cloud execution and safe handling of an already-existing Dataverse case**. A manually submitted flagged case was created successfully earlier. The HTTP path has not yet independently demonstrated creation of a *new* case from a previously unseen transaction. The Power Apps developer environment displayed a Dataverse-capacity warning, so this is a small synthetic-data portfolio example, not a production deployment. The original dataset, its audits, original dashboard, and model-driven case app are managed separately; they are not recreated here.

## Publication notes

The Power Platform solution is a native exported `.zip` and should remain zipped in version control. The PAD `.txt` is the copied action definition; it is **not** a standalone Python program. Both are included to document the actual GUI workflows, not to replace them with reimplementations.

The published PAD file replaces two machine-specific paths with `C:\PATH_TO\...` placeholders; the live desktop flow remains unchanged. The cloud flow's signed trigger URL, Google credentials, local CSV batches, investigation records, and cached connection secrets must not be committed.

The replay Power BI `.pbix` copy is not included in this archive; the original report remains in the parent repository and the replay-specific source-switching procedure is described above.
