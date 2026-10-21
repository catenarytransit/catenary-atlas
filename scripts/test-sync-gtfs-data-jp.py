"""Regression tests for the Japan registry merger (stdlib only)."""
import importlib.util
import unittest
from pathlib import Path

module_path = Path(__file__).with_name("sync-gtfs-data-jp.py")
spec = importlib.util.spec_from_file_location("sync_japan", module_path)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class MergeTests(unittest.TestCase):
    def test_uid_only_change_preserves_identity_without_historic_churn(self):
        before = {"id": "f-a", "spec": "gtfs", "urls": {
            "static_current": "https://example.jp/feed.zip?uid=old"}}
        after = {"id": "f-a", "spec": "gtfs", "urls": {
            "static_current": "https://example.jp/feed.zip"}}
        actual = sync.merge_feed(before, after)
        self.assertEqual(actual["id"], "f-a")
        self.assertNotIn("static_historic", actual["urls"])

    def test_real_url_change_is_archived(self):
        a = {"id": "f-a", "spec": "gtfs", "urls": {
            "static_current": "https://example.jp/old.zip"}}
        b = {"id": "f-a", "spec": "gtfs", "urls": {
            "static_current": "https://example.jp/new.zip"}}
        result = sync.merge_feed(a, b)
        self.assertEqual(result["urls"]["static_historic"],
                         ["https://example.jp/old.zip"])

    def test_duplicate_operator_associations_are_collapsed(self):
        operators = [
            {"onestop_id": "o-a", "name": "A",
             "associated_feeds": [{"feed_onestop_id": "f-a"}]},
            {"onestop_id": "o-a", "name": "A",
             "associated_feeds": [{"feed_onestop_id": "f-b"}]},
        ]
        result = sync.fold_operators([], operators)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["associated_feeds"]), 2)

    def test_duplicate_feed_ids_fail(self):
        source = {"$schema": sync.SCHEMA, "feeds": [
            {"id": "f-a"}, {"id": "f-a"}]}
        with self.assertRaises(sync.RegistryConflict):
            sync.unique_feeds(source, "source")

    def test_ambiguous_operators_fail(self):
        operators = [
            {"onestop_id": "o-a", "name": "Alpha"},
            {"onestop_id": "o-a", "name": "Beta"},
        ]
        with self.assertRaises(sync.RegistryConflict):
            sync.fold_operators([], operators)

    def test_global_url_collision_fails(self):
        own = {"$schema": sync.SCHEMA, "feeds": [
            {"id": "f-a", "urls": {
                "static_current": "https://example.jp/feed.zip"}}]}
        other = [("other.dmfr.json", {"feeds": [
            {"id": "f-b", "urls": {
                "static_current": "https://example.jp/feed.zip?uid=abc"}}]})]
        with self.assertRaises(sync.RegistryConflict):
            sync.validate_target(own, other)

    def test_existing_japanese_id_wins_over_upstream_rename(self):
        legacy = {"id": "f-明石市~たこバス", "spec": "gtfs", "urls": {
            "static_current": "https://example.jp/feed.zip"},
            "license": {"spdx_identifier": "CC-BY-SA-2.1-JP"}}
        incoming = {"id": "f-akashicity~tacobustacobusmini",
                    "supersedes_ids": ["f-明石市~たこバス"],
                    "spec": "gtfs", "urls": {
                        "static_current": "https://example.jp/feed.zip"}}
        other = [("tshimada291.github.com.dmfr.json", {
            "feeds": [legacy]})]
        upstream = {"$schema": sync.SCHEMA, "feeds": [incoming],
                    "operators": [{"onestop_id": "o-akashi", "name": "Akashi",
                                   "associated_feeds": [{"feed_onestop_id": incoming["id"]}]}]}
        aliases = sync.resolve_external_aliases(upstream, other)
        self.assertEqual(aliases, {incoming["id"]: legacy["id"]})
        merged = sync.merge_registry({"$schema": sync.SCHEMA, "feeds": [incoming]},
                                     upstream, aliases)
        self.assertEqual(merged["feeds"], [])
        self.assertEqual(merged["operators"][0]["associated_feeds"][0]
                         ["feed_onestop_id"], legacy["id"])
        sync.validate_target(merged, other)

    def test_same_url_two_old_ids_is_ambiguous(self):
        upstream = {"$schema": sync.SCHEMA, "feeds": [{
            "id": "f-new", "spec": "gtfs",
            "urls": {"static_current": "https://example.jp/feed.zip"}}]}
        other = [("old.dmfr.json", {"feeds": [
            {"id": "f-ひとつ", "spec": "gtfs",
             "urls": {"static_current": "https://example.jp/feed.zip"}},
            {"id": "f-ふたつ", "spec": "gtfs",
             "urls": {"static_current": "https://example.jp/feed.zip"}},
        ]})]
        with self.assertRaises(sync.RegistryConflict):
            sync.resolve_external_aliases(upstream, other)

    def test_conflicting_supersedes_without_matching_url_is_not_auto_merged(self):
        upstream = {"$schema": sync.SCHEMA, "feeds": [{
            "id": "f-new", "spec": "gtfs", "supersedes_ids": ["f-old"],
            "urls": {"static_current": "https://example.jp/new.zip"}}]}
        other = [("old.dmfr.json", {"feeds": [{
            "id": "f-old", "spec": "gtfs",
            "urls": {"static_current": "https://example.jp/old.zip"}}]})]
        with self.assertRaises(sync.RegistryConflict):
            sync.resolve_external_aliases(upstream, other)

    def test_upstream_removal_preserves_local_feed(self):
        old = {"$schema": sync.SCHEMA, "feeds": [
            {"id": "f-local", "spec": "gtfs", "urls": {
                "static_current": "https://example.jp/old.zip"}}]}
        result = sync.merge_registry(old, {"$schema": sync.SCHEMA, "feeds": []})
        self.assertEqual(result["feeds"], old["feeds"])


if __name__ == "__main__":
    unittest.main()
