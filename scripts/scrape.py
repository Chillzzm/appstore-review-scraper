#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect App Store rating distribution and public review data.

Only Python's standard library is required. The iTunes Lookup API is public,
but the apps.apple.com reviews endpoint is an undocumented, best-effort web
endpoint. ``userRatingCount`` is never a text-review count.
"""

import argparse
import csv
import datetime as dt
import email.utils
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SCHEMA_VERSION = 1
DEFAULT_LIMIT = 20
DEFAULT_LOOKUP_INTERVAL = 3.2
DEFAULT_LOOKUP_CACHE_TTL = 24 * 60 * 60
DEFAULT_HEAD_RESCAN_MAX_PAGES = 100
RETRYABLE_HTTP_CODES = (429, 500, 502, 503, 504)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
FORMULA_PREFIXES = ("=", "+", "-", "@")


class FetchError(RuntimeError):
    """A request failed after retrying."""

    def __init__(self, message, status=None, payload=None):
        RuntimeError.__init__(self, message)
        self.status = status
        self.payload = payload


class ReviewsEOF(Exception):
    """Apple's private reviews endpoint reported its normal 40403 end marker."""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_app_id(value):
    """Return a numeric Apple ID from either an ID or an App Store URL."""
    value = str(value or "").strip()
    if re.fullmatch(r"[1-9][0-9]*", value):
        return value
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        parsed = None
    if (parsed and parsed.scheme in ("http", "https") and
            parsed.hostname in ("apps.apple.com", "itunes.apple.com")):
        match = re.search(r"(?:^|/)id([1-9][0-9]*)(?:$|[/?])", parsed.path + "/")
        if not match:
            match = re.search(r"(?:^|[?&])id=([1-9][0-9]*)(?:&|$)", "?" + parsed.query)
        if match:
            return match.group(1)
    raise ValueError("App 必须是数字 Apple ID 或包含 /id<数字> 的 App Store 产品页链接")


def load_storefronts(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "storefronts.json")
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    rows = document.get("storefronts") if isinstance(document, dict) else None
    if not isinstance(rows, list) or len(rows) != 175:
        raise ValueError("storefronts.json 必须恰好包含 175 个 storefront")
    metadata = document.get("metadata") or {}
    if metadata.get("count") != 175 or not metadata.get("source") or not metadata.get("updated_at"):
        raise ValueError("storefronts.json metadata 缺少有效 source/updated_at/count")
    required = ("country", "alpha3", "name", "default_language")
    for row in rows:
        if any(not row.get(key) for key in required):
            raise ValueError("storefront 条目缺少必要字段: %r" % row)
        row["country"] = row["country"].lower()
        row["alpha3"] = row["alpha3"].upper()
    if len({row["country"] for row in rows}) != len(rows):
        raise ValueError("storefront country 代码重复")
    kosovo = [row for row in rows if row["country"] == "xk" and row["alpha3"] == "XKS"]
    if len(kosovo) != 1:
        raise ValueError("storefronts.json 必须包含 xk/XKS Kosovo")
    return document


def select_storefronts(spec, storefronts):
    spec = (spec or "all").strip().lower()
    by_country = {row["country"]: row for row in storefronts}
    if spec == "all":
        return list(storefronts)
    codes = []
    for raw in spec.split(","):
        code = raw.strip().lower()
        if not code:
            continue
        if code not in by_country:
            raise ValueError("未知 App Store 国家/地区代码: %s" % code)
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError("至少需要一个国家/地区代码")
    return [by_country[code] for code in codes]


def atomic_write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = "%s.tmp.%s" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def csv_safe(value):
    """Neutralize spreadsheet formulas while leaving JSON values untouched."""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row.get(key)) for key in fieldnames})


def _decode_error_body(error):
    try:
        body = error.read()
    except Exception:
        return None
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return {"body": body.decode("utf-8", errors="replace")[:1000]}


def _contains_40403(payload):
    if payload is None:
        return False
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in ("code", "status") and str(value) == "40403":
                return True
            if _contains_40403(value):
                return True
    elif isinstance(payload, list):
        return any(_contains_40403(value) for value in payload)
    return False


def _retry_after_seconds(headers, now=None):
    if not headers:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            target = email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=dt.timezone.utc)
            current = now or dt.datetime.now(dt.timezone.utc)
            return max(0.0, (target - current).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class HttpClient(object):
    def __init__(self, retries=5, timeout=30, sleep_func=time.sleep, random_func=random.random):
        self.retries = retries
        self.timeout = timeout
        self.sleep = sleep_func
        self.random = random_func

    def get_json(self, url, headers=None, reviews_eof=False):
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        last_error = None
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                payload = _decode_error_body(error)
                if reviews_eof and error.code == 404 and _contains_40403(payload):
                    raise ReviewsEOF()
                last_error = FetchError(
                    "HTTP %s: %s" % (error.code, url), status=error.code, payload=payload
                )
                if error.code not in RETRYABLE_HTTP_CODES or attempt + 1 >= self.retries:
                    raise last_error
                wait = _retry_after_seconds(error.headers)
                if wait is None:
                    wait = min(60.0, 2.0 * (2 ** attempt)) + self.random()
                sys.stderr.write("[HTTP %s] %.1fs 后重试\n" % (error.code, wait))
                self.sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                last_error = FetchError("请求失败: %s (%s)" % (url, error))
                if attempt + 1 >= self.retries:
                    raise last_error
                wait = min(60.0, 2.0 * (2 ** attempt)) + self.random()
                self.sleep(wait)
        raise last_error or FetchError("请求失败: %s" % url)


def _lookup_cache_path(cache_dir, country, app_ids):
    digest = hashlib.sha256(",".join(sorted(app_ids)).encode("ascii")).hexdigest()[:16]
    return os.path.join(cache_dir, "lookup", "%s-%s.json" % (country, digest))


def _parse_timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _fresh_lookup_cache(value, country, app_ids, cache_ttl):
    if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
        return None
    payload = value["payload"]
    if not isinstance(payload.get("results"), list):
        return None
    if value.get("country") != country or value.get("app_ids") != sorted(app_ids):
        return None
    fetched_at = _parse_timestamp(value.get("fetched_at"))
    if fetched_at is None:
        return None
    age = (dt.datetime.now(dt.timezone.utc) - fetched_at).total_seconds()
    if age < 0 or age > cache_ttl:
        return None
    return payload, value["fetched_at"]


def lookup_apps(country, app_ids, client, cache_dir=None, refresh=False,
                cache_ttl=DEFAULT_LOOKUP_CACHE_TTL, return_metadata=False):
    cache_path = _lookup_cache_path(cache_dir, country, app_ids) if cache_dir else None
    if cache_path and not refresh:
        cached = read_json(cache_path)
        fresh = _fresh_lookup_cache(cached, country, app_ids, cache_ttl)
        if fresh:
            payload, fetched_at = fresh
            if return_metadata:
                return payload, fetched_at, True
            return payload
    query = urllib.parse.urlencode({"id": ",".join(app_ids), "country": country})
    payload = client.get_json("https://itunes.apple.com/lookup?" + query)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise FetchError("Lookup API 返回了无效结构")
    fetched_at = utc_now()
    if cache_path:
        atomic_write_json(cache_path, {
            "schema_version": SCHEMA_VERSION,
            "country": country,
            "app_ids": sorted(app_ids),
            "fetched_at": fetched_at,
            "payload": payload,
        })
    if return_metadata:
        return payload, fetched_at, False
    return payload


def _unique_app_specs(target, competitors):
    specs = []
    seen = set()
    for role, value in [("target", target)] + [("competitor", item) for item in competitors or []]:
        app_id = parse_app_id(value)
        if app_id in seen:
            continue
        seen.add(app_id)
        specs.append({"app_id": app_id, "role": role, "input": value})
    return specs


def top_common_markets(rows, app_ids, limit=5):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["country"], []).append(row)
    ranked = []
    expected = set(app_ids)
    for country, country_rows in grouped.items():
        available = {row["app_id"] for row in country_rows if row["status"] == "available"}
        if available != expected:
            continue
        ranked.append(
            {
                "country": country,
                "country_name": country_rows[0]["country_name"],
                "combined_rating_count": sum(int(row["rating_count"] or 0) for row in country_rows),
            }
        )
    ranked.sort(key=lambda row: (-row["combined_rating_count"], row["country"]))
    return ranked[:limit]


def run_regions(target, competitors, output_dir, countries="all", client=None,
                storefront_document=None, refresh=False,
                lookup_interval=DEFAULT_LOOKUP_INTERVAL, sleep_func=time.sleep,
                lookup_cache_ttl=DEFAULT_LOOKUP_CACHE_TTL):
    client = client or HttpClient()
    storefront_document = storefront_document or load_storefronts()
    selected = select_storefronts(countries, storefront_document["storefronts"])
    specs = _unique_app_specs(target, competitors)
    app_ids = [spec["app_id"] for spec in specs]
    os.makedirs(output_dir, exist_ok=True)
    rows = []
    app_metadata = {}
    for index, storefront in enumerate(selected):
        country = storefront["country"]
        attempted_at = utc_now()
        used_network = True
        try:
            payload, fetched_at, from_cache = lookup_apps(
                country, app_ids, client,
                cache_dir=os.path.join(output_dir, ".cache"), refresh=refresh,
                cache_ttl=lookup_cache_ttl, return_metadata=True)
            used_network = not from_cache
            lookup_source = "cache" if from_cache else "network"
            results = {}
            for result in payload.get("results", []):
                result_id = str(result.get("trackId") or "")
                if result_id in app_ids:
                    results[result_id] = result
            for spec in specs:
                result = results.get(spec["app_id"])
                base = {
                    "country": country, "alpha3": storefront["alpha3"],
                    "country_name": storefront["name"],
                    "default_language": storefront["default_language"],
                    "app_id": spec["app_id"], "app_role": spec["role"],
                    "fetched_at": fetched_at, "app_name": None, "developer": None,
                    "rating_count": None, "average_rating": None, "error": None,
                    "lookup_source": lookup_source,
                }
                if result is None:
                    base["status"] = "unavailable"
                elif result.get("kind") != "software":
                    base["status"] = "invalid_kind"
                    base["error"] = "Lookup result kind=%r" % result.get("kind")
                else:
                    base.update({
                        "status": "available", "app_name": result.get("trackName"),
                        "developer": result.get("sellerName") or result.get("artistName"),
                        "rating_count": result.get("userRatingCount"),
                        "average_rating": result.get("averageUserRating"),
                    })
                    app_metadata.setdefault(spec["app_id"], {
                        "name": base["app_name"], "developer": base["developer"]
                    })
                rows.append(base)
        except FetchError as error:
            for spec in specs:
                rows.append({
                    "country": country, "alpha3": storefront["alpha3"],
                    "country_name": storefront["name"],
                    "default_language": storefront["default_language"],
                    "app_id": spec["app_id"], "app_role": spec["role"],
                    "status": "error", "app_name": None, "developer": None,
                    "rating_count": None, "average_rating": None,
                    "fetched_at": attempted_at, "error": str(error),
                    "lookup_source": "network_error",
                })
        if used_network and lookup_interval > 0 and index + 1 < len(selected):
            sleep_func(lookup_interval)

    generated_at = utc_now()
    top_markets = top_common_markets(rows, app_ids)
    manifest_apps = []
    for spec in specs:
        item = dict(spec)
        item.update(app_metadata.get(spec["app_id"], {}))
        item["validated"] = spec["app_id"] in app_metadata
        manifest_apps.append(item)
    ratings_document = {
        "schema_version": SCHEMA_VERSION, "generated_at": generated_at,
        "metric_definitions": {
            "rating_count": "Apple userRatingCount；包含仅打星、未写文字评论的用户。",
            "average_rating": "Apple averageUserRating；该 storefront 的平均星级。",
            "text_review_count": "Lookup API 不提供官方文字评论总数；不要由 rating_count 推算。",
        },
        "apps": manifest_apps, "top_common_markets": top_markets, "storefronts": rows,
    }
    atomic_write_json(os.path.join(output_dir, "storefront_ratings.json"), ratings_document)
    fields = ["country", "alpha3", "country_name", "default_language", "app_id",
              "app_role", "app_name", "developer", "status", "rating_count",
              "average_rating", "fetched_at", "lookup_source", "error"]
    write_csv(os.path.join(output_dir, "storefront_ratings.csv"), fields, rows)

    existing = read_json(os.path.join(output_dir, "manifest.json"), {}) or {}
    if not isinstance(existing, dict):
        existing = {}
    current_app_ids = {item["app_id"] for item in manifest_apps}
    existing_reviews = existing.get("reviews", [])
    if not isinstance(existing_reviews, list):
        existing_reviews = []
    retained_reviews = [
        item for item in existing_reviews
        if isinstance(item, dict) and str(item.get("app_id") or "") in current_app_ids
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": existing.get("created_at") or generated_at,
        "updated_at": generated_at, "apps": manifest_apps,
        "rating_regions": [row["country"] for row in selected],
        "reviews": retained_reviews,
        "data_notes": [
            "userRatingCount 是评分人数，包含仅打星用户，并非文字评论数。",
            "公开 Lookup API 不提供竞品的官方文字评论总数。",
            "文字评论端点是非官方、best-effort 数据源。",
        ],
        "storefront_source": storefront_document.get("metadata", {}),
    }
    atomic_write_json(os.path.join(output_dir, "manifest.json"), manifest)
    return ratings_document


def _review_dir(run_dir, app_id, country):
    return os.path.join(run_dir, "apps", str(app_id), "reviews", country)


def _review_id(raw):
    if not isinstance(raw, dict):
        return None
    value = raw.get("id")
    if isinstance(value, dict):
        value = value.get("label")
    return str(value) if value is not None else None


def _raw_from_envelope(value):
    if isinstance(value, dict) and isinstance(value.get("raw"), dict):
        return value["raw"], value.get("fetched_at"), value.get("source")
    return value, None, None


def load_raw_jsonl(path):
    records_by_id = {}
    corrupt_lines = 0
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        return [], corrupt_lines
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                raw, _, _ = _raw_from_envelope(envelope)
                review_id = _review_id(raw)
                if not review_id:
                    corrupt_lines += 1
                    continue
                # JSONL is append-only; a later line for the same review is a
                # newer observation (edited review or developer response).
                records_by_id[review_id] = envelope
            except (TypeError, ValueError):
                corrupt_lines += 1
    return list(records_by_id.values()), corrupt_lines


def _append_envelopes(path, raws, app_id, country, source, seen_records, fetched_at=None):
    fetched_at = fetched_at or utc_now()
    new_count = 0
    tracks_raws = isinstance(seen_records, dict)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for raw in raws:
            review_id = _review_id(raw)
            if not review_id:
                continue
            if tracks_raws:
                if review_id in seen_records and seen_records[review_id] == raw:
                    continue
            elif review_id in seen_records:
                continue
            envelope = {"app_id": str(app_id), "country": country,
                        "fetched_at": fetched_at, "source": source, "raw": raw}
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            if tracks_raws:
                seen_records[review_id] = raw
            else:
                seen_records.add(review_id)
            new_count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return new_count


def _rss_value(value):
    return value.get("label") if isinstance(value, dict) else value


def normalize_review(envelope, app_id, country):
    raw, fetched_at, source = _raw_from_envelope(envelope)
    if not isinstance(raw, dict):
        return None
    review_id = _review_id(raw)
    if not review_id:
        return None
    if isinstance(raw.get("attributes"), dict):
        attributes = raw["attributes"]
        response = attributes.get("developerResponse") or {}
        return {
            "app_id": str(app_id), "country": country, "review_id": review_id,
            "rating": attributes.get("rating"), "title": attributes.get("title"),
            "review": attributes.get("review"), "user_name": attributes.get("userName"),
            "date": attributes.get("date"), "is_edited": attributes.get("isEdited"),
            "developer_response_body": response.get("body"),
            "developer_response_date": response.get("modified") or response.get("date"),
            "fetched_at": fetched_at, "source": source or "apps.apple.com-web",
        }
    rating = _rss_value(raw.get("im:rating"))
    try:
        rating = int(rating) if rating is not None else None
    except (TypeError, ValueError):
        pass
    author = raw.get("author") or {}
    return {
        "app_id": str(app_id), "country": country, "review_id": review_id,
        "rating": rating, "title": _rss_value(raw.get("title")),
        "review": _rss_value(raw.get("content")),
        "user_name": _rss_value(author.get("name")),
        "date": _rss_value(raw.get("updated")), "is_edited": None,
        "developer_response_body": None, "developer_response_date": None,
        "fetched_at": fetched_at, "source": source or "itunes-rss",
    }


def _review_date_value(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _review_date(raw):
    normalized = normalize_review({"raw": raw}, "", "")
    return _review_date_value(normalized.get("date") if normalized else None)


def review_page_url(app_id, country, language, offset, limit):
    base = "https://apps.apple.com/api/apps/v1/catalog/%s/apps/%s/reviews" % (country, app_id)
    query = urllib.parse.urlencode({
        "l": language, "sort": "recent", "platform": "web",
        "additionalPlatforms": "appletv,ipad,iphone,mac",
        "offset": offset, "limit": limit,
    })
    return base + "?" + query


def fetch_review_page(client, app_id, country, language, offset, limit):
    url = review_page_url(app_id, country, language, offset, limit)
    headers = {"Referer": "https://apps.apple.com/%s/app/id%s" % (country, app_id)}
    payload = client.get_json(url, headers=headers, reviews_eof=True)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise FetchError("评论端点返回了无效结构")
    return data


def _fetch_rss_reviews(client, app_id, country, max_pages=10):
    records = []
    for page in range(1, max_pages + 1):
        url = ("https://itunes.apple.com/%s/rss/customerreviews/page=%s/id=%s/"
               "sortby=mostrecent/json" % (country, page, app_id))
        payload = client.get_json(url)
        entries = ((payload or {}).get("feed") or {}).get("entry") or []
        reviews = [entry for entry in entries if isinstance(entry, dict) and entry.get("im:rating")]
        records.extend(reviews)
        if len(reviews) < 50:
            break
    return records[:500]


def _rescan_review_head(client, app_id, country, language, raw_path,
                        latest_records, since_date, limit, max_pages,
                        delay, sleep_func):
    """Catch reviews inserted at the mutable head while pagination was running."""
    baseline_ids = set(latest_records)
    offset = 0
    for page in range(max_pages):
        try:
            data = fetch_review_page(client, app_id, country, language, offset, limit)
        except ReviewsEOF:
            return True, None, page + 1
        except FetchError as error:
            return False, "head_rescan_failed: %s" % error, page + 1
        if not data:
            return True, None, page + 1
        reached_anchor = any(_review_id(raw) in baseline_ids for raw in data)
        eligible = data
        reached_since = False
        if since_date:
            eligible = []
            for raw in data:
                review_date = _review_date(raw)
                if review_date is not None and review_date >= since_date:
                    eligible.append(raw)
                elif review_date is not None:
                    reached_since = True
        _append_envelopes(raw_path, eligible, app_id, country,
                          "apps.apple.com-web", latest_records)
        if reached_anchor or reached_since or len(data) < limit:
            return True, None, page + 1
        offset += len(data)
        if delay > 0:
            sleep_func(delay)
    return False, "head_rescan_limit", max_pages


def _export_review_dataset(review_dir, app_id, country, status, stop_reason,
                           truncated, corrupt_lines, since=None):
    raw_path = os.path.join(review_dir, "reviews.raw.jsonl")
    envelopes, new_corrupt = load_raw_jsonl(raw_path)
    corrupt_lines = max(corrupt_lines, new_corrupt)
    reviews = []
    for envelope in envelopes:
        normalized = normalize_review(envelope, app_id, country)
        if normalized:
            reviews.append(normalized)
    since_date = dt.date.fromisoformat(since) if since else None
    if since_date:
        filtered = []
        for row in reviews:
            review_date = _review_date_value(row.get("date"))
            if review_date is not None and review_date >= since_date:
                filtered.append(row)
        reviews = filtered
    reviews.sort(key=lambda row: (row.get("date") or "", row["review_id"]), reverse=True)
    atomic_write_json(os.path.join(review_dir, "reviews.json"), reviews)
    fields = ["app_id", "country", "review_id", "rating", "title", "review",
              "user_name", "date", "is_edited", "developer_response_body",
              "developer_response_date", "fetched_at", "source"]
    write_csv(os.path.join(review_dir, "reviews.csv"), fields, reviews)
    valid_text = [row for row in reviews if str(row.get("review") or "").strip()]
    dates = sorted(str(row["date"]) for row in reviews if row.get("date"))
    dataset = {
        "schema_version": SCHEMA_VERSION, "generated_at": utc_now(),
        "app_id": str(app_id), "country": country, "status": status,
        "stop_reason": stop_reason, "coverage": "best_effort",
        "requested_since": since,
        "truncated": bool(truncated),
        "retrieved_text_review_count": len(valid_text),
        "normalized_review_count": len(reviews),
        "text_character_count": sum(len(str(row.get("review") or "")) for row in valid_text),
        "earliest_date": dates[0] if dates else None,
        "latest_date": dates[-1] if dates else None,
        "developer_response_count": sum(
            1 for row in reviews if str(row.get("developer_response_body") or "").strip()
        ),
        "corrupt_jsonl_lines": corrupt_lines,
        "notes": [
            "apps.apple.com 评论端点并非公开 API，结果不承诺官方全量。",
            "retrieved_text_review_count 仅表示本次可见、已抓取且去重的文字评论数。",
            "不得与 userRatingCount 相减来推算只打星人数。",
        ],
    }
    atomic_write_json(os.path.join(review_dir, "dataset.json"), dataset)
    return dataset


def scrape_review_dataset(run_dir, app_id, country, language, client=None,
                          since=None, resume=False, max_pages=None,
                          limit=DEFAULT_LIMIT, overlap_pages=2, delay=0.6,
                          sleep_func=time.sleep, rss_fallback=False, head_rescan=True,
                          head_rescan_max_pages=DEFAULT_HEAD_RESCAN_MAX_PAGES):
    client = client or HttpClient()
    review_dir = _review_dir(run_dir, app_id, country)
    os.makedirs(review_dir, exist_ok=True)
    raw_path = os.path.join(review_dir, "reviews.raw.jsonl")
    state_path = os.path.join(review_dir, "checkpoint.json")
    existing, corrupt_lines = load_raw_jsonl(raw_path)
    latest_records = {}
    for envelope in existing:
        raw, _, _ = _raw_from_envelope(envelope)
        review_id = _review_id(raw)
        if review_id:
            latest_records[review_id] = raw
    state = read_json(state_path, {}) if resume else {}
    saved_offset = state.get("next_offset", 0) if isinstance(state, dict) else 0
    try:
        saved_offset = max(0, int(saved_offset))
    except (TypeError, ValueError):
        saved_offset = 0
    # The endpoint is a mutable recent-feed without a snapshot token. Starting
    # at an old numeric offset can skip reviews inserted at the head, so resume
    # revalidates from offset zero and relies on review IDs for deduplication.
    offset = 0
    since_date = dt.date.fromisoformat(since) if since else None
    pages = 0
    page_fingerprints = set()
    status, stop_reason, truncated, normal_completion = "partial", "unknown", False, False

    while True:
        if max_pages is not None and pages >= max_pages:
            stop_reason, truncated = "max_pages", True
            break
        try:
            data = fetch_review_page(client, app_id, country, language, offset, limit)
        except ReviewsEOF:
            status, stop_reason, normal_completion = "complete", "apple_40403_eof", True
            break
        except FetchError as error:
            if rss_fallback:
                try:
                    rss_records = _fetch_rss_reviews(client, app_id, country)
                    # RSS has fewer fields than the web response; it may add
                    # missing IDs but must not replace a richer web observation.
                    _append_envelopes(raw_path, rss_records, app_id, country,
                                      "itunes-rss", set(latest_records))
                    status, stop_reason, truncated = "partial", "rss_fallback", True
                except FetchError as rss_error:
                    status = "failed"
                    stop_reason = "web_and_rss_failed: %s" % rss_error
                    truncated = True
            else:
                status = "failed"
                stop_reason = "web_endpoint_failed: %s" % error
                truncated = True
            break
        if not data:
            status, stop_reason, normal_completion = "complete", "empty_page", True
            break
        fingerprint = tuple(_review_id(raw) for raw in data)
        if fingerprint in page_fingerprints:
            stop_reason, truncated = "duplicate_page", True
            break
        page_fingerprints.add(fingerprint)

        eligible, reached_since = data, False
        if since_date:
            eligible = []
            for raw in data:
                review_date = _review_date(raw)
                if review_date is None or review_date >= since_date:
                    eligible.append(raw)
                else:
                    reached_since = True
        _append_envelopes(raw_path, eligible, app_id, country,
                          "apps.apple.com-web", latest_records)
        pages += 1
        offset += len(data)
        atomic_write_json(state_path, {
            "schema_version": SCHEMA_VERSION, "app_id": str(app_id),
            "country": country, "next_offset": offset,
            "pages_in_last_run": pages, "updated_at": utc_now(), "status": "running",
            "resume_from_offset": saved_offset if resume else None,
        })
        if reached_since:
            status, stop_reason, normal_completion = "complete", "since_reached", True
            break
        if len(data) < limit:
            status, stop_reason, normal_completion = "complete", "short_page", True
            break
        if delay > 0:
            sleep_func(delay)

    if normal_completion and head_rescan:
        head_ok, head_reason, _ = _rescan_review_head(
            client, app_id, country, language, raw_path, latest_records,
            since_date, limit, max(1, head_rescan_max_pages), delay, sleep_func)
        if not head_ok:
            status, stop_reason, truncated = "partial", head_reason, True

    atomic_write_json(state_path, {
        "schema_version": SCHEMA_VERSION, "app_id": str(app_id), "country": country,
        "next_offset": offset, "pages_in_last_run": pages, "updated_at": utc_now(),
        "status": status, "stop_reason": stop_reason,
        "resume_from_offset": saved_offset if resume else None,
    })
    return _export_review_dataset(review_dir, app_id, country, status,
                                  stop_reason, truncated, corrupt_lines, since=since)


def run_reviews(run_dir, countries, client=None, storefront_document=None,
                since=None, resume=False, max_pages=None, limit=DEFAULT_LIMIT,
                overlap_pages=2, delay=0.6, sleep_func=time.sleep,
                rss_fallback=False, head_rescan=True):
    manifest_path = os.path.join(run_dir, "manifest.json")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or not manifest.get("apps"):
        raise ValueError("run-dir 缺少有效 manifest.json；请先运行 regions 子命令")
    storefront_document = storefront_document or load_storefronts()
    selected = select_storefronts(countries, storefront_document["storefronts"])
    ratings_document = read_json(os.path.join(run_dir, "storefront_ratings.json"), {}) or {}
    availability = {}
    for row in ratings_document.get("storefronts", []):
        availability[(str(row.get("app_id")), row.get("country"))] = row.get("status")
    datasets = []
    for app in manifest["apps"]:
        app_id = str(app.get("app_id") or "")
        if not re.fullmatch(r"[1-9][0-9]*", app_id):
            raise ValueError("manifest 含无效 app_id: %r" % app_id)
        for storefront in selected:
            country = storefront["country"]
            known_status = availability.get((app_id, country))
            if known_status in ("unavailable", "invalid_kind"):
                stop_reason = (
                    "unavailable_in_storefront"
                    if known_status == "unavailable" else "invalid_kind"
                )
                review_dir = _review_dir(run_dir, app_id, country)
                os.makedirs(review_dir, exist_ok=True)
                atomic_write_json(os.path.join(review_dir, "checkpoint.json"), {
                    "schema_version": SCHEMA_VERSION, "app_id": app_id,
                    "country": country, "next_offset": 0, "pages_in_last_run": 0,
                    "updated_at": utc_now(), "status": "skipped",
                    "stop_reason": stop_reason,
                    "lookup_status": known_status,
                })
                datasets.append(_export_review_dataset(
                    review_dir, app_id, country, "skipped", stop_reason,
                    False, 0, since=since))
                continue
            datasets.append(scrape_review_dataset(
                run_dir, app_id, country, storefront["default_language"],
                client=client, since=since, resume=resume, max_pages=max_pages,
                limit=limit, overlap_pages=overlap_pages, delay=delay,
                sleep_func=sleep_func, rss_fallback=rss_fallback,
                head_rescan=head_rescan,
            ))
    manifest["updated_at"] = utc_now()
    merged = {}
    for item in manifest.get("reviews", []):
        if isinstance(item, dict) and item.get("app_id") and item.get("country"):
            merged[(str(item["app_id"]), item["country"])] = item
    for item in datasets:
        merged[(str(item["app_id"]), item["country"])] = item
    manifest["reviews"] = [merged[key] for key in sorted(merged)]
    atomic_write_json(manifest_path, manifest)
    return datasets


def build_parser():
    parser = argparse.ArgumentParser(
        description="抓取 App Store 全地区评分分布及指定地区的公开文字评论")
    subparsers = parser.add_subparsers(dest="command", required=True)
    regions = subparsers.add_parser("regions", help="查询目标 App 和竞品的地区评分分布")
    regions.add_argument("--target", required=True, help="目标 App 产品页链接或数字 Apple ID")
    regions.add_argument("--competitor", action="append", default=[],
                         help="竞品链接或 ID；可重复传入")
    regions.add_argument("--output-dir", required=True, help="本次任务的输出目录")
    regions.add_argument("--countries", default="all", help="all 或逗号分隔 alpha-2 代码")
    regions.add_argument("--refresh", action="store_true", help="忽略已有 Lookup 缓存")
    regions.add_argument("--lookup-interval", type=float,
                         default=DEFAULT_LOOKUP_INTERVAL, help=argparse.SUPPRESS)
    reviews = subparsers.add_parser("reviews", help="抓取已确认地区的公开文字评论")
    reviews.add_argument("--run-dir", required=True, help="regions 创建的任务目录")
    reviews.add_argument("--countries", required=True, help="逗号分隔 alpha-2 代码，例如 us,cn")
    reviews.add_argument("--since", help="只保留该日期及之后的评论，格式 YYYY-MM-DD")
    reviews.add_argument(
        "--resume", action="store_true",
        help="复用已有记录并从 recent feed 头部重新核对，避免漏掉新评论")
    reviews.add_argument("--max-pages", type=int, help="每个 App/地区最多抓取页数")
    reviews.add_argument("--rss-fallback", action="store_true",
                         help="网页端点失败时降级到 RSS（最多 500 条）")
    reviews.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=argparse.SUPPRESS)
    reviews.add_argument("--overlap-pages", type=int, default=2, help=argparse.SUPPRESS)
    reviews.add_argument("--delay", type=float, default=0.6, help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "regions":
            result = run_regions(
                args.target, args.competitor, os.path.abspath(args.output_dir),
                countries=args.countries, refresh=args.refresh,
                lookup_interval=max(0.0, args.lookup_interval))
            print("地区评分分布已保存；以下数字是评分人数（含仅打星），不是文字评论数。")
            for app in result["apps"]:
                if app.get("validated"):
                    print("App: %s | %s | ID %s" % (
                        app.get("name") or "(未知名称)",
                        app.get("developer") or "(未知开发者)", app["app_id"]))
                else:
                    print("App: ID %s | 未在所选地区验证为可用" % app["app_id"])
            for market in result["top_common_markets"]:
                print("%s (%s): 合计评分人数 %s" % (
                    market["country_name"], market["country"],
                    market["combined_rating_count"]))
            if args.countries.strip().lower() == "all":
                print("请确认要抓取正文的国家/地区后，再运行 reviews 子命令。")
            else:
                print("抓取地区已由用户指定；可直接运行 reviews 子命令。")
        else:
            if args.max_pages is not None and args.max_pages <= 0:
                raise ValueError("--max-pages 必须大于 0")
            if args.limit <= 0 or args.limit > 20:
                raise ValueError("--limit 必须在 1 到 20 之间")
            if args.since:
                dt.date.fromisoformat(args.since)
            datasets = run_reviews(
                os.path.abspath(args.run_dir), args.countries, since=args.since,
                resume=args.resume, max_pages=args.max_pages, limit=args.limit,
                overlap_pages=max(0, args.overlap_pages), delay=max(0.0, args.delay),
                rss_fallback=args.rss_fallback)
            for item in datasets:
                print("%s/%s: status=%s, API可见文字评论=%s, 日期=%s..%s, truncated=%s" % (
                    item["app_id"], item["country"], item["status"],
                    item["retrieved_text_review_count"], item.get("earliest_date") or "-",
                    item.get("latest_date") or "-", str(item["truncated"]).lower()))
            total = sum(item["retrieved_text_review_count"] for item in datasets)
            chars = sum(item["text_character_count"] for item in datasets)
            print("评论数据已保存：本次共 %s 条 API 可见有效文字评论，%s 个字符。" % (total, chars))
            print("数据抓取阶段到此结束；请先询问用户是否继续竞品分析。")
    except (ValueError, FetchError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
