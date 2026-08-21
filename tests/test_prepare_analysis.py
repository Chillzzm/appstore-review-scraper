import importlib.util
import json
import os
import tempfile
import unittest


SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "prepare_analysis.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def review(app_id, country, review_id, rating, text, date, title="title", response=None):
    return {
        "app_id": str(app_id),
        "country": country,
        "review_id": str(review_id),
        "rating": rating,
        "title": title,
        "review": text,
        "date": date,
        "developer_response_body": response,
    }


class AnalysisFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def write_dataset(self, app_id, country, rows, status="complete", truncated=False):
        directory = os.path.join(
            self.run_dir, "apps", str(app_id), "reviews", country
        )
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "reviews.json"), "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False)
        with open(os.path.join(directory, "dataset.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "app_id": str(app_id),
                "country": country,
                "status": status,
                "truncated": truncated,
                "coverage": "best_effort",
            }, handle)

    def write_annotations(self, items):
        path = os.path.join(self.run_dir, "annotations.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item) + "\n")
        return path

    def write_manifest(self, app_ids, review_pairs):
        with open(os.path.join(self.run_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "apps": [{"app_id": str(app_id)} for app_id in app_ids],
                "reviews": [
                    {"app_id": str(app_id), "country": country}
                    for app_id, country in review_pairs
                ],
            }, handle)

    def annotation(self, app_id, country, review_id, themes, jtbds,
                   evidence="observation", confidence="high", content_hash=None):
        key = "%s:%s:%s" % (app_id, country, review_id)
        if content_hash is None:
            rows, _ = analysis.load_reviews(self.run_dir)
            content_hash = next(
                (row["content_hash"] for row in rows if row["review_key"] == key),
                "0" * 64,
            )
        return {
            "review_key": key,
            "review_id": str(review_id),
            "app_id": str(app_id),
            "country": country,
            "content_hash": content_hash,
            "theme_ids": themes,
            "jtbd_ids": jtbds,
            "evidence_level": evidence,
            "confidence": confidence,
        }


class ProfileTests(AnalysisFixture):
    def test_profile_uses_unique_full_dataset_and_reports_empty_failed_dataset(self):
        first = review("1", "us", "a", 5, "great", "2025-01-01", response="thanks")
        duplicate = dict(first)
        self.write_dataset("1", "us", [first, duplicate, review(
            "1", "us", "b", 1, "", "2025-04-01", title=""
        )])
        self.write_dataset("1", "cn", [review(
            "1", "cn", "c", 3, "可以", "2025-07-01"
        )], truncated=True)
        self.write_dataset("2", "us", [], status="failed", truncated=True)

        profile, path = analysis.build_profile(self.run_dir)

        self.assertTrue(os.path.exists(path))
        self.assertEqual(profile["raw_row_count"], 4)
        self.assertEqual(profile["duplicate_row_count"], 1)
        source = profile["source_statistics"]
        self.assertEqual(source["overall"]["review_count"], 3)
        self.assertEqual(source["overall"]["valid_text_count"], 2)
        self.assertEqual(source["overall"]["rating_distribution"]["5"], 1)
        self.assertEqual(profile["analysis_population"]["review_count"], 0)
        self.assertTrue(os.path.exists(profile["analysis_population"]["path"]))
        self.assertEqual(source["overall"]["rating_percentages"]["5"], round(1 / 3.0, 6))
        datasets = {(item["app_id"], item["country"]): item for item in source["datasets"]}
        self.assertEqual(datasets[("1", "cn")]["truncated"], True)
        self.assertEqual(datasets[("2", "us")]["review_count"], 0)
        self.assertEqual(datasets[("2", "us")]["crawl_status"], "failed")

    def test_profile_requires_review_files(self):
        with self.assertRaisesRegex(ValueError, "reviews.json"):
            analysis.build_profile(self.run_dir)

    def test_rejects_path_identity_mismatch_and_conflicting_duplicate(self):
        self.write_dataset("1", "us", [
            review("2", "us", "a", 5, "wrong app", "2025-01-01")
        ])
        with self.assertRaisesRegex(ValueError, "与目录不一致"):
            analysis.load_reviews(self.run_dir)

        self.write_dataset("1", "us", [
            review("1", "us", "a", 5, "first", "2025-01-01"),
            review("1", "us", "a", 1, "changed", "2025-01-01"),
        ])
        with self.assertRaisesRegex(ValueError, "内容冲突"):
            analysis.load_reviews(self.run_dir)

    def test_profile_reports_strict_dates_trends_and_common_window(self):
        self.write_dataset("1", "us", [
            review("1", "us", "a", 5, "one", "2025-01-01"),
            review("1", "us", "b", 1, "bad date", "not-a-date"),
        ])
        self.write_dataset("2", "us", [
            review("2", "us", "c", 4, "two", "2025-01-01"),
            review("2", "us", "d", 4, "two", "2025-04-01"),
        ])

        profile, _ = analysis.build_profile(self.run_dir)

        datasets = {
            (item["app_id"], item["country"]): item
            for item in profile["source_statistics"]["datasets"]
        }
        self.assertEqual(datasets[("1", "us")]["invalid_fields"]["date"], 1)
        self.assertEqual(
            datasets[("2", "us")]["quarterly_rating_trend"][0]["average_rating"],
            4.0,
        )
        window = profile["analysis_population"]["comparison_windows"][0]
        self.assertTrue(window["comparable_for_cross_app_time_window"])
        self.assertEqual(window["common_start_date"], "2025-01-01")
        app_one = next(item for item in window["per_app"] if item["app_id"] == "1")
        self.assertEqual(app_one["excluded_from_analysis_count"], 1)

    def test_profile_applies_common_window_to_analysis_population(self):
        self.write_dataset("1", "us", [
            review("1", "us", "jan", 1, "old", "2025-01-01"),
            review("1", "us", "dec", 5, "aligned", "2025-12-01"),
        ])
        self.write_dataset("2", "us", [
            review("2", "us", "dec", 4, "aligned", "2025-12-01")
        ])

        profile, _ = analysis.build_profile(self.run_dir)

        population = profile["analysis_population"]
        self.assertEqual(population["review_count"], 2)
        with open(population["path"], encoding="utf-8") as handle:
            keys = [json.loads(line)["review_key"] for line in handle if line.strip()]
        self.assertEqual(keys, ["1:us:dec", "2:us:dec"])
        aligned = {
            (item["app_id"], item["country"]): item
            for item in population["datasets"]
        }
        self.assertEqual(aligned[("1", "us")]["rating_denominator"], 1)
        self.assertEqual(aligned[("1", "us")]["average_rating"], 5.0)

    def test_manifest_ignores_orphan_app_files(self):
        self.write_dataset("111", "us", [
            review("111", "us", "a", 5, "current", "2025-01-01")
        ])
        self.write_dataset("999", "us", [
            review("999", "us", "z", 1, "orphan", "2025-01-01")
        ])
        self.write_manifest(["111"], [("111", "us")])

        profile, _ = analysis.build_profile(self.run_dir)

        source_apps = {
            item["app_id"] for item in profile["source_statistics"]["datasets"]
        }
        self.assertEqual(source_apps, {"111"})
        self.assertEqual(profile["analysis_population"]["review_count"], 1)

    def test_manifest_missing_competitor_does_not_become_single_app(self):
        self.write_dataset("111", "us", [
            review("111", "us", "a", 5, "current", "2025-01-01")
        ])
        self.write_manifest(["111", "222"], [("111", "us")])

        profile, _ = analysis.build_profile(self.run_dir)

        self.assertEqual(profile["analysis_population"]["review_count"], 0)
        window = profile["analysis_population"]["comparison_windows"][0]
        self.assertEqual(window["app_count"], 2)
        self.assertFalse(window["comparable_for_cross_app_time_window"])
        with self.assertRaisesRegex(ValueError, "共同时间窗口"):
            analysis.build_sample(self.run_dir, per_app=10)


class SampleTests(AnalysisFixture):
    def test_sample_is_reproducible_stratified_and_limited_per_app(self):
        rows_one = []
        rows_two = []
        for index in range(12):
            rows_one.append(review(
                "1", "us" if index % 2 == 0 else "cn", "a%s" % index,
                (index % 5) + 1, "text %s" % index,
                "2024-%02d-01" % ((index % 12) + 1),
            ))
        for index in range(8):
            rows_two.append(review(
                "2", "us" if index % 2 == 0 else "cn", "b%s" % index, (index % 5) + 1,
                "other %s" % index, "2024-%02d-01" % ((index % 8) + 1),
            ))
        self.write_dataset("1", "us", [row for row in rows_one if row["country"] == "us"])
        self.write_dataset("1", "cn", [row for row in rows_one if row["country"] == "cn"])
        self.write_dataset("2", "us", [row for row in rows_two if row["country"] == "us"])
        self.write_dataset("2", "cn", [row for row in rows_two if row["country"] == "cn"])

        selected_one, metadata_one, path = analysis.build_sample(
            self.run_dir, per_app=4, seed=42
        )
        selected_two, metadata_two, _ = analysis.build_sample(
            self.run_dir, per_app=4, seed=42
        )

        self.assertTrue(os.path.exists(path))
        self.assertEqual(
            [item["review_key"] for item in selected_one],
            [item["review_key"] for item in selected_two],
        )
        self.assertEqual(metadata_one["sampled_review_count"], 8)
        self.assertEqual(metadata_one["apps"], metadata_two["apps"])
        counts = {}
        for item in selected_one:
            counts[item["app_id"]] = counts.get(item["app_id"], 0) + 1
        self.assertEqual(counts, {"1": 4, "2": 4})
        countries_for_one = {
            item["country"] for item in selected_one if item["app_id"] == "1"
        }
        self.assertEqual(countries_for_one, {"us", "cn"})
        app_one = next(item for item in metadata_one["apps"] if item["app_id"] == "1")
        self.assertEqual(sum(item["sampled_count"] for item in app_one["strata"]), 4)
        self.assertTrue(all("inclusion_probability" in item for item in app_one["strata"]))

    def test_sample_rejects_non_positive_limit(self):
        self.write_dataset("1", "us", [review("1", "us", "a", 5, "ok", "2025-01-01")])
        with self.assertRaises(ValueError):
            analysis.build_sample(self.run_dir, per_app=0)

    def test_sample_excludes_blank_text_and_rejects_empty_population(self):
        self.write_dataset("1", "us", [
            review("1", "us", "a", 5, "", "2025-01-01"),
            review("1", "us", "b", 4, "kept", "2025-01-01"),
        ])
        selected, metadata, _ = analysis.build_sample(self.run_dir, per_app=10)
        self.assertEqual([item["review_id"] for item in selected], ["b"])
        self.assertEqual(metadata["source_normalized_count"], 2)
        self.assertEqual(metadata["analysis_population_count"], 1)

        self.write_dataset("1", "us", [
            review("1", "us", "a", 5, "  ", "2025-01-01")
        ])
        with self.assertRaisesRegex(ValueError, "非空且满足共同时间窗口"):
            analysis.build_sample(self.run_dir, per_app=10)


class AggregateTests(AnalysisFixture):
    def setUp(self):
        super().setUp()
        self.write_dataset("1", "us", [
            review("1", "us", "a", 5, "great", "2025-01-01"),
            review("1", "us", "b", 1, "bad", "2025-02-01"),
            review("1", "us", "c", 2, "slow", "2025-03-01"),
        ])

    def test_aggregate_requires_full_population_and_computes_rating_metrics(self):
        path = self.write_annotations([
            self.annotation("1", "us", "a", ["T01"], ["J01"]),
            self.annotation("1", "us", "b", ["T01", "T02"], [], confidence="medium"),
            self.annotation("1", "us", "c", [], []),
        ])

        result, output = analysis.aggregate_annotations(self.run_dir, path)

        self.assertTrue(os.path.exists(output))
        self.assertEqual(result["annotation_completed_count"], 3)
        self.assertEqual(result["population_mode"], "full")
        group = result["groups"][0]
        themes = {item["id"]: item for item in group["themes"]}
        self.assertEqual(themes["T01"]["sample_mention_count"], 2)
        self.assertEqual(themes["T01"]["estimated_population_mention_rate"], round(2 / 3.0, 6))
        self.assertEqual(themes["T01"]["estimated_population_average_rating"], 3.0)
        self.assertEqual(themes["T01"]["estimated_population_low_rating_rate"], 0.5)
        self.assertEqual(themes["T02"]["estimated_population_mention_rate"], round(1 / 3.0, 6))

    def test_aggregate_rejects_unknown_or_duplicate_review_keys(self):
        unknown = self.write_annotations([
            self.annotation("1", "us", "missing", ["T01"], [])
        ])
        with self.assertRaisesRegex(ValueError, "未知 review_key"):
            analysis.aggregate_annotations(self.run_dir, unknown)

        duplicate = self.write_annotations([
            self.annotation("1", "us", "a", ["T01"], []),
            self.annotation("1", "us", "a", ["T02"], []),
        ])
        with self.assertRaisesRegex(ValueError, "重复 review_key"):
            analysis.aggregate_annotations(self.run_dir, duplicate)

    def test_aggregate_validates_schema_enums_and_composite_key(self):
        item = self.annotation("1", "us", "a", ["T01"], [])
        item["review_key"] = "wrong"
        path = self.write_annotations([item])
        with self.assertRaisesRegex(ValueError, "复合键不一致"):
            analysis.aggregate_annotations(self.run_dir, path)

        item = self.annotation("1", "us", "a", ["T01"], [])
        item["confidence"] = "certain"
        path = self.write_annotations([item])
        with self.assertRaisesRegex(ValueError, "confidence 无效"):
            analysis.aggregate_annotations(self.run_dir, path)

        item = self.annotation("1", "us", "a", ["T1"], [])
        path = self.write_annotations([item])
        with self.assertRaisesRegex(ValueError, "无效 ID"):
            analysis.aggregate_annotations(self.run_dir, path)

    def test_aggregate_rejects_incomplete_annotations(self):
        path = self.write_annotations([
            self.annotation("1", "us", "a", ["T01"], []),
            self.annotation("1", "us", "b", [], []),
        ])
        with self.assertRaisesRegex(ValueError, "完整覆盖"):
            analysis.aggregate_annotations(self.run_dir, path)

    def test_aggregate_rejects_stale_content_hash(self):
        items = [
            self.annotation("1", "us", review_id, [], [])
            for review_id in ("a", "b", "c")
        ]
        items[0]["content_hash"] = "0" * 64
        path = self.write_annotations(items)
        with self.assertRaisesRegex(ValueError, "content_hash"):
            analysis.aggregate_annotations(self.run_dir, path)


class WeightedAggregateTests(AnalysisFixture):
    def test_stratified_sample_reports_weighted_rate(self):
        rows = [
            review("1", "us", "low%s" % index, 1, "low", "2025-01-01")
            for index in range(9)
        ]
        rows.append(review("1", "us", "high", 5, "high", "2025-01-01"))
        self.write_dataset("1", "us", rows)
        selected, _, _ = analysis.build_sample(self.run_dir, per_app=2, seed=42)
        annotations = []
        for row in selected:
            annotations.append(self.annotation(
                "1", "us", row["review_id"],
                ["T01"] if row["rating"] == 5 else [], [],
            ))
        path = self.write_annotations(annotations)

        result, _ = analysis.aggregate_annotations(self.run_dir, path)

        self.assertEqual(result["population_mode"], "sample")
        theme = result["groups"][0]["themes"][0]
        self.assertEqual(theme["sample_mention_rate"], 0.5)
        self.assertEqual(theme["estimated_population_mention_count"], 1.0)
        self.assertEqual(theme["estimated_population_denominator"], 10.0)
        self.assertEqual(theme["estimated_population_mention_rate"], 0.1)
        self.assertEqual(theme["population_metric_status"], "weighted_api_visible_population_estimate")

    def test_unrepresented_stratum_suppresses_population_rate(self):
        self.write_dataset("1", "us", [
            review("1", "us", "q1", 1, "first", "2025-01-01"),
            review("1", "us", "q2", 5, "second", "2025-04-01"),
        ])
        selected, _, _ = analysis.build_sample(self.run_dir, per_app=1, seed=42)
        row = selected[0]
        path = self.write_annotations([
            self.annotation("1", "us", row["review_id"], ["T01"], [])
        ])

        result, _ = analysis.aggregate_annotations(self.run_dir, path)

        self.assertTrue(result["sample_has_unrepresented_strata"])
        group = result["groups"][0]
        self.assertFalse(group["population_estimate_available"])
        self.assertIsNone(group["themes"][0]["estimated_population_mention_rate"])
        self.assertEqual(group["themes"][0]["sample_mention_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
