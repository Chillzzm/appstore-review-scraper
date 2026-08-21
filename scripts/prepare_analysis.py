#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare App Store review data for evidence-backed analysis.

The script is deliberately model-free. It profiles the complete normalized
dataset, creates a reproducible stratified sample, and aggregates externally
produced theme/JTBD annotations without estimating counts in prose.
"""

import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys


SCHEMA_VERSION = 2
LARGE_REVIEW_THRESHOLD = 2000
LARGE_CHARACTER_THRESHOLD = 1000000
EVIDENCE_LEVELS = {"observation", "inference", "hypothesis"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def atomic_write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = "%s.tmp.%s" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _review_key(app_id, country, review_id):
    return "%s:%s:%s" % (str(app_id), str(country).lower(), str(review_id))


def _content_hash(row):
    fields = (
        "app_id", "country", "review_id", "rating", "title", "review",
        "username", "date", "version", "is_edited", "developer_response_id",
        "developer_response_body", "developer_response_date",
    )
    payload = {field: row.get(field) for field in fields}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _corpus_hash(rows):
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["review_key"]):
        digest.update(row["review_key"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(row["content_hash"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _valid_text_rows(rows):
    return [row for row in rows if str(row.get("review") or "").strip()]


def _parse_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _path_identity(path):
    """Infer app ID and country from apps/<id>/reviews/<country>/reviews.json."""
    parts = os.path.normpath(path).split(os.sep)
    try:
        apps_index = len(parts) - 1 - parts[::-1].index("apps")
        if parts[apps_index + 2] != "reviews":
            return None, None
        return parts[apps_index + 1], parts[apps_index + 3].lower()
    except (ValueError, IndexError):
        return None, None


def _normalize_rating(value):
    try:
        rating = int(value)
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 5 else None


def _quarter(value):
    date_value = _parse_date(value)
    if date_value is None:
        return "unknown"
    return "%04d-Q%d" % (date_value.year, ((date_value.month - 1) // 3) + 1)


def _rating_band(rating):
    if rating in (1, 2):
        return "low_1_2"
    if rating == 3:
        return "neutral_3"
    if rating in (4, 5):
        return "high_4_5"
    return "unknown"


def _manifest_scope(run_dir):
    path = os.path.join(os.path.abspath(run_dir), "manifest.json")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as error:
        raise ValueError("无法读取 manifest.json: %s" % error)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("apps"), list):
        raise ValueError("manifest.json 缺少有效 apps")
    app_ids = {
        str(item.get("app_id")) for item in manifest["apps"]
        if isinstance(item, dict) and item.get("app_id")
    }
    if not app_ids:
        raise ValueError("manifest.json 没有有效 App")
    review_pairs = None
    if "reviews" in manifest:
        if not isinstance(manifest["reviews"], list):
            raise ValueError("manifest.json 的 reviews 必须是数组")
        review_pairs = {
            (str(item.get("app_id")), str(item.get("country") or "").lower())
            for item in manifest["reviews"]
            if isinstance(item, dict) and item.get("app_id") and item.get("country")
        }
    return app_ids, review_pairs


def review_files(run_dir):
    pattern = os.path.join(os.path.abspath(run_dir), "apps", "*", "reviews", "*", "reviews.json")
    app_ids, review_pairs = _manifest_scope(run_dir)
    paths = []
    for path in sorted(glob.glob(pattern)):
        app_id, country = _path_identity(path)
        if app_ids is not None and app_id not in app_ids:
            continue
        if review_pairs is not None and (app_id, country) not in review_pairs:
            continue
        paths.append(path)
    return paths


def load_reviews(run_dir):
    """Load and deduplicate normalized reviews from a completed run directory."""
    rows_by_key = {}
    stats = {
        "files": [],
        "raw_rows": 0,
        "duplicate_rows": 0,
        "invalid_rows": 0,
    }
    paths = review_files(run_dir)
    if not paths:
        raise ValueError("run-dir 下没有 apps/*/reviews/*/reviews.json；请先完成评论抓取")
    for path in paths:
        stats["files"].append(os.path.abspath(path))
        app_from_path, country_from_path = _path_identity(path)
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as error:
            raise ValueError("无法读取规范化评论文件 %s: %s" % (path, error))
        if not isinstance(document, list):
            raise ValueError("评论文件必须是 JSON 数组: %s" % path)
        for raw in document:
            stats["raw_rows"] += 1
            if not isinstance(raw, dict):
                stats["invalid_rows"] += 1
                continue
            if not app_from_path or not country_from_path:
                raise ValueError("无法从评论路径识别 app_id/country: %s" % path)
            raw_app_id = str(raw.get("app_id") or "")
            raw_country = str(raw.get("country") or "").lower()
            if ((raw_app_id and raw_app_id != app_from_path)
                    or (raw_country and raw_country != country_from_path)):
                raise ValueError(
                    "评论中的 app_id/country 与目录不一致: %s" % path
                )
            app_id = app_from_path
            country = country_from_path
            review_id = str(raw.get("review_id") or raw.get("id") or "")
            if not app_id or not country or not review_id:
                stats["invalid_rows"] += 1
                continue
            key = _review_key(app_id, country, review_id)
            row = dict(raw)
            row.update({
                "app_id": app_id,
                "country": country,
                "review_id": review_id,
                "review_key": key,
                "rating": _normalize_rating(raw.get("rating")),
            })
            row["content_hash"] = _content_hash(row)
            if key in rows_by_key:
                if rows_by_key[key]["content_hash"] != row["content_hash"]:
                    raise ValueError("同一 review_key 出现内容冲突: %s" % key)
                stats["duplicate_rows"] += 1
                continue
            rows_by_key[key] = row
    return [rows_by_key[key] for key in sorted(rows_by_key)], stats


def _load_dataset_metadata(run_dir):
    pattern = os.path.join(
        os.path.abspath(run_dir), "apps", "*", "reviews", "*", "dataset.json"
    )
    app_ids, review_pairs = _manifest_scope(run_dir)
    metadata = {}
    for path in sorted(glob.glob(pattern)):
        app_id, country = _path_identity(path)
        if app_ids is not None and app_id not in app_ids:
            continue
        if review_pairs is not None and (app_id, country) not in review_pairs:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError) as error:
            raise ValueError("无法读取 dataset.json %s: %s" % (path, error))
        if not app_id or not country or not isinstance(value, dict):
            raise ValueError("dataset.json 路径或内容无效: %s" % path)
        value_app_id = str(value.get("app_id") or app_id)
        value_country = str(value.get("country") or country).lower()
        if value_app_id != app_id or value_country != country:
            raise ValueError("dataset.json 身份与目录不一致: %s" % path)
        metadata[(app_id, country)] = value
    return metadata


def _summarize_reviews(rows, metadata=None):
    metadata = metadata or {}
    rating_counts = {str(rating): 0 for rating in range(1, 6)}
    valid_ratings = []
    dates = []
    quarters = collections.Counter()
    quarterly_ratings = collections.defaultdict(list)
    missing = collections.Counter()
    invalid = collections.Counter()
    valid_text_count = 0
    text_character_count = 0
    developer_response_count = 0
    for row in rows:
        rating = row.get("rating")
        if rating is None:
            missing["rating"] += 1
        else:
            rating_counts[str(rating)] += 1
            valid_ratings.append(rating)
        date_value = _parse_date(row.get("date"))
        if date_value is not None:
            dates.append(date_value)
        elif row.get("date"):
            invalid["date"] += 1
        else:
            missing["date"] += 1
        if not str(row.get("title") or "").strip():
            missing["title"] += 1
        text = str(row.get("review") or "")
        if text.strip():
            valid_text_count += 1
            text_character_count += len(text)
        else:
            missing["review"] += 1
        if str(row.get("developer_response_body") or "").strip():
            developer_response_count += 1
        quarter = _quarter(row.get("date"))
        quarters[quarter] += 1
        if rating is not None:
            quarterly_ratings[quarter].append(rating)
    app_id = rows[0]["app_id"] if rows else None
    country = rows[0]["country"] if rows else None
    dataset = metadata.get((app_id, country), {}) if app_id and country else {}
    return {
        "review_count": len(rows),
        "valid_text_count": valid_text_count,
        "text_character_count": text_character_count,
        "rating_distribution": rating_counts,
        "rating_percentages": {
            key: (round(value / float(len(valid_ratings)), 6) if valid_ratings else 0.0)
            for key, value in rating_counts.items()
        },
        "rating_denominator": len(valid_ratings),
        "average_rating": (
            round(sum(valid_ratings) / float(len(valid_ratings)), 6)
            if valid_ratings else None
        ),
        "earliest_date": min(dates).isoformat() if dates else None,
        "latest_date": max(dates).isoformat() if dates else None,
        "quarter_distribution": dict(sorted(quarters.items())),
        "quarterly_rating_trend": [
            {
                "quarter": quarter,
                "review_count": quarters[quarter],
                "valid_rating_count": len(quarterly_ratings.get(quarter, [])),
                "average_rating": (
                    round(
                        sum(quarterly_ratings[quarter])
                        / float(len(quarterly_ratings[quarter])), 6
                    ) if quarterly_ratings.get(quarter) else None
                ),
                "rating_distribution": {
                    str(rating): quarterly_ratings.get(quarter, []).count(rating)
                    for rating in range(1, 6)
                },
            }
            for quarter in sorted(quarters)
        ],
        "developer_response_count": developer_response_count,
        "missing_fields": dict(sorted(missing.items())),
        "invalid_fields": dict(sorted(invalid.items())),
        "language": {
            "status": "not_available_in_source_schema",
            "unknown_text_count": valid_text_count,
            "unknown_text_rate": 1.0 if valid_text_count else 0.0,
        },
        "crawl_status": dataset.get("status"),
        "truncated": dataset.get("truncated"),
        "coverage": dataset.get("coverage"),
    }


def _analysis_population(rows, app_ids, countries=None):
    """Build a same-country, common-date population for multi-App comparison."""
    valid_text_rows = _valid_text_rows(rows)
    app_ids = sorted(set(str(value) for value in app_ids))
    countries = sorted(
        set(countries or []) | set(row["country"] for row in valid_text_rows)
    )
    population = []
    windows = []
    for country in countries:
        per_app = []
        valid_ranges = []
        for app_id in app_ids:
            app_rows = [
                row for row in valid_text_rows
                if row["app_id"] == app_id and row["country"] == country
            ]
            dates = [_parse_date(row.get("date")) for row in app_rows]
            valid_dates = [value for value in dates if value is not None]
            item = {
                "app_id": app_id,
                "source_valid_text_count": len(app_rows),
                "valid_date_count": len(valid_dates),
                "unknown_or_invalid_date_count": len(dates) - len(valid_dates),
                "earliest_date": min(valid_dates).isoformat() if valid_dates else None,
                "latest_date": max(valid_dates).isoformat() if valid_dates else None,
            }
            per_app.append(item)
            if valid_dates:
                valid_ranges.append((min(valid_dates), max(valid_dates)))
        date_complete = len(valid_ranges) == len(app_ids) and bool(app_ids)
        start = max((value[0] for value in valid_ranges), default=None)
        end = min((value[1] for value in valid_ranges), default=None)
        has_overlap = bool(date_complete and start <= end)
        if len(app_ids) < 2:
            included = [
                row for row in valid_text_rows if row["country"] == country
            ]
            population.extend(included)
            included_by_app = collections.Counter(row["app_id"] for row in included)
            reason = "single_app"
            comparable = False
        elif has_overlap:
            included = [
                row for row in valid_text_rows
                if row["country"] == country
                and _parse_date(row.get("date")) is not None
                and start <= _parse_date(row.get("date")) <= end
            ]
            included_by_app = collections.Counter(row["app_id"] for row in included)
            comparable = all(included_by_app[app_id] > 0 for app_id in app_ids)
            if comparable:
                population.extend(included)
                reason = "common_window_applied"
            else:
                reason = "empty_app_after_common_window"
        else:
            included_by_app = collections.Counter()
            comparable = False
            reason = "missing_app_or_valid_date_or_no_overlap"
        for item in per_app:
            item["analysis_population_count"] = included_by_app[item["app_id"]]
            item["excluded_from_analysis_count"] = (
                item["source_valid_text_count"]
                - item["analysis_population_count"]
            )
        windows.append({
            "country": country,
            "app_count": len(app_ids),
            "per_app": per_app,
            "common_start_date": start.isoformat() if has_overlap else None,
            "common_end_date": end.isoformat() if has_overlap else None,
            "comparable_for_cross_app_time_window": comparable,
            "reason": reason,
        })
    return sorted(population, key=lambda row: row["review_key"]), windows


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = "%s.tmp.%s" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        for row in sorted(rows, key=lambda item: item["review_key"]):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build_profile(run_dir):
    rows, load_stats = load_reviews(run_dir)
    metadata = _load_dataset_metadata(run_dir)
    manifest_app_ids, _ = _manifest_scope(run_dir)
    app_ids = sorted(manifest_app_ids or (
        set(row["app_id"] for row in rows)
        | set(app_id for app_id, _ in metadata)
    ))
    population, comparison_windows = _analysis_population(
        rows, app_ids, [country for _, country in metadata]
    )
    by_dataset = collections.defaultdict(list)
    by_app = collections.defaultdict(list)
    for row in rows:
        by_dataset[(row["app_id"], row["country"])].append(row)
        by_app[row["app_id"]].append(row)

    datasets = []
    for app_id, country in sorted(set(by_dataset) | set(metadata)):
        summary = _summarize_reviews(by_dataset.get((app_id, country), []))
        dataset_meta = metadata.get((app_id, country), {})
        summary.update({
            "crawl_status": dataset_meta.get("status"),
            "truncated": dataset_meta.get("truncated"),
            "coverage": dataset_meta.get("coverage"),
            "stop_reason": dataset_meta.get("stop_reason"),
            "requested_since": dataset_meta.get("requested_since"),
        })
        summary.update({"app_id": app_id, "country": country})
        datasets.append(summary)

    apps = []
    for app_id in sorted(by_app):
        summary = _summarize_reviews(by_app[app_id])
        summary.update({"app_id": app_id})
        apps.append(summary)

    overall = _summarize_reviews(rows)
    population_by_dataset = collections.defaultdict(list)
    population_by_app = collections.defaultdict(list)
    for row in population:
        population_by_dataset[(row["app_id"], row["country"])].append(row)
        population_by_app[row["app_id"]].append(row)
    window_by_country = {item["country"]: item for item in comparison_windows}
    population_datasets = []
    for app_id, country in sorted(set(by_dataset) | set(metadata)):
        summary = _summarize_reviews(
            population_by_dataset.get((app_id, country), [])
        )
        dataset_meta = metadata.get((app_id, country), {})
        window = window_by_country.get(country, {})
        summary.update({
            "app_id": app_id,
            "country": country,
            "common_start_date": window.get("common_start_date"),
            "common_end_date": window.get("common_end_date"),
            "comparable_for_cross_app_time_window": window.get(
                "comparable_for_cross_app_time_window", False
            ),
            "crawl_status": dataset_meta.get("status"),
            "truncated": dataset_meta.get("truncated"),
            "coverage": dataset_meta.get("coverage"),
            "stop_reason": dataset_meta.get("stop_reason"),
        })
        population_datasets.append(summary)
    population_apps = []
    for app_id in app_ids:
        summary = _summarize_reviews(population_by_app.get(app_id, []))
        summary.update({"app_id": app_id})
        population_apps.append(summary)
    population_overall = _summarize_reviews(population)
    population_country_count = len(set(row["country"] for row in population))
    app_rollups_safe = len(app_ids) < 2 or population_country_count <= 1
    analysis_dir = os.path.join(os.path.abspath(run_dir), "analysis")
    population_path = os.path.join(analysis_dir, "population.jsonl")
    _write_jsonl(population_path, population)
    profile = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "run_dir": os.path.abspath(run_dir),
        "source_files": load_stats["files"],
        "raw_row_count": load_stats["raw_rows"],
        "duplicate_row_count": load_stats["duplicate_rows"],
        "invalid_row_count": load_stats["invalid_rows"],
        "source_statistics": {
            "scope": "全部规范化 API 可见记录；只用于数据质量和单组描述。",
            "overall": overall,
            "apps": apps,
            "datasets": datasets,
        },
        "analysis_population": {
            "definition": (
                "单 App 为非空正文；多 App 仅保留同一 country 中所有 App 都有数据且"
                "日期落在共同交集窗口内的本次 API 可见评论。"
            ),
            "review_count": len(population),
            "text_character_count": sum(
                len(str(row.get("review") or "")) for row in population
            ),
            "sha256": _corpus_hash(population),
            "path": population_path,
            "overall": population_overall,
            "overall_scope": "descriptive_only_across_countries",
            "app_rollup_status": (
                "available_single_country_or_single_app"
                if app_rollups_safe else
                "omitted_multi_country_composition_not_standardized"
            ),
            "apps": population_apps if app_rollups_safe else [],
            "datasets": population_datasets,
            "comparison_windows": comparison_windows,
        },
        "large_dataset": (
            len(population) > LARGE_REVIEW_THRESHOLD
            or sum(len(str(row.get("review") or "")) for row in population)
            > LARGE_CHARACTER_THRESHOLD
        ),
        "large_dataset_thresholds": {
            "valid_text_count": LARGE_REVIEW_THRESHOLD,
            "text_character_count": LARGE_CHARACTER_THRESHOLD,
        },
    }
    output_path = os.path.join(os.path.abspath(run_dir), "analysis", "profile.json")
    atomic_write_json(output_path, profile)
    return profile, output_path


def _allocate_groups(groups, capacity, seed, *scope):
    """Allocate quota across groups, covering groups before repeat picks."""
    sizes = {key: len(value) for key, value in groups.items() if value}
    total = sum(sizes.values())
    if capacity >= total:
        return dict(sizes)
    keys = sorted(sizes)
    allocations = {key: 0 for key in keys}
    remaining = capacity
    while remaining > 0:
        candidates = [key for key in keys if allocations[key] < sizes[key]]
        if not candidates:
            break
        chosen = min(candidates, key=lambda key: (
            allocations[key] > 0,
            allocations[key] / float(sizes[key]),
            _stable_seed(seed, *(scope + (key,))),
        ))
        allocations[chosen] += 1
        remaining -= 1
    return allocations


def _allocate_strata(strata, capacity, seed, app_id):
    """Allocate hierarchically by country, rating band, then quarter."""
    by_country = collections.defaultdict(list)
    for key, values in strata.items():
        by_country[key[0]].extend(values)
    country_allocations = _allocate_groups(
        by_country, capacity, seed, app_id, "country"
    )
    allocations = {key: 0 for key in strata}
    for country in sorted(by_country):
        country_capacity = country_allocations.get(country, 0)
        by_rating = collections.defaultdict(list)
        for key, values in strata.items():
            if key[0] == country:
                by_rating[key[1]].extend(values)
        rating_allocations = _allocate_groups(
            by_rating, country_capacity, seed, app_id, country, "rating"
        )
        for rating_band in sorted(by_rating):
            rating_capacity = rating_allocations.get(rating_band, 0)
            by_quarter = {
                key[2]: values for key, values in strata.items()
                if key[0] == country and key[1] == rating_band
            }
            quarter_allocations = _allocate_groups(
                by_quarter, rating_capacity, seed,
                app_id, country, rating_band, "quarter"
            )
            for quarter, count in quarter_allocations.items():
                allocations[(country, rating_band, quarter)] = count
    return allocations


def _stable_seed(seed, *parts):
    material = "%s|%s" % (seed, "|".join(str(part) for part in parts))
    return int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:16], 16)


def build_sample(run_dir, per_app=2000, seed=42):
    if per_app <= 0:
        raise ValueError("--per-app 必须大于 0")
    source_rows, _ = load_reviews(run_dir)
    dataset_metadata = _load_dataset_metadata(run_dir)
    manifest_app_ids, _ = _manifest_scope(run_dir)
    app_ids = sorted(manifest_app_ids or (
        set(row["app_id"] for row in source_rows)
        | set(app_id for app_id, _ in dataset_metadata)
    ))
    rows, comparison_windows = _analysis_population(
        source_rows, app_ids, [country for _, country in dataset_metadata]
    )
    if not rows:
        raise ValueError("没有非空且满足共同时间窗口的评论，无法生成分析样本")
    by_app = collections.defaultdict(list)
    for row in rows:
        by_app[row["app_id"]].append(row)

    selected = []
    app_metadata = []
    for app_id in sorted(by_app):
        app_rows = by_app[app_id]
        strata = collections.defaultdict(list)
        for row in app_rows:
            key = (row["country"], _rating_band(row.get("rating")), _quarter(row.get("date")))
            strata[key].append(row)
        allocations = _allocate_strata(
            strata, min(per_app, len(app_rows)), seed, app_id
        )
        app_selected = []
        for stratum in sorted(strata):
            count = allocations.get(stratum, 0)
            candidates = sorted(strata[stratum], key=lambda row: row["review_key"])
            if count >= len(candidates):
                chosen = candidates
            else:
                chosen = sorted(candidates, key=lambda row: (
                    _stable_seed(seed, app_id, *stratum, row["review_key"]),
                    row["review_key"],
                ))[:count]
            app_selected.extend(chosen)
        app_selected.sort(key=lambda row: row["review_key"])
        selected.extend(app_selected)
        stratum_metadata = []
        for stratum in sorted(strata):
            population_count = len(strata[stratum])
            sampled_count = allocations.get(stratum, 0)
            stratum_metadata.append({
                "country": stratum[0],
                "rating_band": stratum[1],
                "calendar_quarter": stratum[2],
                "population_count": population_count,
                "sampled_count": sampled_count,
                "inclusion_probability": (
                    round(sampled_count / float(population_count), 12)
                    if population_count else 0.0
                ),
                "analysis_weight": (
                    round(population_count / float(sampled_count), 12)
                    if sampled_count else None
                ),
            })
        app_metadata.append({
            "app_id": app_id,
            "analysis_population_count": len(app_rows),
            "sampled_review_count": len(app_selected),
            "sampling_fraction": (
                round(len(app_selected) / float(len(app_rows)), 6)
                if app_rows else 0.0
            ),
            "stratum_count": len(strata),
            "represented_stratum_count": sum(
                1 for value in allocations.values() if value > 0
            ),
            "strata": stratum_metadata,
        })

    analysis_dir = os.path.join(os.path.abspath(run_dir), "analysis")
    sample_path = os.path.join(analysis_dir, "sample.jsonl")
    _write_jsonl(sample_path, selected)
    source_by_dataset = collections.Counter(
        (row["app_id"], row["country"]) for row in source_rows
    )
    valid_text_by_dataset = collections.Counter(
        (row["app_id"], row["country"])
        for row in _valid_text_rows(source_rows)
    )
    population_by_dataset = collections.Counter(
        (row["app_id"], row["country"]) for row in rows
    )
    sample_by_dataset = collections.Counter(
        (row["app_id"], row["country"]) for row in selected
    )
    datasets = []
    for app_id, country in sorted(
            set(source_by_dataset) | set(dataset_metadata)):
        value = dataset_metadata.get((app_id, country), {})
        datasets.append({
            "app_id": app_id,
            "country": country,
            "source_normalized_count": source_by_dataset[(app_id, country)],
            "source_valid_text_count": valid_text_by_dataset[(app_id, country)],
            "analysis_population_count": population_by_dataset[(app_id, country)],
            "sampled_review_count": sample_by_dataset[(app_id, country)],
            "crawl_status": value.get("status"),
            "truncated": value.get("truncated"),
            "coverage": value.get("coverage"),
            "stop_reason": value.get("stop_reason"),
        })
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "seed": seed,
        "per_app_limit": per_app,
        "stratification": ["app_id", "country", "rating_band", "calendar_quarter"],
        "source_normalized_count": len(source_rows),
        "source_valid_text_count": len(_valid_text_rows(source_rows)),
        "analysis_population_definition": (
            "单 App 为非空正文；多 App 为同 country 共同日期交集内的非空正文。"
        ),
        "analysis_population_count": len(rows),
        "analysis_population_sha256": _corpus_hash(rows),
        "sampled_review_count": len(selected),
        "sample_sha256": _corpus_hash(selected),
        "comparison_windows": comparison_windows,
        "apps": app_metadata,
        "datasets": datasets,
        "sample_path": sample_path,
    }
    metadata_path = os.path.join(analysis_dir, "sample_metadata.json")
    atomic_write_json(metadata_path, metadata)
    return selected, metadata, sample_path


def load_annotations(path, review_index):
    annotations = []
    seen = set()
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as error:
                raise ValueError("annotations 第 %s 行不是有效 JSON: %s" % (line_number, error))
            if not isinstance(item, dict):
                raise ValueError("annotations 第 %s 行必须是 JSON 对象" % line_number)
            required = (
                "review_key", "review_id", "app_id", "country", "content_hash",
                "theme_ids", "jtbd_ids", "evidence_level", "confidence",
            )
            missing = [field for field in required if field not in item]
            if missing:
                raise ValueError("annotations 第 %s 行缺少字段: %s" % (line_number, ", ".join(missing)))
            for field in ("review_key", "review_id", "app_id", "country", "content_hash"):
                if not isinstance(item[field], str) or not item[field]:
                    raise ValueError("annotations 第 %s 行 %s 必须是非空字符串" % (line_number, field))
            if item["country"] != item["country"].lower():
                raise ValueError("annotations 第 %s 行 country 必须为小写" % line_number)
            key = _review_key(item["app_id"], item["country"], item["review_id"])
            if item["review_key"] != key:
                raise ValueError("annotations 第 %s 行 review_key 与复合键不一致" % line_number)
            if key not in review_index:
                raise ValueError("annotations 第 %s 行引用未知 review_key: %s" % (line_number, key))
            if item["content_hash"] != review_index[key]["content_hash"]:
                raise ValueError("annotations 第 %s 行 content_hash 已过期或不匹配" % line_number)
            if key in seen:
                raise ValueError("annotations 含重复 review_key: %s" % key)
            seen.add(key)
            for field in ("theme_ids", "jtbd_ids"):
                values = item[field]
                if not isinstance(values, list) or any(
                        not isinstance(value, str) or not value.strip() for value in values):
                    raise ValueError("annotations 第 %s 行 %s 必须是字符串数组" % (line_number, field))
                item[field] = sorted(set(value.strip() for value in values))
                pattern = r"T[0-9]{2,3}" if field == "theme_ids" else r"J[0-9]{2,3}"
                invalid_labels = [
                    value for value in item[field]
                    if re.fullmatch(pattern, value) is None
                ]
                if invalid_labels:
                    raise ValueError(
                        "annotations 第 %s 行 %s 含无效 ID: %s"
                        % (line_number, field, ", ".join(invalid_labels))
                    )
            if item["evidence_level"] not in EVIDENCE_LEVELS:
                raise ValueError("annotations 第 %s 行 evidence_level 无效" % line_number)
            if item["confidence"] not in CONFIDENCE_LEVELS:
                raise ValueError("annotations 第 %s 行 confidence 无效" % line_number)
            annotations.append(item)
    if not annotations:
        raise ValueError("annotations 文件没有有效记录")
    return annotations


def _stratum_key(row):
    return (
        row["app_id"], row["country"],
        _rating_band(row.get("rating")), _quarter(row.get("date")),
    )


def _load_sample_index(run_dir, review_index):
    sample_path = os.path.join(os.path.abspath(run_dir), "analysis", "sample.jsonl")
    if not os.path.exists(sample_path):
        return None
    sample_index = {}
    with open(sample_path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as error:
                raise ValueError("sample.jsonl 第 %s 行无效: %s" % (line_number, error))
            if not isinstance(row, dict) or not isinstance(row.get("review_key"), str):
                raise ValueError("sample.jsonl 第 %s 行缺少 review_key" % line_number)
            key = row["review_key"]
            if key not in review_index:
                raise ValueError("sample.jsonl 引用当前总体之外的 review_key: %s" % key)
            if key in sample_index:
                raise ValueError("sample.jsonl 含重复 review_key: %s" % key)
            if row.get("content_hash") != review_index[key]["content_hash"]:
                raise ValueError("sample.jsonl 的 content_hash 已过期: %s" % key)
            sample_index[key] = review_index[key]
    if not sample_index:
        raise ValueError("sample.jsonl 没有有效记录")
    metadata_path = os.path.join(
        os.path.abspath(run_dir), "analysis", "sample_metadata.json"
    )
    try:
        with open(metadata_path, encoding="utf-8") as handle:
            metadata = json.load(handle)
    except (OSError, ValueError) as error:
        raise ValueError("无法读取 sample_metadata.json: %s" % error)
    if metadata.get("analysis_population_sha256") != _corpus_hash(review_index.values()):
        raise ValueError("sample 来自旧的分析总体；请重新运行 sample")
    if metadata.get("sample_sha256") != _corpus_hash(sample_index.values()):
        raise ValueError("sample.jsonl 与 sample_metadata.json 不一致")
    return sample_index


def _label_summary(annotations, review_index, field, label_ids, raw_denominator,
                   weights, weighted_denominator, population_estimate_available,
                   mode):
    labels = collections.defaultdict(list)
    for annotation in annotations:
        for label_id in annotation[field]:
            labels[label_id].append(annotation)
    output = []
    for label_id in sorted(label_ids):
        items = labels[label_id]
        rated_items = [
            item for item in items
            if review_index[item["review_key"]].get("rating") is not None
        ]
        sample_ratings = [
            review_index[item["review_key"]]["rating"] for item in rated_items
        ]
        weighted_count = sum(weights[item["review_key"]] for item in items)
        weighted_rating_denominator = sum(
            weights[item["review_key"]] for item in rated_items
        )
        weighted_rating_sum = sum(
            review_index[item["review_key"]]["rating"]
            * weights[item["review_key"]]
            for item in rated_items
        )
        weighted_low_count = sum(
            weights[item["review_key"]] for item in rated_items
            if review_index[item["review_key"]]["rating"] <= 2
        )
        evidence = collections.Counter(item["evidence_level"] for item in items)
        confidence = collections.Counter(item["confidence"] for item in items)
        output.append({
            "id": label_id,
            "sample_mention_count": len(items),
            "sample_denominator": raw_denominator,
            "sample_mention_rate": (
                round(len(items) / float(raw_denominator), 6)
                if raw_denominator else None
            ),
            "estimated_population_mention_count": (
                round(weighted_count, 6) if population_estimate_available else None
            ),
            "estimated_population_denominator": (
                round(weighted_denominator, 6)
                if population_estimate_available else None
            ),
            "estimated_population_mention_rate": (
                round(weighted_count / float(weighted_denominator), 6)
                if population_estimate_available and weighted_denominator else None
            ),
            "population_metric_status": (
                "exact_complete_api_visible_population"
                if mode == "full" else
                "weighted_api_visible_population_estimate"
                if population_estimate_available else
                "unavailable_unrepresented_strata"
            ),
            "sample_valid_rating_count": len(sample_ratings),
            "sample_average_rating": (
                round(sum(sample_ratings) / float(len(sample_ratings)), 6)
                if sample_ratings else None
            ),
            "estimated_population_average_rating": (
                round(weighted_rating_sum / float(weighted_rating_denominator), 6)
                if population_estimate_available and weighted_rating_denominator else None
            ),
            "sample_low_rating_count": sum(
                1 for rating in sample_ratings if rating <= 2
            ),
            "sample_low_rating_rate": (
                round(sum(1 for rating in sample_ratings if rating <= 2)
                      / float(len(sample_ratings)), 6)
                if sample_ratings else None
            ),
            "estimated_population_low_rating_rate": (
                round(weighted_low_count / float(weighted_rating_denominator), 6)
                if population_estimate_available and weighted_rating_denominator else None
            ),
            "evidence_levels": dict(sorted(evidence.items())),
            "confidence": dict(sorted(confidence.items())),
            "review_keys": sorted(item["review_key"] for item in items),
        })
    return output


def aggregate_annotations(run_dir, annotations_path):
    source_reviews, _ = load_reviews(run_dir)
    metadata = _load_dataset_metadata(run_dir)
    manifest_app_ids, _ = _manifest_scope(run_dir)
    app_ids = sorted(manifest_app_ids or (
        set(row["app_id"] for row in source_reviews)
        | set(app_id for app_id, _ in metadata)
    ))
    reviews, comparison_windows = _analysis_population(
        source_reviews, app_ids, [country for _, country in metadata]
    )
    if not reviews:
        raise ValueError("没有非空且满足共同时间窗口的评论，无法聚合分析")
    review_index = {row["review_key"]: row for row in reviews}
    annotations = load_annotations(annotations_path, review_index)
    annotation_keys = set(item["review_key"] for item in annotations)
    full_keys = set(review_index)
    if annotation_keys == full_keys:
        mode = "full"
        expected_index = review_index
    else:
        sample_index = _load_sample_index(run_dir, review_index)
        sample_keys = set(sample_index or {})
        if sample_index is not None and annotation_keys == sample_keys:
            mode = "sample"
            expected_index = sample_index
        else:
            expected = sample_keys if sample_index is not None else full_keys
            missing = len(expected - annotation_keys)
            unexpected = len(annotation_keys - expected)
            raise ValueError(
                "annotations 必须完整覆盖全量总体或当前 sample：缺少 %s，超出 %s"
                % (missing, unexpected)
            )

    population_strata = collections.Counter(_stratum_key(row) for row in reviews)
    expected_strata = collections.Counter(
        _stratum_key(row) for row in expected_index.values()
    )
    weights = {}
    for key, row in expected_index.items():
        stratum = _stratum_key(row)
        weights[key] = (
            population_strata[stratum] / float(expected_strata[stratum])
        )
    source_by_group = collections.Counter(
        (row["app_id"], row["country"]) for row in source_reviews
    )
    valid_text_by_group = collections.Counter(
        (row["app_id"], row["country"])
        for row in _valid_text_rows(source_reviews)
    )
    population_by_group = collections.Counter(
        (row["app_id"], row["country"]) for row in reviews
    )
    expected_by_group = collections.Counter(
        (row["app_id"], row["country"]) for row in expected_index.values()
    )
    grouped = collections.defaultdict(list)
    for annotation in annotations:
        grouped[(str(annotation["app_id"]), str(annotation["country"]).lower())].append(annotation)

    theme_ids = set(
        label for item in annotations for label in item["theme_ids"]
    )
    jtbd_ids = set(
        label for item in annotations for label in item["jtbd_ids"]
    )
    observed_label_ids = {
        "theme_ids": sorted(theme_ids),
        "jtbd_ids": sorted(jtbd_ids),
        "theme_id_pattern": "T[0-9]{2,3}",
        "jtbd_id_pattern": "J[0-9]{2,3}",
    }
    observed_label_ids["sha256"] = hashlib.sha256(
        json.dumps(
            observed_label_ids, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    group_keys = (
        set(source_by_group) | set(population_by_group) | set(metadata)
    )
    groups = []
    for app_id, country in sorted(group_keys):
        items = grouped[(app_id, country)]
        raw_denominator = expected_by_group[(app_id, country)]
        group_population_strata = {
            stratum: count for stratum, count in population_strata.items()
            if stratum[0] == app_id and stratum[1] == country
        }
        represented_population_count = sum(
            count for stratum, count in group_population_strata.items()
            if expected_strata[stratum] > 0
        )
        population_count = population_by_group[(app_id, country)]
        estimate_available = bool(
            population_count
            and represented_population_count == population_count
        )
        weighted_denominator = sum(
            weights[item["review_key"]] for item in items
        )
        dataset = metadata.get((app_id, country), {})
        groups.append({
            "app_id": app_id,
            "country": country,
            "source_normalized_count": source_by_group[(app_id, country)],
            "source_valid_text_count": valid_text_by_group[(app_id, country)],
            "analysis_population_count": population_count,
            "expected_annotation_count": raw_denominator,
            "annotation_completed_count": len(items),
            "sampling_fraction": (
                round(raw_denominator / float(population_count), 6)
                if population_count else None
            ),
            "represented_population_count": represented_population_count,
            "population_estimate_available": estimate_available,
            "crawl_status": dataset.get("status"),
            "truncated": dataset.get("truncated"),
            "coverage": dataset.get("coverage"),
            "stop_reason": dataset.get("stop_reason"),
            "themes": _label_summary(
                items, review_index, "theme_ids", theme_ids, raw_denominator,
                weights, weighted_denominator, estimate_available, mode,
            ),
            "jtbds": _label_summary(
                items, review_index, "jtbd_ids", jtbd_ids, raw_denominator,
                weights, weighted_denominator, estimate_available, mode,
            ),
        })

    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "annotations_path": os.path.abspath(annotations_path),
        "population_mode": mode,
        "source_normalized_count": len(source_reviews),
        "source_valid_text_count": len(_valid_text_rows(source_reviews)),
        "analysis_population_definition": (
            "单 App 为非空正文；多 App 为同 country 共同日期交集内的非空正文。"
        ),
        "analysis_population_count": len(reviews),
        "analysis_population_sha256": _corpus_hash(reviews),
        "expected_annotation_count": len(expected_index),
        "annotation_completed_count": len(annotations),
        "annotations_complete": True,
        "observed_label_ids": observed_label_ids,
        "sample_has_unrepresented_strata": any(
            expected_strata[stratum] == 0 for stratum in population_strata
        ),
        "denominator_definition": (
            "full 模式为 analysis_population 中的全部记录：单 App 为非空正文，多 App"
            "为同 country 共同日期交集；sample 模式仅在每个非空分层均有样本时输出"
            "按 N/n 加权的该分析总体估计，否则只输出样本率。"
        ),
        "comparison_windows": comparison_windows,
        "groups": groups,
    }
    output_path = os.path.join(os.path.abspath(run_dir), "analysis", "aggregation.json")
    atomic_write_json(output_path, result)
    return result, output_path


def build_parser():
    parser = argparse.ArgumentParser(description="准备和聚合 App Store 评论分析数据")
    subparsers = parser.add_subparsers(dest="command", required=True)
    profile = subparsers.add_parser("profile", help="对全部规范化评论做确定性统计")
    profile.add_argument("--run-dir", required=True)
    sample = subparsers.add_parser("sample", help="生成稳定的分层分析样本")
    sample.add_argument("--run-dir", required=True)
    sample.add_argument("--per-app", type=int, default=2000)
    sample.add_argument("--seed", type=int, default=42)
    aggregate = subparsers.add_parser("aggregate", help="校验并聚合主题/JTBD 标注")
    aggregate.add_argument("--run-dir", required=True)
    aggregate.add_argument("--annotations", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "profile":
            profile, path = build_profile(args.run_dir)
            overall = profile["source_statistics"]["overall"]
            population = profile["analysis_population"]
            print("Profile: %s" % path)
            print("源唯一评论=%s，源有效文字=%s，分析总体=%s，分析字符=%s，大数据=%s" % (
                overall["review_count"], overall["valid_text_count"],
                population["review_count"], population["text_character_count"],
                str(profile["large_dataset"]).lower()))
            incomparable = [
                item for item in population["comparison_windows"]
                if item["app_count"] >= 2
                and not item["comparable_for_cross_app_time_window"]
            ]
            if incomparable:
                print("警告：%s 个地区没有可比较的共同时间窗口。" % len(incomparable), file=sys.stderr)
        elif args.command == "sample":
            _, metadata, path = build_sample(args.run_dir, args.per_app, args.seed)
            print("Sample: %s (%s 条)" % (path, metadata["sampled_review_count"]))
            unrepresented = sum(
                item["stratum_count"] - item["represented_stratum_count"]
                for item in metadata["apps"]
            )
            if unrepresented:
                print(
                    "警告：%s 个非空分层未抽中；聚合不会输出这些组的总体提及率。"
                    % unrepresented,
                    file=sys.stderr,
                )
        else:
            result, path = aggregate_annotations(args.run_dir, args.annotations)
            print("Aggregation: %s (%s 条标注)" % (
                path, result["annotation_completed_count"]
            ))
            if result["sample_has_unrepresented_strata"]:
                print(
                    "警告：存在未覆盖分层；相应组仅可报告样本率。",
                    file=sys.stderr,
                )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
