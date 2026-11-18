#!/usr/bin/env python3
"""Replay historical AMLNet transactions into chronological CSV batches.

The Power Automate Desktop flow ingests released batches separately.
Requires: pandas
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="CSV exported from AMLNet's cleaned transactions table")
    parser.add_argument("--output", type=Path, required=True, help="Folder monitored by Power Automate (incoming)")
    parser.add_argument("--timestamp-column", default="transaction_timestamp", help="Source CSV's timestamp column")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--interval", type=float, default=10.0, help="Seconds between batch releases")
    parser.add_argument("--max-rows", type=int, default=1000, help="Maximum input rows for this demonstration")
    args = parser.parse_args()

    if args.batch_size < 1 or args.max_rows < 1 or args.interval < 0:
        parser.error("--batch-size and --max-rows must be positive; --interval must be nonnegative")
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        parser.error(f"Source file does not exist: {source}")
    output.mkdir(parents=True, exist_ok=True)
    log_dir = output.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "release_log.csv"

    # Read as strings so IDs, numeric formatting, and blank fields are preserved.
    df = pd.read_csv(source, dtype=str, keep_default_na=False, nrows=args.max_rows, encoding="utf-8-sig")
    if args.timestamp_column not in df.columns:
        parser.error(f"Timestamp column '{args.timestamp_column}' is missing. Available columns: {', '.join(df.columns)}")
    if df.empty:
        parser.error("No transactions found in the input CSV")

    parsed = pd.to_datetime(df[args.timestamp_column], utc=True, errors="coerce")
    if parsed.isna().any():
        examples = df.loc[parsed.isna(), args.timestamp_column].head(3).tolist()
        parser.error(f"{parsed.isna().sum()} rows have missing/unparseable timestamps, e.g. {examples}")
    df = df.assign(_sort_timestamp=parsed).sort_values("_sort_timestamp", kind="stable").drop(columns="_sort_timestamp")

    total_batches = (len(df) + args.batch_size - 1) // args.batch_size
    file_names = [f"replay_batch_{i:04d}.csv" for i in range(1, total_batches + 1)]
    conflicts = [name for name in file_names if (output / name).exists() or (output / (name + ".tmp")).exists()]
    if conflicts:
        parser.error("These output files already exist (nothing overwritten): " + ", ".join(conflicts[:5]) + ". Move previous test batches out of 'incoming' first.")

    new_log = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as log_file:
        writer = csv.writer(log_file)
        if new_log:
            writer.writerow(["batch_file", "row_count", "first_transaction_time", "last_transaction_time", "released_at_utc"])
            log_file.flush()
        for index, batch_name in enumerate(file_names):
            batch = df.iloc[index * args.batch_size : (index + 1) * args.batch_size]
            final_path = output / batch_name
            temporary_path = output / (batch_name + ".tmp")
            # Write .tmp, then rename atomically. A subsequent PAD folder scan won't read a partial CSV.
            try:
                batch.to_csv(temporary_path, index=False, encoding="utf-8-sig")
                os.replace(temporary_path, final_path)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
            released_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            writer.writerow([batch_name, len(batch), batch[args.timestamp_column].iloc[0], batch[args.timestamp_column].iloc[-1], released_at])
            log_file.flush()
            print(f"[{index + 1}/{total_batches}] Released {batch_name} ({len(batch)} rows), transaction times {batch[args.timestamp_column].iloc[0]} → {batch[args.timestamp_column].iloc[-1]}", flush=True)
            if index + 1 < total_batches:
                time.sleep(args.interval)
    print(f"Finished. Release log: {log_path}")


if __name__ == "__main__":
    main()
