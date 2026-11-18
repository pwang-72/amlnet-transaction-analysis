"""Ingest a cleaned AMLNet CSV batch into BigQuery; optionally relay flagged cases.

Requires google-cloud-bigquery and your existing Google Cloud credentials.
To enable cloud case delivery, set AMLNET_CASE_FLOW_URL in the Windows user
environment and call this script with --sync-cases.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


def send_cases(url: str, cases: list[dict]) -> None:
    """Submit the flagged rows and wait for the flow's success response."""
    request = Request(
        url,
        data=json.dumps({"CasesJson": cases}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise RuntimeError(f"Cloud flow returned HTTP {response.status}")
    except HTTPError as exc:
        # Avoid printing a signed trigger URL from the HTTP exception.
        raise RuntimeError(f"Cloud flow returned HTTP {exc.code}") from None


def ingest(file: Path, *, location: str, sync_cases: bool, flow_url: str | None) -> None:
    from google.cloud import bigquery

    if sync_cases and (not flow_url or not flow_url.startswith("https://")):
        raise ValueError("Set the HTTPS AMLNET_CASE_FLOW_URL environment variable first")

    client = bigquery.Client()
    target = f"{client.project}.amlnet_replay.transactions"
    schema = client.get_table(target).schema
    stage = f"{client.project}.amlnet_replay._batch_{uuid4().hex}"

    try:
        config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=1,
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            max_bad_records=0,
            allow_quoted_newlines=True,
        )
        with file.open("rb") as source:
            client.load_table_from_file(
                source, stage, job_config=config, location=location
            ).result()

        columns = ", ".join(f"`{field.name}`" for field in schema)
        values = ", ".join(f"s.`{field.name}`" for field in schema)
        merge = f"""
            MERGE `{target}` AS t
            USING `{stage}` AS s
            ON t.transaction_id = s.transaction_id
            WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({values})
        """
        job = client.query(merge, location=location)
        job.result()
        inserted = job.num_dml_affected_rows or 0

        cases_sent = 0
        if sync_cases:
            # Read this batch, not the whole replay table. Retrying an already
            # inserted batch remains safe because Dataverse checks Transaction ID.
            flagged_sql = f"""
                SELECT
                    CAST(transaction_id AS STRING) AS transaction_id,
                    FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ',
                                     transaction_timestamp, 'UTC')
                        AS transaction_timestamp,
                    origin_id,
                    destination_id,
                    CAST(amount AS STRING) AS amount,
                    label_is_fraud,
                    label_is_money_laundering
                FROM `{stage}`
                WHERE label_is_fraud OR label_is_money_laundering
                ORDER BY transaction_timestamp, transaction_id
            """
            cases = [dict(row.items()) for row in client.query(flagged_sql, location=location).result()]
            if cases:
                send_cases(flow_url, cases)
                cases_sent = len(cases)

        print(
            f"SUCCESS: {file.name}; inserted={inserted}; cases_sent={cases_sent}",
            flush=True,
        )
    finally:
        client.delete_table(stage, not_found_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--location", default="EU")
    parser.add_argument("--sync-cases", action="store_true")
    args = parser.parse_args()

    file = args.file.expanduser()
    if not file.is_absolute():
        file = Path(__file__).resolve().parent / file

    try:
        ingest(
            file.resolve(),
            location=args.location,
            sync_cases=args.sync_cases,
            flow_url=os.environ.get("AMLNET_CASE_FLOW_URL"),
        )
        return 0
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
