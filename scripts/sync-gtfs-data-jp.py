#!/usr/bin/env python3
"""Reconcile GTFS Data JP DMFR records without changing existing Onestop identities.

Usage:
  python scripts/sync-gtfs-data-jp.py [--upstream-file path] [--dry-run]
  python scripts/sync-gtfs-data-jp.py --validate-only
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "feeds" / "gtfs-data-jp.dmfr.json"
UPSTREAM = (
    "https://raw.githubusercontent.com/transitland/transitland-atlas/"
    "refs/heads/main/feeds/gtfs-data-jp.dmfr.json"
)
SCHEMA = "https://dmfr.transit.land/json-schema/dmfr.schema-v0.6.0.json"


class RegistryConflict(ValueError):
    pass


def canonical_url(value: str) -> str:
    """Ignore the GTFS Data JP ephemeral uid query key, but no other identity keys."""
    parsed = urlsplit(value.strip())
    query = sorted(
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "uid"
    )
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
         urlencode(query), "")
    )


def unique_feeds(document: dict, label: str) -> dict[str, dict]:
    if document.get("$schema") != SCHEMA:
        raise RegistryConflict(f"{label}: unexpected DMFR schema")
    result = {}
    for feed in document.get("feeds", []):
        feed_id = feed.get("id")
        if not isinstance(feed_id, str) or not feed_id.startswith("f-"):
            raise RegistryConflict(f"{label}: invalid feed ID {feed_id!r}")
        if feed_id in result:
            raise RegistryConflict(f"{label}: duplicate feed ID {feed_id}")
        result[feed_id] = feed
    return result


def merge_feed(previous: dict, incoming: dict) -> dict:
    if previous["id"] != incoming["id"]:
        raise RegistryConflict("attempted to change a feed identity")
    if previous.get("spec") != incoming.get("spec"):
        raise RegistryConflict(f"{previous['id']}: GTFS spec changed")
    merged = copy.deepcopy(previous)
    for key, value in incoming.items():
        if key not in ("urls", "supersedes_ids"):
            merged[key] = copy.deepcopy(value)
    old_urls = previous.get("urls") or {}
    merged_urls = {**copy.deepcopy(old_urls), **copy.deepcopy(incoming.get("urls") or {})}
    old_current = old_urls.get("static_current")
    new_current = merged_urls.get("static_current")
    if old_current and new_current and canonical_url(old_current) != canonical_url(new_current):
        historic = merged_urls.get("static_historic") or []
        if isinstance(historic, str):
            historic = [historic]
        if old_current not in historic:
            historic.append(old_current)
        merged_urls["static_historic"] = historic
    merged["urls"] = merged_urls
    old_aliases = previous.get("supersedes_ids") or []
    new_aliases = incoming.get("supersedes_ids") or []
    if old_aliases or new_aliases:
        merged["supersedes_ids"] = sorted(set(old_aliases) | set(new_aliases))
    return merged


def fold_operators(older: list[dict], newer: list[dict]) -> list[dict]:
    """One operator record per Onestop ID, with all feed associations retained."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for origin, records in (("existing", older), ("upstream", newer)):
        for op in records:
            op_id = op.get("onestop_id")
            if not isinstance(op_id, str) or not op_id.startswith("o-"):
                raise RegistryConflict(f"{origin}: invalid operator ID {op_id!r}")
            groups.setdefault(op_id, []).append((origin, op))

    folded = []
    for op_id, entries in sorted(groups.items()):
        result: dict = {}
        associations: dict[str, dict] = {}
        for origin in ("existing", "upstream"):
            members = [value for source, value in entries if source == origin]
            for field in ("name", "short_name", "website"):
                values = {json.dumps(member[field], ensure_ascii=False, sort_keys=True)
                          for member in members if field in member}
                if len(values) > 1:
                    raise RegistryConflict(f"{op_id}: conflicting {field} in {origin}")
            for member in members:
                for key, value in member.items():
                    if key == "associated_feeds":
                        for association in value:
                            association_id = json.dumps(association, sort_keys=True,
                                                        ensure_ascii=False)
                            associations[association_id] = copy.deepcopy(association)
                    elif key == "tags":
                        result["tags"] = {**result.get("tags", {}), **copy.deepcopy(value)}
                    else:
                        result[key] = copy.deepcopy(value)
        result["associated_feeds"] = sorted(
            associations.values(),
            key=lambda item: (item.get("feed_onestop_id", ""),
                              item.get("gtfs_agency_id", ""))
        )
        folded.append(result)
    return folded


def resolve_external_aliases(
    upstream: dict, other_files: list[tuple[str, dict]]
) -> dict[str, str]:
    """Prefer previously registered feed IDs to upstream renames.

    A feed belongs to the same identity only when its current URL (excluding
    GTFS Data JP's ephemeral uid) matches, or when the Onestop ID is exact.
    Explicit supersedes IDs without a URL match need human review rather
    than silently moving a feed between registry files.
    """
    by_url: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    by_id: dict[str, list[tuple[str, dict]]] = {}
    for path, document in other_files:
        for feed in document.get("feeds", []):
            by_id.setdefault(feed["id"], []).append((path, feed))
            for field, url in current_url_fields(feed):
                by_url.setdefault((field, url), []).append((path, feed))

    aliases: dict[str, str] = {}
    reused: dict[str, str] = {}
    for feed_id, incoming in unique_feeds(upstream, "upstream Japan").items():
        matches = {
            (path, candidate["id"]): candidate
            for field, url in current_url_fields(incoming)
            for path, candidate in by_url.get((field, url), [])
        }
        # Exact IDs take precedence even if the URL has been updated.
        matches.update({
            (path, candidate["id"]): candidate
            for path, candidate in by_id.get(feed_id, [])
        })
        if not matches:
            superseded = [
                (path, candidate["id"])
                for legacy_id in incoming.get("supersedes_ids", [])
                for path, candidate in by_id.get(legacy_id, [])
            ]
            if superseded:
                raise RegistryConflict(
                    f"{feed_id}: supersedes a cataloged feed but its current "
                    f"URL differs; review the existing record(s): {superseded}"
                )
            continue
        if len(matches) != 1:
            raise RegistryConflict(
                f"{feed_id}: ambiguous existing feed identities: {list(matches)}"
            )
        (path, legacy_id), previous = next(iter(matches.items()))
        if incoming.get("spec") != previous.get("spec"):
            raise RegistryConflict(
                f"{feed_id}: spec differs from existing {legacy_id} in {path}"
            )
        prior = reused.get(legacy_id)
        if prior and prior != feed_id:
            raise RegistryConflict(
                f"two upstream feeds ({prior}, {feed_id}) map to {legacy_id}"
            )
        reused[legacy_id] = feed_id
        aliases[feed_id] = legacy_id
    return aliases


def rewrite_operator_feeds(operators: list[dict], aliases: dict[str, str]) -> list[dict]:
    rewritten = copy.deepcopy(operators)
    for op in rewritten:
        for assoc in op.get("associated_feeds", []):
            source_id = assoc.get("feed_onestop_id")
            assoc["feed_onestop_id"] = aliases.get(source_id, source_id)
    return rewritten


def merge_registry(existing: dict, upstream: dict, aliases: dict[str, str] | None = None) -> dict:
    aliases = aliases or {}
    previous = unique_feeds(existing, "local Japan")
    latest = unique_feeds(upstream, "upstream Japan")
    merged = copy.deepcopy(existing)
    # This also removes shadow records accidentally imported in an older sync.
    combined = {
        feed_id: copy.deepcopy(feed)
        for feed_id, feed in previous.items()
        if feed_id not in aliases
    }
    for feed_id, feed in latest.items():
        if feed_id in aliases:
            continue
        combined[feed_id] = (merge_feed(previous[feed_id], feed)
                             if feed_id in previous else copy.deepcopy(feed))
    merged["feeds"] = [combined[key] for key in sorted(combined)]
    merged["operators"] = fold_operators(
        rewrite_operator_feeds(existing.get("operators", []), aliases),
        rewrite_operator_feeds(upstream.get("operators", []), aliases),
    )
    return merged


def current_url_fields(feed: dict):
    for key, value in (feed.get("urls") or {}).items():
        if key == "static_current" or key.startswith("realtime_"):
            if isinstance(value, str) and value:
                yield key, canonical_url(value)


def validate_target(target: dict, other_files: list[tuple[str, dict]]) -> None:
    own_feeds = unique_feeds(target, "merged Japan")
    operator_ids = set()
    for operator in target.get("operators", []):
        op_id = operator.get("onestop_id")
        if not op_id or op_id in operator_ids:
            raise RegistryConflict(f"duplicate or empty operator ID: {op_id}")
        operator_ids.add(op_id)
    other_feed_ids = {}
    other_operator_ids = {}
    existing_urls: dict[tuple[str, str], str] = {}
    for path, doc in other_files:
        for feed in doc.get("feeds", []):
            feed_id = feed.get("id")
            if feed_id:
                other_feed_ids.setdefault(feed_id, path)
            for key, url in current_url_fields(feed):
                existing_urls.setdefault((key, url), f"{path}:{feed_id}")
        for operator in doc.get("operators", []):
            op_id = operator.get("onestop_id")
            if op_id:
                other_operator_ids.setdefault(op_id, path)

    used_urls: dict[tuple[str, str], str] = {}
    for feed_id, feed in own_feeds.items():
        if feed_id in other_feed_ids:
            raise RegistryConflict(
                f"feed ID {feed_id} also defined in {other_feed_ids[feed_id]}"
            )
        for key, url in current_url_fields(feed):
            pair = (key, url)
            owner = used_urls.get(pair) or existing_urls.get(pair)
            if owner and owner != feed_id:
                raise RegistryConflict(
                    f"URL collision: {feed_id} and {owner} share {key}={url}"
                )
            used_urls[pair] = feed_id
    for op_id in operator_ids:
        if op_id in other_operator_ids:
            raise RegistryConflict(
                f"operator ID {op_id} also defined in {other_operator_ids[op_id]}"
            )

    all_ids = set(own_feeds) | set(other_feed_ids)
    for op in target.get("operators", []):
        for association in op.get("associated_feeds", []):
            if association.get("feed_onestop_id") not in all_ids:
                raise RegistryConflict(
                    f"{op['onestop_id']}: association to undefined feed "
                    f"{association.get('feed_onestop_id')}"
                )


def load_other_files(directory: Path, target: Path) -> list[tuple[str, dict]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.dmfr.json"))
        if path.resolve() != target.resolve()
    ]


def fetch_upstream() -> dict:
    req = Request(UPSTREAM, headers={"User-Agent": "catenary-atlas-japan-sync/1.0"})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true",
                        help="offline validation of the checked-in catalog")
    parser.add_argument("--check", action="store_true",
                        help="fail if the import would change the checked-in catalog")
    args = parser.parse_args()
    if args.validate_only and (args.upstream_file or args.dry_run or args.check):
        parser.error("--validate-only cannot be combined with other modes")

    current = json.loads(TARGET.read_text(encoding="utf-8"))
    others = load_other_files(TARGET.parent, TARGET)
    if args.validate_only:
        validate_target(current, others)
        print(f"Validated {len(current['feeds'])} Japan feeds, "
              f"{len(current.get('operators', []))} operators")
        return

    upstream = (json.loads(args.upstream_file.read_text(encoding="utf-8"))
                if args.upstream_file else fetch_upstream())
    aliases = resolve_external_aliases(upstream, others)
    merged = merge_registry(current, upstream, aliases)
    validate_target(merged, others)

    current_ids = unique_feeds(current, "current")
    new_ids = unique_feeds(upstream, "upstream")
    updated = sum(feed_id in current_ids and current_ids[feed_id] != merged_feed
                  for feed_id, merged_feed in
                  ((feed["id"], feed) for feed in merged["feeds"])
                  if feed_id in new_ids)
    added = len(set(new_ids) - set(current_ids) - set(aliases))
    retained = len(set(current_ids) - set(new_ids) - set(aliases))
    print(f"Added {added}, updated {updated}, retained local-only {retained}, "
          f"reused external identities {len(aliases)}; total "
          f"{len(merged['feeds'])} feeds, {len(merged['operators'])} operators")
    if args.check:
        if current != merged:
            raise SystemExit("Japan registry differs from supplied upstream")
    elif not args.dry_run:
        TARGET.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


if __name__ == "__main__":
    main()
