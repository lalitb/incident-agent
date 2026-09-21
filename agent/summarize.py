import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import fmean


# These labels are useful for distinguishing application versions
# and instances. Omitted labels are listed explicitly.
KEEP_LABELS = {
    "service_name",
    "service_version",
    "service_instance_id",
    "deployment_environment_name",
    "instance",
    "job",
    "exported_job",
    "outcome",
}

METRIC_UNITS = {
    "request_rate": "requests/second",
    "request_duration_mean_seconds": "seconds",
    "connection_wait_mean_seconds": "seconds",
    "pool_limit": "connections",
    "pool_in_use": "connections",
    "pool_utilization": "ratio",
}


def utc(timestamp):
    return datetime.fromtimestamp(
        timestamp,
        timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def utc_nanoseconds(timestamp):
    seconds, nanoseconds = divmod(int(timestamp), 1_000_000_000)
    date = datetime.fromtimestamp(seconds, timezone.utc)
    return date.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanoseconds:09d}Z"


def rounded(value):
    if value is None:
        return None

    # Significant digits preserve tiny baseline wait times.
    return float(f"{value:.6g}")


def summarize_series(series, bucket_seconds):
    buckets = defaultdict(list)

    for point in series["points"]:
        timestamp = float(point["timestamp"])
        bucket_start = (
            math.floor(timestamp / bucket_seconds)
            * bucket_seconds
        )
        buckets[bucket_start].append(point)

    rows = []
    total_valid = 0
    total_missing = 0

    for bucket_start, points in sorted(buckets.items()):
        values = []

        for point in points:
            value = point["value"]

            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                values.append(float(value))

        valid_count = len(values)
        missing_count = len(points) - valid_count

        total_valid += valid_count
        total_missing += missing_count

        rows.append([
            utc(bucket_start),
            valid_count,
            missing_count,
            rounded(min(values)) if values else None,
            rounded(fmean(values)) if values else None,
            rounded(max(values)) if values else None,
        ])

    labels = series.get("labels", {})
    points = series["points"]

    timestamps = [
        float(point["timestamp"])
        for point in points
    ]

    return {
        "labels": {
            key: value
            for key, value in labels.items()
            if key in KEEP_LABELS
        },
        "omitted_label_names": sorted(
            key for key in labels if key not in KEEP_LABELS
        ),
        "returned_sample_range": (
            {
                "first": utc(min(timestamps)),
                "last": utc(max(timestamps)),
            }
            if timestamps
            else None
        ),
        "valid_samples": total_valid,
        "missing_samples": total_missing,
        "buckets": rows,
    }


def summarize_evidence(items, bucket_seconds=60):
    if type(bucket_seconds) is not int or bucket_seconds < 1:
        raise ValueError("bucket_seconds must be a positive integer")

    summarized = []

    for item in items:
        if item.get("tool") != "query_metrics":
            timestamp_field = {
                "get_trace": "start_time_unix_nano",
                "find_traces": "start_time_unix_nano",
                "search_logs": "timestamp_unix_nano",
            }.get(item.get("tool"))
            if timestamp_field:
                rows = [dict(row) for row in item["data"]]
                for row in rows:
                    if row.get(timestamp_field) is not None:
                        row["source_timestamp_utc"] = utc_nanoseconds(row[timestamp_field])
                item = {**item, "data": rows}
            summarized.append(item)
            continue

        result = {
            key: value
            for key, value in item.items()
            if key != "data"
        }

        result["representation"] = "metric_bucket_summary"
        result["unit"] = METRIC_UNITS.get(
            item.get("metric"),
            "unspecified",
        )
        result["bucket_seconds"] = bucket_seconds

        # Columns defined once instead of repeated for every row.
        result["bucket_columns"] = [
            "bucket_start_utc",
            "valid_samples",
            "missing_samples",
            "minimum",
            "sample_mean",
            "maximum",
        ]

        result["data"] = [
            summarize_series(series, bucket_seconds)
            for series in item["data"]
        ]

        if item.get("metric") == "pool_limit":
            # Bucketing would otherwise hide intermediate configuration values.
            for original, summary in zip(item["data"], result["data"]):
                summary["observed_values"] = sorted({
                    point["value"] for point in original["points"]
                    if type(point["value"]) in (int, float)
                    and math.isfinite(point["value"])
                })

        summarized.append(result)

    return summarized
