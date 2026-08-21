import csv
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "scrape.py")
SPEC = importlib.util.spec_from_file_location("appstore_scrape", SCRIPT)
scrape = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scrape)


def storefront_document(*rows):
    return {
        "metadata": {"source": "test", "updated_at": "2026-01-01", "count": len(rows)},
        "storefronts": list(rows),
    }


US = {"country": "us", "alpha3": "USA", "name": "United States", "default_language": "en-US"}
CA = {"country": "ca", "alpha3": "CAN", "name": "Canada", "default_language": "en-CA"}


def review(review_id, body="Good", date="2026-08-01T00:00:00Z", title="Title", rating=5):
    return {
        "id": str(review_id),
        "type": "customerReviews",
        "attributes": {
            "rating": rating,
            "title": title,
            "review": body,
            "userName": "tester",
            "date": date,
            "isEdited": False,
            "developerResponse": {"body": "Thanks", "modified": "2026-08-02T00:00:00Z"},
        },
    }


class SequenceClient(object):
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def get_json(self, url, headers=None, reviews_eof=False):
        self.calls.append((url, headers, reviews_eof))
        if not self.values:
            raise AssertionError("unexpected request: %s" % url)
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class LookupClient(object):
    def __init__(self):
        self.calls = []

    def get_json(self, url, headers=None, reviews_eof=False):
        self.calls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        country = query["country"][0]
        ids = query["id"][0].split(",")
        self.asserted_batch = ids
        results = [
            {
                "trackId": int(ids[0]),
                "kind": "software",
                "trackName": "Target",
                "sellerName": "Target Inc",
                "userRatingCount": 100 if country == "us" else 10,
                "averageUserRating": 4.5,
            }
        ]
        if country == "us" and len(ids) > 1:
            results.append(
                {
                    "trackId": int(ids[1]),
                    "kind": "software",
                    "trackName": "Competitor",
                    "artistName": "Competitor Inc",
                    "userRatingCount": 50,
                    "averageUserRating": 4.0,
                }
            )
        return {"resultCount": len(results), "results": results}


class PagedClient(object):
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def get_json(self, url, headers=None, reviews_eof=False):
        self.calls.append((url, headers, reviews_eof))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        offset = int(query["offset"][0])
        limit = int(query["limit"][0])
        return {"data": self.rows[offset:offset + limit]}


class ParseAndStorefrontTests(unittest.TestCase):
    def test_parse_numeric_id_and_product_urls(self):
        self.assertEqual(scrape.parse_app_id("1041517543"), "1041517543")
        self.assertEqual(
            scrape.parse_app_id("https://apps.apple.com/us/app/example/id1041517543?mt=8"),
            "1041517543",
        )
        self.assertEqual(
            scrape.parse_app_id("https://apps.apple.com/app?foo=1&id=1041517543"),
            "1041517543",
        )

    def test_parse_rejects_names_bundle_ids_and_bad_urls(self):
        for value in ("Fitbod", "com.example.app", "0", "https://example.com/app"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scrape.parse_app_id(value)

    def test_static_storefront_catalog_has_exactly_175_and_kosovo(self):
        document = scrape.load_storefronts()
        self.assertEqual(len(document["storefronts"]), 175)
        self.assertEqual(document["metadata"]["count"], 175)
        self.assertTrue(document["metadata"]["source"].startswith("https://developer.apple.com/"))
        self.assertTrue(document["metadata"]["updated_at"])
        kosovo = [row for row in document["storefronts"] if row["alpha3"] == "XKS"]
        self.assertEqual(kosovo[0]["country"], "xk")

    def test_country_selection_is_validated_and_deduplicated(self):
        rows = scrape.select_storefronts("US,ca,us", [US, CA])
        self.assertEqual([row["country"] for row in rows], ["us", "ca"])
        with self.assertRaises(ValueError):
            scrape.select_storefronts("zz", [US, CA])


class RegionsTests(unittest.TestCase):
    def test_batches_apps_per_country_records_statuses_and_uses_cache(self):
        with tempfile.TemporaryDirectory() as output_dir:
            client = LookupClient()
            result = scrape.run_regions(
                "111",
                ["https://apps.apple.com/app/comp/id222"],
                output_dir,
                countries="us,ca",
                client=client,
                storefront_document=storefront_document(US, CA),
                lookup_interval=0,
            )
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(client.asserted_batch, ["111", "222"])
            statuses = {
                (row["country"], row["app_id"]): row["status"]
                for row in result["storefronts"]
            }
            self.assertEqual(statuses[("us", "111")], "available")
            self.assertEqual(statuses[("us", "222")], "available")
            self.assertEqual(statuses[("ca", "222")], "unavailable")
            missing = next(
                row for row in result["storefronts"]
                if row["country"] == "ca" and row["app_id"] == "222"
            )
            self.assertIsNone(missing["rating_count"])
            self.assertEqual(result["top_common_markets"][0]["country"], "us")
            self.assertIn("仅打星", result["metric_definitions"]["rating_count"])
            us_cache_path = scrape._lookup_cache_path(
                os.path.join(output_dir, ".cache"), "us", ["111", "222"])
            us_cache = scrape.read_json(us_cache_path)
            self.assertEqual(us_cache["country"], "us")
            self.assertEqual(us_cache["app_ids"], ["111", "222"])
            self.assertIn("fetched_at", us_cache)
            self.assertIn("payload", us_cache)
            first_us = next(row for row in result["storefronts"] if row["country"] == "us")
            self.assertEqual(first_us["fetched_at"], us_cache["fetched_at"])
            self.assertEqual(first_us["lookup_source"], "network")

            no_network = SequenceClient([])
            cached_result = scrape.run_regions(
                "111", ["222"], output_dir, countries="us,ca", client=no_network,
                storefront_document=storefront_document(US, CA), lookup_interval=0,
            )
            self.assertEqual(no_network.calls, [])
            cached_us = next(
                row for row in cached_result["storefronts"] if row["country"] == "us"
            )
            self.assertEqual(cached_us["fetched_at"], us_cache["fetched_at"])
            self.assertEqual(cached_us["lookup_source"], "cache")
            manifest = scrape.read_json(os.path.join(output_dir, "manifest.json"))
            self.assertEqual([row["role"] for row in manifest["apps"]], ["target", "competitor"])

    def test_stale_lookup_cache_is_not_stamped_as_current(self):
        with tempfile.TemporaryDirectory() as output_dir:
            cache_path = scrape._lookup_cache_path(
                os.path.join(output_dir, ".cache"), "us", ["111"])
            stale_payload = {
                "results": [{
                    "trackId": 111, "kind": "software", "trackName": "Stale",
                    "userRatingCount": 1, "averageUserRating": 1.0,
                }]
            }
            scrape.atomic_write_json(cache_path, {
                "schema_version": 1, "country": "us", "app_ids": ["111"],
                "fetched_at": "2000-01-01T00:00:00Z", "payload": stale_payload,
            })
            client = LookupClient()
            result = scrape.run_regions(
                "111", [], output_dir, countries="us", client=client,
                storefront_document=storefront_document(US), lookup_interval=0,
            )
            self.assertEqual(len(client.calls), 1)
            row = result["storefronts"][0]
            self.assertEqual(row["rating_count"], 100)
            self.assertEqual(row["lookup_source"], "network")
            self.assertNotEqual(row["fetched_at"], "2000-01-01T00:00:00Z")

        with tempfile.TemporaryDirectory() as output_dir:
            cache_path = scrape._lookup_cache_path(
                os.path.join(output_dir, ".cache"), "us", ["111"])
            scrape.atomic_write_json(cache_path, stale_payload)  # Legacy cache had no timestamp.
            client = LookupClient()
            result = scrape.run_regions(
                "111", [], output_dir, countries="us", client=client,
                storefront_document=storefront_document(US), lookup_interval=0,
            )
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(result["storefronts"][0]["lookup_source"], "network")

    def test_new_region_run_drops_review_summaries_for_removed_apps(self):
        with tempfile.TemporaryDirectory() as output_dir:
            scrape.atomic_write_json(os.path.join(output_dir, "manifest.json"), {
                "created_at": "2026-01-01T00:00:00Z",
                "reviews": [
                    {"app_id": "111", "country": "us", "status": "complete"},
                    {"app_id": "999", "country": "us", "status": "complete"},
                ],
            })
            scrape.run_regions(
                "111", [], output_dir, countries="us", client=LookupClient(),
                storefront_document=storefront_document(US), lookup_interval=0,
            )
            manifest = scrape.read_json(os.path.join(output_dir, "manifest.json"))
            self.assertEqual(
                [(row["app_id"], row["country"]) for row in manifest["reviews"]],
                [("111", "us")],
            )

    def test_lookup_failure_is_error_not_zero(self):
        with tempfile.TemporaryDirectory() as output_dir:
            result = scrape.run_regions(
                "111", [], output_dir, countries="us",
                client=SequenceClient([scrape.FetchError("offline")]),
                storefront_document=storefront_document(US), lookup_interval=0,
            )
            row = result["storefronts"][0]
            self.assertEqual(row["status"], "error")
            self.assertIsNone(row["rating_count"])
            self.assertIn("offline", row["error"])

    def test_non_software_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as output_dir:
            payload = {"results": [{"trackId": 111, "kind": "song", "userRatingCount": 99}]}
            result = scrape.run_regions(
                "111", [], output_dir, countries="us", client=SequenceClient([payload]),
                storefront_document=storefront_document(US), lookup_interval=0,
            )
            self.assertEqual(result["storefronts"][0]["status"], "invalid_kind")
            self.assertIsNone(result["storefronts"][0]["rating_count"])


class CliInteractionTests(unittest.TestCase):
    def test_region_prompt_respects_user_selected_countries(self):
        result = {
            "apps": [{
                "app_id": "111", "validated": True,
                "name": "Target", "developer": "Target Inc",
            }],
            "top_common_markets": [{
                "country": "us", "country_name": "United States",
                "combined_rating_count": 100,
            }],
        }
        with mock.patch.object(scrape, "run_regions", return_value=result):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(scrape.main([
                    "regions", "--target", "111", "--output-dir", "/tmp/run",
                    "--countries", "us,cn",
                ]), 0)
            direct_text = output.getvalue()
            self.assertIn("抓取地区已由用户指定", direct_text)
            self.assertNotIn("请确认要抓取正文", direct_text)

            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(scrape.main([
                    "regions", "--target", "111", "--output-dir", "/tmp/run",
                    "--countries", "all",
                ]), 0)
            discovery_text = output.getvalue()
            self.assertIn("请确认要抓取正文", discovery_text)


class HttpTests(unittest.TestCase):
    def test_429_honors_retry_after_then_succeeds(self):
        error = urllib.error.HTTPError(
            "https://example.test", 429, "limited", {"Retry-After": "0"}, io.BytesIO(b"{}")
        )
        response = io.BytesIO(b'{"ok": true}')
        sleeps = []
        client = scrape.HttpClient(retries=2, sleep_func=sleeps.append, random_func=lambda: 0)
        with mock.patch.object(scrape.urllib.request, "urlopen", side_effect=[error, response]):
            self.assertEqual(client.get_json("https://example.test"), {"ok": True})
        self.assertEqual(sleeps, [0.0])

    def test_40403_is_normal_reviews_eof(self):
        body = json.dumps({"errors": [{"status": "40403", "title": "end"}]}).encode()
        error = urllib.error.HTTPError(
            "https://example.test", 404, "not found", {}, io.BytesIO(body)
        )
        client = scrape.HttpClient(retries=1)
        with mock.patch.object(scrape.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(scrape.ReviewsEOF):
                client.get_json("https://example.test", reviews_eof=True)


class ReviewTests(unittest.TestCase):
    def test_short_page_exports_normalized_json_csv_and_dataset(self):
        with tempfile.TemporaryDirectory() as run_dir:
            client = SequenceClient([
                {"data": [review("1", body="=SUM(A1:A2)"), review("2", body="很好")]}]
            )
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=client,
                limit=3, delay=0, head_rescan=False,
            )
            self.assertEqual(dataset["status"], "complete")
            self.assertEqual(dataset["stop_reason"], "short_page")
            self.assertEqual(dataset["retrieved_text_review_count"], 2)
            self.assertEqual(dataset["text_character_count"], len("=SUM(A1:A2)") + len("很好"))
            self.assertEqual(dataset["earliest_date"], "2026-08-01T00:00:00Z")
            self.assertEqual(dataset["latest_date"], "2026-08-01T00:00:00Z")
            self.assertEqual(dataset["developer_response_count"], 2)
            review_dir = scrape._review_dir(run_dir, "111", "us")
            rows = scrape.read_json(os.path.join(review_dir, "reviews.json"))
            self.assertEqual(rows[0]["app_id"], "111")
            self.assertEqual(rows[0]["country"], "us")
            self.assertEqual(rows[0]["developer_response_body"], "Thanks")
            with open(os.path.join(review_dir, "reviews.csv"), encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            formula_row = next(row for row in csv_rows if "SUM" in row["review"])
            self.assertTrue(formula_row["review"].startswith("'="))
            self.assertEqual(next(row for row in rows if row["review_id"] == "1")["review"], "=SUM(A1:A2)")

    def test_empty_page_and_40403_are_complete(self):
        for value, reason in [({"data": []}, "empty_page"), (scrape.ReviewsEOF(), "apple_40403_eof")]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as run_dir:
                dataset = scrape.scrape_review_dataset(
                    run_dir, "111", "us", "en-US", client=SequenceClient([value]),
                    delay=0, head_rescan=False,
                )
                self.assertEqual(dataset["status"], "complete")
                self.assertEqual(dataset["stop_reason"], reason)

    def test_duplicate_page_stops_without_looping(self):
        page = [review("1"), review("2")]
        client = SequenceClient([{"data": page}, {"data": page}])
        with tempfile.TemporaryDirectory() as run_dir:
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=client,
                limit=2, delay=0, head_rescan=False,
            )
            self.assertEqual(dataset["stop_reason"], "duplicate_page")
            self.assertTrue(dataset["truncated"])
            self.assertEqual(dataset["normalized_review_count"], 2)
            self.assertEqual(len(client.calls), 2)

    def test_resume_revalidates_from_head_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as run_dir:
            review_dir = scrape._review_dir(run_dir, "111", "us")
            os.makedirs(review_dir)
            scrape.atomic_write_json(os.path.join(review_dir, "checkpoint.json"), {"next_offset": 60})
            seen = set()
            scrape._append_envelopes(
                os.path.join(review_dir, "reviews.raw.jsonl"), [review("old")],
                "111", "us", "apps.apple.com-web", seen,
            )
            client = SequenceClient([{"data": [review("old"), review("new")]}])
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=client, resume=True,
                overlap_pages=2, limit=20, delay=0, head_rescan=False,
            )
            query = urllib.parse.parse_qs(urllib.parse.urlparse(client.calls[0][0]).query)
            self.assertEqual(query["offset"], ["0"])
            self.assertEqual(dataset["normalized_review_count"], 2)

    def test_resume_does_not_miss_more_than_one_page_of_new_head_reviews(self):
        with tempfile.TemporaryDirectory() as run_dir:
            review_dir = scrape._review_dir(run_dir, "111", "us")
            os.makedirs(review_dir)
            old = [review("old-%03d" % index) for index in range(100)]
            scrape._append_envelopes(
                os.path.join(review_dir, "reviews.raw.jsonl"), old,
                "111", "us", "apps.apple.com-web", {},
            )
            scrape.atomic_write_json(
                os.path.join(review_dir, "checkpoint.json"), {"next_offset": 100})
            new = [review("new-%03d" % index) for index in range(60)]
            client = PagedClient(new + old)
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=client, resume=True,
                limit=20, delay=0, head_rescan=True,
            )
            rows = scrape.read_json(os.path.join(review_dir, "reviews.json"))
            ids = {row["review_id"] for row in rows}
            self.assertEqual(dataset["normalized_review_count"], 160)
            self.assertTrue({"new-%03d" % index for index in range(60)} <= ids)
            first_query = urllib.parse.parse_qs(
                urllib.parse.urlparse(client.calls[0][0]).query)
            self.assertEqual(first_query["offset"], ["0"])
            checkpoint = scrape.read_json(os.path.join(review_dir, "checkpoint.json"))
            self.assertEqual(checkpoint["resume_from_offset"], 100)

    def test_head_rescan_limit_is_reported_as_partial_and_truncated(self):
        page_one = [review("new-a-%02d" % index) for index in range(20)]
        page_two = [review("new-b-%02d" % index) for index in range(20)]
        with tempfile.TemporaryDirectory() as run_dir:
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=SequenceClient([
                    {"data": []}, {"data": page_one}, {"data": page_two},
                ]), limit=20, delay=0, head_rescan=True,
                head_rescan_max_pages=2,
            )
            self.assertEqual(dataset["status"], "partial")
            self.assertEqual(dataset["stop_reason"], "head_rescan_limit")
            self.assertTrue(dataset["truncated"])
            self.assertEqual(dataset["normalized_review_count"], 40)

    def test_since_filters_old_reviews_and_stops(self):
        with tempfile.TemporaryDirectory() as run_dir:
            client = SequenceClient([{"data": [
                review("new", date="2026-08-01T00:00:00Z"),
                review("old", date="2025-01-01T00:00:00Z"),
            ]}])
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=client,
                since="2026-01-01", limit=2, delay=0, head_rescan=False,
            )
            self.assertEqual(dataset["stop_reason"], "since_reached")
            self.assertEqual(dataset["normalized_review_count"], 1)

    def test_since_is_applied_to_existing_and_rss_records_at_final_export(self):
        with tempfile.TemporaryDirectory() as run_dir:
            review_dir = scrape._review_dir(run_dir, "111", "us")
            os.makedirs(review_dir)
            scrape._append_envelopes(
                os.path.join(review_dir, "reviews.raw.jsonl"), [
                    review("existing-new", date="2026-08-01T00:00:00Z"),
                    review("existing-old", date="2025-01-01T00:00:00Z"),
                ], "111", "us", "apps.apple.com-web", {},
            )
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US",
                client=SequenceClient([{"data": []}]), since="2026-01-01",
                delay=0, head_rescan=False,
            )
            rows = scrape.read_json(os.path.join(review_dir, "reviews.json"))
            self.assertEqual([row["review_id"] for row in rows], ["existing-new"])
            self.assertEqual(dataset["requested_since"], "2026-01-01")

        rss_entries = []
        for review_id, date in [
                ("rss-new", "2026-08-01T00:00:00Z"),
                ("rss-old", "2025-01-01T00:00:00Z")]:
            rss_entries.append({
                "id": {"label": review_id}, "im:rating": {"label": "4"},
                "title": {"label": "RSS title"}, "content": {"label": "RSS body"},
                "author": {"name": {"label": "RSS user"}},
                "updated": {"label": date},
            })
        with tempfile.TemporaryDirectory() as run_dir:
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=SequenceClient([
                    scrape.FetchError("private endpoint unavailable"),
                    {"feed": {"entry": rss_entries}},
                ]), since="2026-01-01", rss_fallback=True,
                delay=0, head_rescan=False,
            )
            rows = scrape.read_json(os.path.join(
                scrape._review_dir(run_dir, "111", "us"), "reviews.json"))
            self.assertEqual([row["review_id"] for row in rows], ["rss-new"])
            self.assertEqual(dataset["normalized_review_count"], 1)

    def test_newer_observation_replaces_edited_review_and_developer_response(self):
        with tempfile.TemporaryDirectory() as run_dir:
            review_dir = scrape._review_dir(run_dir, "111", "us")
            os.makedirs(review_dir)
            raw_path = os.path.join(review_dir, "reviews.raw.jsonl")
            original = review("1", body="old body")
            scrape._append_envelopes(
                raw_path, [original], "111", "us", "apps.apple.com-web", {},
                fetched_at="2026-08-01T00:00:00Z",
            )
            edited = review("1", body="edited body")
            edited["attributes"]["isEdited"] = True
            edited["attributes"]["developerResponse"] = {
                "body": "Updated response", "modified": "2026-08-03T00:00:00Z",
            }
            scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US",
                client=SequenceClient([{"data": [edited]}]),
                limit=2, delay=0, head_rescan=False,
            )
            rows = scrape.read_json(os.path.join(review_dir, "reviews.json"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["review"], "edited body")
            self.assertTrue(rows[0]["is_edited"])
            self.assertEqual(rows[0]["developer_response_body"], "Updated response")

    def test_corrupt_jsonl_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as run_dir:
            review_dir = scrape._review_dir(run_dir, "111", "us")
            os.makedirs(review_dir)
            with open(os.path.join(review_dir, "reviews.raw.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("not json\n")
                handle.write(json.dumps(review("1")) + "\n")
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=SequenceClient([{"data": []}]),
                delay=0, head_rescan=False,
            )
            self.assertEqual(dataset["corrupt_jsonl_lines"], 1)
            self.assertEqual(dataset["normalized_review_count"], 1)

    def test_opt_in_rss_fallback_is_marked_truncated(self):
        rss_entry = {
            "id": {"label": "rss-1"}, "im:rating": {"label": "4"},
            "title": {"label": "RSS title"}, "content": {"label": "RSS body"},
            "author": {"name": {"label": "RSS user"}},
            "updated": {"label": "2026-08-01T00:00:00Z"},
        }
        client = SequenceClient([
            scrape.FetchError("private endpoint unavailable"),
            {"feed": {"entry": [rss_entry]}},
        ])
        with tempfile.TemporaryDirectory() as run_dir:
            dataset = scrape.scrape_review_dataset(
                run_dir, "111", "us", "en-US", client=client,
                rss_fallback=True, delay=0, head_rescan=False,
            )
            self.assertEqual(dataset["stop_reason"], "rss_fallback")
            self.assertTrue(dataset["truncated"])
            rows = scrape.read_json(os.path.join(scrape._review_dir(run_dir, "111", "us"), "reviews.json"))
            self.assertEqual(rows[0]["source"], "itunes-rss")
            self.assertEqual(rows[0]["rating"], 4)

    def test_run_reviews_isolates_each_app_country_checkpoint(self):
        with tempfile.TemporaryDirectory() as run_dir:
            scrape.atomic_write_json(os.path.join(run_dir, "manifest.json"), {
                "apps": [{"app_id": "111"}, {"app_id": "222"}], "reviews": []
            })
            client = SequenceClient([{"data": []}, {"data": []}, {"data": []}, {"data": []}])
            datasets = scrape.run_reviews(
                run_dir, "us,ca", client=client,
                storefront_document=storefront_document(US, CA),
                delay=0, head_rescan=False,
            )
            self.assertEqual(len(datasets), 4)
            for app_id in ("111", "222"):
                for country in ("us", "ca"):
                    path = os.path.join(scrape._review_dir(run_dir, app_id, country), "checkpoint.json")
                    self.assertTrue(os.path.exists(path), path)

    def test_unavailable_storefront_is_skipped_and_manifest_history_is_merged(self):
        with tempfile.TemporaryDirectory() as run_dir:
            scrape.atomic_write_json(os.path.join(run_dir, "manifest.json"), {
                "apps": [{"app_id": "111"}],
                "reviews": [{"app_id": "111", "country": "ca", "status": "complete"}],
            })
            scrape.atomic_write_json(os.path.join(run_dir, "storefront_ratings.json"), {
                "storefronts": [
                    {"app_id": "111", "country": "us", "status": "unavailable"}
                ]
            })
            client = SequenceClient([])
            datasets = scrape.run_reviews(
                run_dir, "us", client=client,
                storefront_document=storefront_document(US), delay=0, head_rescan=False,
            )
            self.assertEqual(client.calls, [])
            self.assertEqual(datasets[0]["status"], "skipped")
            self.assertEqual(datasets[0]["stop_reason"], "unavailable_in_storefront")
            manifest = scrape.read_json(os.path.join(run_dir, "manifest.json"))
            self.assertEqual(
                {(row["app_id"], row["country"]) for row in manifest["reviews"]},
                {("111", "ca"), ("111", "us")},
            )

    def test_lookup_error_is_retried_but_invalid_kind_stays_distinct(self):
        for lookup_status, expected_calls, expected_status, expected_reason in [
                ("error", 1, "complete", "empty_page"),
                ("invalid_kind", 0, "skipped", "invalid_kind")]:
            with self.subTest(lookup_status=lookup_status), tempfile.TemporaryDirectory() as run_dir:
                scrape.atomic_write_json(os.path.join(run_dir, "manifest.json"), {
                    "apps": [{"app_id": "111"}], "reviews": [],
                })
                scrape.atomic_write_json(os.path.join(run_dir, "storefront_ratings.json"), {
                    "storefronts": [{
                        "app_id": "111", "country": "us", "status": lookup_status,
                    }],
                })
                client = SequenceClient([{"data": []}] if expected_calls else [])
                datasets = scrape.run_reviews(
                    run_dir, "us", client=client,
                    storefront_document=storefront_document(US),
                    delay=0, head_rescan=False,
                )
                self.assertEqual(len(client.calls), expected_calls)
                self.assertEqual(datasets[0]["status"], expected_status)
                self.assertEqual(datasets[0]["stop_reason"], expected_reason)
                checkpoint = scrape.read_json(os.path.join(
                    scrape._review_dir(run_dir, "111", "us"), "checkpoint.json"))
                if lookup_status == "invalid_kind":
                    self.assertEqual(checkpoint["lookup_status"], "invalid_kind")


if __name__ == "__main__":
    unittest.main()
