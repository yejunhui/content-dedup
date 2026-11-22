"""Group exact duplicates and fingerprint-similar files into safe clusters."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .adapters import AdapterRegistry
from .adapters.base import ContentAdapter, MatchInfo
from .config import AppConfig
from .index import HammingIndex
from .models import DuplicateGroup, DuplicateMember, FileRecord
from .util import safe_relpath


def keeper_key(record: FileRecord, policy: str) -> tuple[Any, ...]:
    path = record.path
    if policy == "newest":
        return (-record.mtime_ns, len(path), path)
    if policy == "largest":
        return (-record.size, len(path), path)
    if policy == "shortest_path":
        return (len(path), record.mtime_ns, path)
    return (record.mtime_ns, len(path), path)


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        parent = self.parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self.rank[root_left] < self.rank[root_right]:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        if self.rank[root_left] == self.rank[root_right]:
            self.rank[root_left] += 1


class Grouper:
    def __init__(self, config: AppConfig, registry: AdapterRegistry) -> None:
        self.config = config
        self.registry = registry
        self.warnings: list[str] = []

    def group(self, records: list[FileRecord], root: str) -> list[DuplicateGroup]:
        self.warnings = []
        if not records:
            return []

        # One entity per exact-hash bucket; exact duplicates are never split.
        buckets: dict[str, list[int]] = defaultdict(list)
        for index, record in enumerate(records):
            buckets[record.exact_hash].append(index)
        entities: list[list[int]] = []
        entity_of_record: list[int] = [0] * len(records)
        for bucket in buckets.values():
            entity_id = len(entities)
            entities.append(bucket)
            for record_index in bucket:
                entity_of_record[record_index] = entity_id

        entity_rep: list[int] = []
        for members in entities:
            entity_rep.append(min(members, key=lambda index: keeper_key(records[index], self.config.safety.keep)))

        union = _UnionFind(len(entities))
        self._union_fingerprint_matches(records, entities, entity_rep, union)

        components: dict[int, list[int]] = defaultdict(list)
        for entity_id in range(len(entities)):
            components[union.find(entity_id)].append(entity_id)

        groups: list[DuplicateGroup] = []
        for component in components.values():
            for cluster in self._star_clusters(records, entities, entity_rep, component):
                group = self._build_group(records, entities, cluster, root)
                if group is not None:
                    groups.append(group)

        groups.sort(key=lambda item: (-item.reclaimable_bytes, item.keeper.path))
        for index, group in enumerate(groups, start=1):
            group.group_id = f"g{index:06d}"
        return groups

    def _union_fingerprint_matches(
        self,
        records: list[FileRecord],
        entities: list[list[int]],
        entity_rep: list[int],
        union: _UnionFind,
    ) -> None:
        grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
        for entity_id, representative in enumerate(entity_rep):
            record = records[representative]
            for fingerprint in record.fingerprints.values():
                grouped[(record.adapter, fingerprint.kind)].append(entity_id)

        for (adapter_name, kind), entity_ids in grouped.items():
            if len(entity_ids) < 2:
                continue
            adapter = self.registry.get(adapter_name)
            if kind in {"phash_seq", "chromaprint"}:
                self._union_sequence_matches(records, entity_rep, entity_ids, adapter, kind, union)
            else:
                self._union_hamming_matches(
                    records, entity_rep, entity_ids, adapter, kind, union
                )

    def _union_hamming_matches(
        self,
        records: list[FileRecord],
        entity_rep: list[int],
        entity_ids: list[int],
        adapter: ContentAdapter,
        kind: str,
        union: _UnionFind,
    ) -> None:
        threshold, bits = self._hamming_settings(adapter, kind)
        values: list[tuple[int, int]] = []
        for entity_id in entity_ids:
            fingerprint = records[entity_rep[entity_id]].fingerprints.get(kind)
            if fingerprint is None:
                continue
            try:
                value = int(str(fingerprint.value), 16)
            except ValueError:
                continue
            values.append((entity_id, value))
        if len(values) < 2:
            return

        if threshold >= bits:
            candidates: Any = None
        else:
            index = HammingIndex(threshold, bits=bits)
            for entity_id, value in values:
                index.add(entity_id, value)
            candidates = index

        for position, (left_id, left_value) in enumerate(values):
            if candidates is None:
                other_ids = [entity_id for entity_id, _ in values[position + 1 :]]
            else:
                other_ids = [item for item in candidates.query(left_value) if item > left_id]
            left_fp = records[entity_rep[left_id]].fingerprints[kind]
            for right_id in other_ids:
                right_fp = records[entity_rep[right_id]].fingerprints.get(kind)
                if right_fp is None:
                    continue
                info = adapter.match(kind, left_fp, right_fp)
                if info is not None and info.similar:
                    union.union(left_id, right_id)

    def _union_sequence_matches(
        self,
        records: list[FileRecord],
        entity_rep: list[int],
        entity_ids: list[int],
        adapter: ContentAdapter,
        kind: str,
        union: _UnionFind,
    ) -> None:
        items: list[tuple[int, float]] = []
        for entity_id in entity_ids:
            fingerprint = records[entity_rep[entity_id]].fingerprints.get(kind)
            if fingerprint is None:
                continue
            try:
                duration = float(fingerprint.meta.get("duration") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            items.append((entity_id, duration))
        items.sort(key=lambda item: item[1])
        tolerance = (
            self.config.video.duration_tolerance
            if adapter.name == "video"
            else self.config.audio.duration_tolerance
        )
        limit = (
            self.config.video.max_pair_comparisons
            if adapter.name == "video"
            else self.config.audio.max_pair_comparisons
        )
        compared = 0
        for position, (left_id, left_duration) in enumerate(items):
            left_fp = records[entity_rep[left_id]].fingerprints[kind]
            for right_id, right_duration in items[position + 1 :]:
                if left_duration > 0 and right_duration > 0:
                    if right_duration > left_duration / max(1e-9, 1.0 - tolerance):
                        break
                    ratio = left_duration / right_duration
                    if ratio < 1.0 - tolerance:
                        continue
                right_fp = records[entity_rep[right_id]].fingerprints.get(kind)
                if right_fp is None:
                    continue
                if compared >= limit:
                    self.warnings.append(
                        f"{adapter.name}: pair comparison limit reached "
                        f"({limit}); some similar files may not be grouped"
                    )
                    return
                compared += 1
                info = adapter.match(kind, left_fp, right_fp)
                if info is not None and info.similar:
                    union.union(left_id, right_id)

    def _hamming_settings(self, adapter: ContentAdapter, kind: str) -> tuple[int, int]:
        if adapter.name == "image":
            threshold = (
                self.config.image.phash_threshold
                if kind == "phash"
                else self.config.image.dhash_threshold
            )
            return threshold, 64
        if adapter.name == "document":
            return self.config.document.simhash_hamming, 64
        if adapter.name == "text":
            return self.config.text.simhash_hamming, 64
        if adapter.name == "archive":
            return 0, 256
        return 0, 64

    def _star_clusters(
        self,
        records: list[FileRecord],
        entities: list[list[int]],
        entity_rep: list[int],
        component: list[int],
    ) -> list[list[int]]:
        ordered = sorted(
            component,
            key=lambda entity_id: keeper_key(
                records[entity_rep[entity_id]], self.config.safety.keep
            ),
        )
        clusters: list[tuple[int, list[int]]] = []
        for entity_id in ordered:
            best: tuple[tuple[Any, ...], int] | None = None
            for cluster_index, (anchor, _) in enumerate(clusters):
                info = self._entity_match(records, entity_rep, anchor, entity_id)
                if info is None or not info.similar:
                    continue
                key = self._match_rank(records, entity_rep, anchor, entity_id, info)
                if best is None or key < best[0]:
                    best = (key, cluster_index)
            if best is None:
                clusters.append((entity_id, [entity_id]))
            else:
                clusters[best[1]][1].append(entity_id)
        return [members for _, members in clusters]

    def _match_rank(
        self,
        records: list[FileRecord],
        entity_rep: list[int],
        left_entity: int,
        right_entity: int,
        info: MatchInfo,
    ) -> tuple[Any, ...]:
        left = records[entity_rep[left_entity]]
        right = records[entity_rep[right_entity]]
        if left.exact_hash == right.exact_hash:
            return (0, 0.0, 0.0)
        if info.distance is not None:
            return (1, float(info.distance), -(info.score or 0.0))
        if info.score is not None:
            return (1, 64.0 * (1.0 - float(info.score)), -float(info.score))
        return (2, 1e18, 0.0)

    def _entity_match(
        self,
        records: list[FileRecord],
        entity_rep: list[int],
        left_entity: int,
        right_entity: int,
    ) -> MatchInfo | None:
        left = records[entity_rep[left_entity]]
        right = records[entity_rep[right_entity]]
        if left.exact_hash == right.exact_hash:
            return MatchInfo(similar=True, distance=0.0, score=1.0, detail={"layer": "exact"})
        if left.adapter != right.adapter:
            return None
        adapter = self.registry.get(left.adapter)
        best: MatchInfo | None = None
        for kind in adapter.fingerprint_kinds:
            left_fingerprint = left.fingerprints.get(kind)
            right_fingerprint = right.fingerprints.get(kind)
            if left_fingerprint is None or right_fingerprint is None:
                continue
            info = adapter.match(kind, left_fingerprint, right_fingerprint)
            if info is None or not info.similar:
                continue
            if best is None or self._info_is_better(info, best):
                best = info
        return best

    @staticmethod
    def _info_is_better(candidate: MatchInfo, current: MatchInfo) -> bool:
        candidate_rank = (
            candidate.distance if candidate.distance is not None else 1e18,
            -(candidate.score or 0.0),
        )
        current_rank = (
            current.distance if current.distance is not None else 1e18,
            -(current.score or 0.0),
        )
        return candidate_rank < current_rank

    def _build_group(
        self,
        records: list[FileRecord],
        entities: list[list[int]],
        cluster: list[int],
        root: str,
    ) -> DuplicateGroup | None:
        record_indices: list[int] = []
        for entity_id in cluster:
            record_indices.extend(entities[entity_id])
        if len(record_indices) < 2:
            return None

        keeper_index = min(
            record_indices, key=lambda index: keeper_key(records[index], self.config.safety.keep)
        )
        keeper = records[keeper_index]
        duplicates: list[DuplicateMember] = []
        reasons: set[str] = set()
        adapter_names: set[str] = set()

        for record_index in record_indices:
            if record_index == keeper_index:
                continue
            record = records[record_index]
            if record.exact_hash == keeper.exact_hash:
                info = MatchInfo(similar=True, distance=0.0, score=1.0, detail={"layer": "exact"})
                reason = "exact"
            else:
                info, reason = self._record_match(records, entities, keeper_index, record_index)
                if info is None or not info.similar:
                    self.warnings.append(
                        f"not grouping {record.path!r} with {keeper.path!r}: "
                        "cached fingerprint no longer matches"
                    )
                    continue
                adapter_names.add(record.adapter)
            detail = dict(info.detail)
            if reason != "exact":
                detail.setdefault("layer", "fingerprint")
                reasons.add(reason)
            duplicates.append(
                DuplicateMember(
                    path=record.path,
                    rel_path=safe_relpath(record.path, root),
                    size=record.size,
                    mtime_ns=record.mtime_ns,
                    adapter=record.adapter,
                    exact_hash=record.exact_hash,
                    reason=reason,
                    distance=info.distance,
                    score=info.score,
                    detail=detail,
                )
            )

        if not duplicates:
            return None
        duplicates.sort(key=lambda item: item.path)
        if reasons:
            if len(reasons) == 1 and len(adapter_names) == 1:
                kind = f"{next(iter(adapter_names))}:{next(iter(reasons))}"
            else:
                kind = "+".join(sorted(reasons))
        else:
            kind = "exact"

        reclaimable = sum(duplicate.size for duplicate in duplicates)
        return DuplicateGroup(
            group_id="",
            kind=kind,
            keeper=DuplicateMember(
                path=keeper.path,
                rel_path=safe_relpath(keeper.path, root),
                size=keeper.size,
                mtime_ns=keeper.mtime_ns,
                adapter=keeper.adapter,
                exact_hash=keeper.exact_hash,
                reason="keeper",
                detail={},
            ),
            duplicates=duplicates,
            reclaimable_bytes=reclaimable,
        )

    def _record_match(
        self,
        records: list[FileRecord],
        entities: list[list[int]],
        keeper_index: int,
        record_index: int,
    ) -> tuple[MatchInfo | None, str]:
        keeper = records[keeper_index]
        record = records[record_index]
        if keeper.adapter != record.adapter:
            return None, ""
        adapter = self.registry.get(keeper.adapter)
        keeper_fingerprints = self._member_fingerprints(records, entities, keeper_index)
        record_fingerprints = self._member_fingerprints(records, entities, record_index)
        best: MatchInfo | None = None
        best_kind = ""
        for kind in adapter.fingerprint_kinds:
            left_fingerprint = keeper_fingerprints.get(kind)
            right_fingerprint = record_fingerprints.get(kind)
            if left_fingerprint is None or right_fingerprint is None:
                continue
            info = adapter.match(kind, left_fingerprint, right_fingerprint)
            if info is None or not info.similar:
                continue
            if best is None or self._info_is_better(info, best):
                best = info
                best_kind = kind
        return best, best_kind

    @staticmethod
    def _member_fingerprints(
        records: list[FileRecord],
        entities: list[list[int]],
        record_index: int,
    ) -> dict:
        record = records[record_index]
        if record.fingerprints:
            return record.fingerprints
        for members in entities:
            if record_index in members:
                for member_index in members:
                    if records[member_index].fingerprints:
                        return records[member_index].fingerprints
        return {}
