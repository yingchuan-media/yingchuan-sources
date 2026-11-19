import datetime
import json
import tempfile
import unittest
from pathlib import Path

import check_health as health
import check_subscriptions as subscriptions


class HealthArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "checkout"
        (self.root / "subscriptions").mkdir(parents=True)
        archive = self.root / subscriptions.HEALTH_ARCHIVE
        archive.parent.mkdir()
        self.write(archive, {"last_checked_at": None, "pending": {}, "archived": []})
        self.site = {"name": "测试站", "api": "https://catalog.example.test/vod", "disabled": False}
        for group in subscriptions.GROUPS:
            sources = {"sample": self.site} if group == "film-tv" else {}
            self.write(self.subscription(self.root, group), {"cache_time": 7200, "api_site": sources})
        (self.root / "README.md").write_text(
            f"Header\n{health.INVENTORY_START}\n{health.INVENTORY_END}\n", encoding="utf-8")
        self.sequence = 0

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def subscription(root, group="film-tv"):
        return root / "subscriptions" / f"{group}.json"

    def row(self, **overrides):
        return {**health.initial_result("film-tv", "sample", self.site),
                **{field: health.Outcome("ok", "fixture") for field in health.FIELDS},
                **overrides}

    def publish(self, day, row, hour=7):
        self.sequence += 1
        target = self.directory / f"publication-{self.sequence}"
        checked_at = datetime.datetime(2026, 11, day, hour,
                                       tzinfo=subscriptions.SCHEDULE_TZ).isoformat()
        original_files = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        archived = health.prepare_publication(self.root, target, [row], checked_at, "fixture", None)
        self.assertTrue(all(path.read_bytes() == body for path, body in original_files.items()))
        self.root = target
        return archived, subscriptions.read_archive(target)

    def test_seventh_consecutive_failure_moves_full_source_out_of_subscription(self):
        failure = health.Outcome("fail", "HTTP 404")
        for day in range(1, 7):
            archived, state = self.publish(day, self.row(catalog=failure))
            self.assertEqual(archived, 0)
            self.assertEqual(state["pending"]["sample"]["first_failed"], "2026-11-01")
            self.assertEqual(subscriptions.read_json(self.subscription(self.root))["api_site"]["sample"],
                             {**self.site, "name": "不可用 测试站"})
        archived, state = self.publish(7, self.row(catalog=failure))
        self.assertEqual(archived, 1)
        self.assertEqual(state["archived"], [{"group": "film-tv", "key": "sample", "site": self.site,
                                             "first_failed": "2026-11-01", "last_failed": "2026-11-07"}])
        self.assertEqual(state["pending"], {})
        self.assertEqual(subscriptions.check(self.root)["sources"], 0)
        report = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("本次从订阅移出 1 项", report)
        self.assertNotIn("不可用 测试站", report)

    def test_recovery_and_client_reproduction_clear_failure_period(self):
        self.publish(1, self.row(catalog=health.Outcome("fail", "HTTP 404")))
        for day in range(2, 10):
            archived, state = self.publish(
                day, self.row(playback=health.Outcome("unknown", "加密流需客户端验证")))
            self.assertEqual(archived, 0)
            self.assertEqual(state["pending"], {})
        self.assertEqual(subscriptions.read_json(self.subscription(self.root))["api_site"]["sample"]["name"],
                         "不可用 测试站")
        self.publish(10, self.row(cover=health.Outcome("fail", "HTTP 404")))
        self.assertEqual(subscriptions.read_json(self.subscription(self.root))["api_site"]["sample"], self.site)

    def test_same_day_missing_day_and_configuration_change(self):
        failure = self.row(catalog=health.Outcome("fail", "HTTP 503"))
        self.publish(1, failure)
        _, same_day = self.publish(1, failure, hour=20)
        self.assertEqual(same_day["pending"]["sample"]["first_failed"], "2026-11-01")
        with self.assertRaisesRegex(ValueError, "newer"):
            self.publish(1, failure)
        _, after_gap = self.publish(3, failure)
        self.assertEqual(after_gap["pending"]["sample"]["first_failed"], "2026-11-03")
        document = subscriptions.read_json(self.subscription(self.root))
        document["api_site"]["sample"]["api"] = "https://replacement.example.test/vod"
        self.write(self.subscription(self.root), document)
        _, changed = self.publish(4, failure)
        self.assertEqual(changed["pending"]["sample"]["first_failed"], "2026-11-04")

    def test_access_limit_and_single_playback_failure_require_client(self):
        for row in (self.row(catalog=health.Outcome("fail", "HTTP 403")),
                    self.row(catalog=health.Outcome("unknown", "需要本人访问确认")),
                    self.row(playback=health.Outcome("fail", "有界样本未解码首帧"))):
            self.assertEqual(health.update_sources(
                self.root, {"film-tv": subscriptions.read_json(self.subscription(self.root))},
                [row], "2026-11-01T07:00:00+08:00")[0]["pending"], {})
            self.assertEqual(row["name"], "不可用 测试站")

    def test_incomplete_or_duplicate_result_produces_no_candidate(self):
        for rows in ([], [self.row(), self.row()]):
            target = self.directory / "incomplete"
            with self.assertRaisesRegex(ValueError, "match"):
                health.prepare_publication(self.root, target, rows,
                                           "2026-11-01T07:00:00+08:00", "fixture", None)
            self.assertFalse(target.exists())
