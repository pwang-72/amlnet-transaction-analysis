"""Repair and audit AMLNet v2 transaction metadata before BigQuery reload.

The source CSV stores `metadata` as a Python-style dictionary string
rather than valid JSON. This script:

1. streams the source CSV without loading the full file into memory;
2. converts `datetime.datetime(...)` values to ISO-8601 strings;
3. safely parses the remaining Python literals with `ast.literal_eval`;
4. audits the metadata fields intended for later staging columns;
5. compares the top-level `category` field with
   `metadata.merchant_info.category`;
6. serializes the parsed metadata as valid JSON; and
7. writes a corrected CSV while leaving all other source columns unchanged.

The corrected CSV can then be loaded into BigQuery with `metadata` typed
as JSON.
"""

import ast
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


# ============================================================
# Configuration
# ============================================================

# Preserve the original source file and write the corrected data to a
# separate processed-data path.
INPUT_PATH = Path("data/raw/transactions.csv")
OUTPUT_PATH = Path("data/processed/transactions_fixed.csv")

# Retain a small human-readable sample of parsed metadata for inspection.
SAMPLE_SIZE = 100


# ============================================================
# 1. Parse Python-style metadata
# ============================================================

# Example source value:
#
# {'timestamp': datetime.datetime(2025, 12, 1, 11, 14, 46, 636449),
#  'location': {'city': 'Sydney', 'state': 'NSW'},
#  'merchant_info': None}
#
# `ast.literal_eval()` can safely parse Python literals such as dict,
# None, True and False, but it deliberately does not execute function
# calls. Convert the datetime constructor to a quoted ISO-8601 string
# before parsing the dictionary.

DATETIME_PATTERN = re.compile(
    r"datetime\.datetime\("
    r"\s*(\d+),"
    r"\s*(\d+),"
    r"\s*(\d+),"
    r"\s*(\d+),"
    r"\s*(\d+),"
    r"\s*(\d+)"
    r"(?:,\s*(\d+))?"
    r"\s*\)"
)


def replace_datetime(match):
    """
    Replace one datetime.datetime(...) expression with an ISO string.
    """

    values = [
        int(value) if value is not None else 0
        for value in match.groups()
    ]

    timestamp = datetime(*values)

    # repr() adds quotes so ast.literal_eval() interprets the replacement
    # as a normal Python string rather than executable syntax.
    return repr(timestamp.isoformat())


def parse_metadata(value):
    """
    Convert one source metadata string into a Python dictionary.
    """
    value = DATETIME_PATTERN.sub(replace_datetime, value)

    # Fail explicitly if the source contains a datetime representation
    # outside the pattern handled above. This avoids silently writing a
    # partially converted metadata value.
    if "datetime.datetime" in value:
        raise ValueError(
            "Unparsed datetime.datetime() expression remains."
        )

    metadata = ast.literal_eval(value)

    if not isinstance(metadata, dict):
        raise TypeError(
            f"Expected metadata to be dict, "
            f"got {type(metadata).__name__}."
        )

    return metadata


# ============================================================
# 2. Extract metadata fields for the audit
# ============================================================

def extract_audit_fields(metadata):

    """
    Return metadata fields planned for staging-table materialization.
    """

    # ``or {}`` makes nested lookups safe when the whole nested object is
    # represented by Python None / JSON null.
    location = metadata.get("location") or {}
    device_info = metadata.get("device_info") or {}
    merchant_info = metadata.get("merchant_info") or {}

    return {
        "transaction_timestamp": metadata.get("timestamp"),
        "state": location.get("state"),
        "city": location.get("city"),
        "postcode": location.get("postcode"),
        "device_type": device_info.get("type"),
        "device_os": device_info.get("os"),
        "payment_method": metadata.get("payment_method"),
        "merchant_id": merchant_info.get("merchant_id"),
        "merchant_category": merchant_info.get("category"),
        "merchant_risk_level": merchant_info.get("risk_level"),
        "merchant_avg_transaction": merchant_info.get(
            "avg_transaction"
        ),
    }


# ============================================================
# 3. Process CSV
# ============================================================

def process_csv():

    """
    Repair metadata while collecting completeness diagnostics.
    """

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Counter is convenient here because unseen keys default to zero.
    null_counts = Counter()
    sample_rows = []

    n_rows = 0
    n_missing_metadata = 0
    n_failed = 0

    # Track whether the top-level transaction category duplicates the
    # merchant category nested inside metadata.
    category_comparison = Counter()

    audit_fields = [
        "transaction_timestamp",
        "state",
        "city",
        "postcode",
        "device_type",
        "device_os",
        "payment_method",
        "merchant_id",
        "merchant_category",
        "merchant_risk_level",
        "merchant_avg_transaction",
    ]

    # Stream rows rather than loading the ~730 MB source CSV into memory.
    # csv.DictWriter preserves every source column and changes only metadata.
    with (
        INPUT_PATH.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as source,
        OUTPUT_PATH.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as target,
    ):
        reader = csv.DictReader(source)

        # Stop early if the input is structurally different from expected.
        if reader.fieldnames is None:
            raise ValueError("CSV has no header.")

        if "metadata" not in reader.fieldnames:
            raise ValueError(
                "CSV does not contain a metadata column."
            )

        writer = csv.DictWriter(
            target,
            fieldnames=reader.fieldnames,
        )
        writer.writeheader()

        # CSV row 1 is the header, so data starts at row 2.
        for row_number, row in enumerate(
            reader,
            start=2,
        ):
            n_rows += 1
            raw_metadata = row["metadata"]

            # ------------------------------------------------
            # Missing metadata
            # ------------------------------------------------

            # If the entire metadata field is empty, count every audited
            # nested field as missing and preserve the row unchanged.
            if not raw_metadata:
                n_missing_metadata += 1

                for field in audit_fields:
                    null_counts[field] += 1

                writer.writerow(row)
                continue

            # ------------------------------------------------
            # Parse metadata
            # ------------------------------------------------

            # Fail on the first malformed value so the corrected output
            # cannot be mistaken for a fully successful transformation.
            try:
                metadata = parse_metadata(raw_metadata)

            except (ValueError, SyntaxError, TypeError) as error:
                n_failed += 1

                print(
                    f"\nMetadata parsing failed at "
                    f"CSV row {row_number:,}"
                )
                print(raw_metadata[:500])

                raise error

            # ------------------------------------------------
            # Compare category fields
            # ------------------------------------------------

            # ``merchant_info`` is optional. The comparison distinguishes
            # true disagreements from cases where one side is simply absent.
            merchant_info = metadata.get("merchant_info") or {}

            category = row["category"]
            merchant_category = merchant_info.get("category")

            if not category and merchant_category is None:
                category_comparison["both_missing"] += 1

            elif not category:
                category_comparison["category_missing"] += 1

            elif merchant_category is None:
                category_comparison["merchant_category_missing"] += 1

            elif category == merchant_category:
                category_comparison["same"] += 1

            else:
                category_comparison["different"] += 1

            # ------------------------------------------------
            # Audit selected metadata fields
            # ------------------------------------------------

            audit_values = extract_audit_fields(metadata)

            for field, value in audit_values.items():
                if value is None:
                    null_counts[field] += 1

            # Keep only a small sample for visual inspection.
            if len(sample_rows) < SAMPLE_SIZE:
                sample_rows.append(audit_values)

            # ------------------------------------------------
            # Serialize as valid JSON
            # ------------------------------------------------

            # json.dumps() performs the correct recursive conversion:
            # Python None/True/False -> JSON null/true/false.
            fixed_metadata = json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            # Confirm that the generated text can be parsed back as JSON.
            # Because json.dumps() produced it, failure would indicate an
            # unexpected serialization problem.
            json.loads(fixed_metadata)

            # Replace only metadata; every other source field is untouched.
            row["metadata"] = fixed_metadata

            writer.writerow(row)

            # Lightweight progress reporting for the large source file.
            if n_rows % 100_000 == 0:
                print(
                    f"Processed {n_rows:,} rows..."
                )

    return {
        "n_rows": n_rows,
        "n_missing_metadata": n_missing_metadata,
        "n_failed": n_failed,
        "null_counts": null_counts,
        "sample_rows": sample_rows,
        "audit_fields": audit_fields,
        "category_comparison": category_comparison,
    }


# ============================================================
# 4. Display audit results
# ============================================================

def display_audit(results):

    """
    Print transformation status and metadata completeness results.
    """
    
    n_rows = results["n_rows"]

    print("\n" + "=" * 60)
    print("Metadata audit")
    print("=" * 60)

    print(f"Rows processed:     {n_rows:,}")
    print(
        f"Missing metadata:   "
        f"{results['n_missing_metadata']:,}"
    )
    print(
        f"Failed conversions: "
        f"{results['n_failed']:,}"
    )

    print("\nMetadata completeness:")

    for field in results["audit_fields"]:
        n_null = results["null_counts"][field]
        null_rate = (
            n_null / n_rows
            if n_rows
            else 0
        )

        print(
            f"{field:<24}"
            f"{n_null:>12,} nulls "
            f"({null_rate:>7.2%})"
        )

    print("\nCategory vs metadata merchant category:")

    comparison = results["category_comparison"]

    for key in [
        "same",
        "different",
        "category_missing",
        "merchant_category_missing",
        "both_missing",
    ]:
        count = comparison[key]
        rate = count / n_rows if n_rows else 0

        print(
            f"{key:<28}"
            f"{count:>12,} "
            f"({rate:>7.2%})"
        )

    print(
        f"\nFirst {len(results['sample_rows'])} "
        f"parsed metadata records:"
    )

    for i, row in enumerate(
        results["sample_rows"],
        start=1,
    ):
        print(f"{i:>3}: {row}")

    print("\nOutput:")
    print(OUTPUT_PATH.resolve())


# ============================================================
# 5. Run
# ============================================================

def main():
    """
    Run the repair and print the accompanying metadata audit.
    """
    
    results = process_csv()
    display_audit(results)


if __name__ == "__main__":
    main()
