#!/usr/bin/env python3
"""Native BSP4 writer/resume/refilter/oracle/corruption regression."""

import argparse
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_organizer as organizer


def organizer_fixture_module():
    path = os.path.join(ROOT, "tests", "pool_organizer.py")
    spec = importlib.util.spec_from_file_location(
        "brainstorm_pool_organizer_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(scanner, *args, expect=True):
    result = subprocess.run(
        [scanner, *args], cwd=ROOT, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    if expect and result.returncode:
        raise AssertionError(
            "command failed (%d): %s\nstdout:\n%s\nstderr:\n%s" %
            (result.returncode, " ".join(args), result.stdout, result.stderr))
    if not expect and not result.returncode:
        raise AssertionError("corrupt pool was accepted: %s" % " ".join(args))
    return result


def criteria(path, count, schema, threads, resume, refilter=False,
             start=0, checkpoint=None, tag_rule=None):
    if checkpoint is None:
        checkpoint = 2048 if refilter else count // 2
    lines = [
        "poolver 1",
        "threads %d" % threads,
        "start %d" % start,
        "count %s" % ("all" if refilter else count),
        "checkpoint %d" % checkpoint,
        "chunk 2048",
        "resume %d" % resume,
        "format binary",
        "output_schema %d" % schema,
        "tag_route collect",
        tag_rule or "tag tag_charm 1 %d 1" % (8 if refilter else 39),
        "end",
        "",
    ]
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(lines))


def header_text(raw):
    prefix = raw[:1024].split(b"\0", 1)[0].decode("ascii")
    header_bytes = int(re.search(
        r"(?m)^header_bytes (\d+)$", prefix).group(1))
    return header_bytes, raw[:header_bytes].split(
        b"\0", 1)[0].decode("ascii")


def header_value(text, key, base=10):
    pattern = r"(?m)^%s ([0-9a-f]+)$" if base == 16 else r"(?m)^%s (\d+)$"
    return int(re.search(pattern % re.escape(key), text).group(1), base)


def replace_one(text, pattern, replacement):
    text, changed = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if changed != 1:
        raise AssertionError("header field did not rewrite once: %s" % pattern)
    return text


def fnv64(payload, value=organizer.FNV64_OFFSET):
    for byte in payload:
        value = ((value ^ byte) * organizer.FNV64_PRIME) & organizer.MASK64
    return value


def snapshot_id(segment_id, records, data_bytes, membership_digest):
    value = ("snapshot:%016x:%016x:%016x:%016x" %
             (segment_id, records, data_bytes,
              membership_digest)).encode("ascii")
    return fnv64(value)


def fabricate_checkpoint(
        source, output, state_path, cursor, rank_cutoff=None):
    raw = open(source, "rb").read()
    reader = organizer.BSPoolReader(source)
    if reader.schema != 4:
        raise AssertionError("checkpoint source is not BSP4")
    cutoff = cursor if rank_cutoff is None else rank_cutoff
    selected = [block for block in reader.blocks if block.last_rank < cutoff]
    if not selected or len(selected) == len(reader.blocks):
        raise AssertionError("checkpoint does not split BSP4 blocks")
    if reader.blocks[len(selected)].first_rank < cutoff:
        raise AssertionError("checkpoint cuts through a BSP4 block")
    prefix_records = sum(block.count for block in selected)
    data_end = reader.blocks[len(selected)].offset
    prefix_data_bytes = data_end - reader.header_bytes
    records = list(reader.iter_records())
    membership = organizer._bsp4_membership_start()
    metadata = organizer._bsp4_metadata_start()
    for block in selected:
        batch = records[
            block.first_record:block.first_record + block.count]
        membership = organizer._bsp4_update_membership_digest(
            membership, [record.rank for record in batch])
        metadata = organizer._bsp4_update_metadata_digest(
            metadata, [record.occurrences for record in batch])

    header_bytes, text = header_text(raw)
    segment = header_value(text, "segment_id", 16)
    identity = snapshot_id(
        segment, prefix_records, prefix_data_bytes, membership)
    for pattern, replacement in (
        (r"^records \d+$", "records %d" % prefix_records),
        (r"^data_bytes \d+$", "data_bytes %d" % prefix_data_bytes),
        (r"^complete [01]$", "complete 0"),
        (r"^coverage_complete [01]$", "coverage_complete 0"),
        (r"^membership_digest [0-9a-f]+$",
         "membership_digest %016x" % membership),
        (r"^metadata_digest [0-9a-f]+$",
         "metadata_digest %016x" % metadata),
        (r"^snapshot_id [0-9a-f]+$",
         "snapshot_id %016x" % identity),
        (r"^scan_cursor \d+$", "scan_cursor %d" % cursor),
    ):
        text = replace_one(text, pattern, replacement)
    if re.search(r"(?m)^input_cursor \d+$", text):
        text = replace_one(
            text, r"^input_cursor \d+$", "input_cursor %d" % cursor)
    encoded = text.encode("ascii")
    if len(encoded) > header_bytes:
        raise AssertionError("checkpoint header overflow")
    with open(output, "wb") as handle:
        handle.write(encoded.ljust(header_bytes, b"\0"))
        handle.write(raw[header_bytes:data_end])

    catalog = re.search(
        r"(?m)^catalog_hash ([0-9a-f]+)$", text).group(1)
    criterion = re.search(
        r"(?m)^criteria_hash ([0-9a-f]+)$", text).group(1)
    range_start = header_value(text, "input_record_start") \
        if re.search(r"(?m)^input_record_start ", text) else \
        header_value(text, "range_start")
    range_end = header_value(text, "input_record_end") \
        if re.search(r"(?m)^input_record_end ", text) else \
        header_value(text, "range_end")
    state = (
        "BRAINSTORM_SEED_POOL_STATE 3\n"
        "catalog_hash %s\ncriteria_hash %s\n"
        "range_start %d\nrange_end %d\n"
        "cursor %d\noutput_bytes %d\n"
        "membership_digest %016x\nmetadata_digest %016x\n"
        "matched %d\nscanned %d\n"
        "elapsed_seconds 0.000000000\ndone 0\nend\n"
    ) % (
        catalog, criterion, range_start, range_end, cursor, data_end,
        membership, metadata, prefix_records, cursor - range_start)
    with open(state_path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(state)


def mutate(source, target, offset):
    shutil.copyfile(source, target)
    with open(target, "r+b") as handle:
        handle.seek(offset)
        value = handle.read(1)
        if not value:
            raise AssertionError("mutation lies outside pool")
        handle.seek(offset)
        handle.write(bytes((value[0] ^ 1,)))


def normalized_summary(scanner, pool, path):
    result = run(scanner, "summarize", pool, "--record-digest")
    if not re.search(
            r"(?m)^record_metadata_digest [0-9a-f]{16}$", result.stdout):
        raise AssertionError("record-level semantic audit digest is missing")
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(result.stdout)
    return "\n".join(
        line for line in result.stdout.splitlines()
        if not line.startswith(("membership_digest ",
                                "metadata_digest ")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scanner", default=os.path.join(
        ROOT, "native",
        "brainstorm_seed_pool.exe" if os.name == "nt"
        else "brainstorm_seed_pool"))
    parser.add_argument("--snapshot", default=os.path.join(
        ROOT, "native_search.cfg"))
    parser.add_argument("--count", type=int, default=8192)
    args = parser.parse_args()
    scanner = os.path.abspath(args.scanner)
    snapshot = os.path.abspath(args.snapshot)
    if args.count < 4096 or args.count % 4096:
        raise SystemExit("--count must be a multiple of 4096 and at least 4096")
    if not os.path.isfile(scanner) or not os.path.isfile(snapshot):
        raise SystemExit("scanner and snapshot must exist")

    with tempfile.TemporaryDirectory(
            prefix="brainstorm-native-bsp4-") as temp:
        v3_cfg = os.path.join(temp, "v3.cfg")
        v4_one_cfg = os.path.join(temp, "v4-one.cfg")
        v4_many_cfg = os.path.join(temp, "v4-many.cfg")
        # Keep the canonical-layout oracle in one publication epoch. A
        # checkpoint is allowed to flush a partial record block, so mixing a
        # mid-scan durability boundary into this fixture would not prove the
        # normal 1K/4K writer capacities. Resume is exercised independently
        # below with an intentional half-range checkpoint.
        criteria(v3_cfg, args.count, 3, 4, 0, checkpoint=args.count)
        criteria(v4_one_cfg, args.count, 4, 1, 0, checkpoint=args.count)
        criteria(v4_many_cfg, args.count, 4, 8, 0,
                 checkpoint=args.count)
        v3 = os.path.join(temp, "v3.bspool")
        v4_one = os.path.join(temp, "v4-one.bspool")
        v4_many = os.path.join(temp, "v4-many.bspool")
        run(scanner, "scan", snapshot, v3_cfg, v3)
        run(scanner, "scan", snapshot, v4_one_cfg, v4_one)
        run(scanner, "scan", snapshot, v4_many_cfg, v4_many)
        if open(v4_one, "rb").read() != open(v4_many, "rb").read():
            raise AssertionError("BSP4 output differs across 1/8 workers")

        reader3 = organizer.BSPoolReader(v3)
        reader4 = organizer.BSPoolReader(v4_one)
        if (any(block.count > organizer.BSP3_WRITE_RECORDS
                for block in reader3.blocks)
                or any(block.count > organizer.BSP4_WRITE_RECORDS
                       for block in reader4.blocks)):
            raise AssertionError("native writer exceeded its schema block bound")
        if (reader3.records >= organizer.BSP3_WRITE_RECORDS
                and reader3.blocks[0].count != organizer.BSP3_WRITE_RECORDS):
            raise AssertionError("BSP3 canonical 1K block boundary changed")
        if (reader4.records >= organizer.BSP4_WRITE_RECORDS
                and reader4.blocks[0].count != organizer.BSP4_WRITE_RECORDS):
            raise AssertionError("BSP4 writer did not use canonical 4K blocks")
        records3 = list(reader3.iter_records())
        records4 = list(reader4.iter_records())
        logical3 = [(record.rank, tuple(
            item.raw for item in record.occurrences)) for record in records3]
        logical4 = [(record.rank, tuple(
            item.raw for item in record.occurrences)) for record in records4]
        if logical3 != logical4:
            raise AssertionError("BSP3/BSP4 logical records differ")
        if reader4.data_bytes >= reader3.data_bytes:
            raise AssertionError("dense BSP4 sample did not compress below BSP3")

        reencoded = os.path.join(temp, "python-reencoded.bspool")
        writer = organizer.BSP4OutputWriter(
            reader4, "native-bsp4-oracle", "native BSP4 oracle", reencoded)
        # Native checkpoint boundaries deliberately flush a partial logical
        # block. Preserve those boundaries so this compares the exact codec
        # bytes rather than merely an equivalent regrouping of records.
        for block in reader4.blocks:
            for record in records4[
                    block.first_record:block.first_record + block.count]:
                writer.add(record)
            writer._flush()
        writer.finalize()
        os.replace(writer.temp_path, writer.final_path)
        python_reader = organizer.BSPoolReader(reencoded)
        native_data = open(v4_one, "rb").read()[
            reader4.header_bytes:reader4.header_bytes + reader4.data_bytes]
        python_data = open(reencoded, "rb").read()[
            python_reader.header_bytes:
            python_reader.header_bytes + python_reader.data_bytes]
        if native_data != python_data:
            raise AssertionError(
                "native BSP4 data differs from Python oracle bytes")

        # A pre-4K BSP4 pool remains a first-class native input. Force the
        # Python oracle to flush the same logical records every 1,024 entries,
        # then exercise native export and summary over those legacy blocks.
        legacy_v4 = os.path.join(temp, "legacy-1k-v4.bspool")
        legacy_writer = organizer.BSP4OutputWriter(
            reader4, "legacy-1k-native-reader",
            "legacy 1K BSP4 native reader", legacy_v4)
        for index, record in enumerate(records4, 1):
            legacy_writer.add(record)
            if index % organizer.BSP3_WRITE_RECORDS == 0:
                legacy_writer._flush()
        legacy_writer.finalize()
        os.replace(legacy_writer.temp_path, legacy_writer.final_path)
        legacy_reader = organizer.BSPoolReader(legacy_v4)
        if (not legacy_reader.blocks
                or legacy_reader.blocks[0].count
                != organizer.BSP3_WRITE_RECORDS):
            raise AssertionError("legacy BSP4 oracle did not use 1K blocks")
        legacy_txt = os.path.join(temp, "legacy-1k-v4.txt")
        canonical_txt = os.path.join(temp, "canonical-4k-v4.txt")
        run(scanner, "export", legacy_v4, legacy_txt)
        run(scanner, "export", v4_one, canonical_txt)
        if open(legacy_txt, "rb").read() != open(canonical_txt, "rb").read():
            raise AssertionError("native legacy-1K/canonical-4K exports differ")
        if normalized_summary(
                scanner, legacy_v4,
                os.path.join(temp, "legacy-1k-v4.summary")) \
                != normalized_summary(
                    scanner, v4_one,
                    os.path.join(temp, "canonical-4k-v4.summary")):
            raise AssertionError(
                "native legacy-1K/canonical-4K summaries differ")

        sparse_cfg = os.path.join(temp, "rice-sparse.cfg")
        sparse_pool = os.path.join(temp, "rice-sparse.bspool")
        sparse_count = max(32768, args.count)
        criteria(
            sparse_cfg, sparse_count, 4, 4, 0,
            checkpoint=sparse_count,
            tag_rule="tag tag_charm 1 small 1 small 1")
        run(scanner, "scan", snapshot, sparse_cfg, sparse_pool)
        sparse_reader = organizer.BSPoolReader(sparse_pool)
        sparse_records = list(sparse_reader.iter_records())
        if not any(block.rank_codec == organizer.BSP4_RANK_RICE
                   for block in sparse_reader.blocks):
            raise AssertionError("sparse native scan did not select Rice ranks")
        sparse_oracle = os.path.join(temp, "rice-python-oracle.bspool")
        sparse_writer = organizer.BSP4OutputWriter(
            sparse_reader, "native-rice-oracle",
            "native Rice oracle", sparse_oracle)
        for block in sparse_reader.blocks:
            for record in sparse_records[
                    block.first_record:block.first_record + block.count]:
                sparse_writer.add(record)
            sparse_writer._flush()
        sparse_writer.finalize()
        os.replace(sparse_writer.temp_path, sparse_writer.final_path)
        sparse_python = organizer.BSPoolReader(sparse_oracle)
        sparse_native_data = open(sparse_pool, "rb").read()[
            sparse_reader.header_bytes:
            sparse_reader.header_bytes + sparse_reader.data_bytes]
        sparse_python_data = open(sparse_oracle, "rb").read()[
            sparse_python.header_bytes:
            sparse_python.header_bytes + sparse_python.data_bytes]
        if sparse_native_data != sparse_python_data:
            raise AssertionError(
                "native Rice block bytes differ from Python oracle")

        merge_clean_cfg = os.path.join(temp, "merge-clean.cfg")
        shard4a_cfg = os.path.join(temp, "shard4a.cfg")
        shard4b_cfg = os.path.join(temp, "shard4b.cfg")
        shard3a_cfg = os.path.join(temp, "shard3a.cfg")
        half = args.count // 2
        criteria(merge_clean_cfg, args.count, 4, 4, 0,
                 checkpoint=args.count)
        criteria(shard4a_cfg, half, 4, 2, 0, start=0,
                 checkpoint=half)
        criteria(shard4b_cfg, half, 4, 2, 0, start=half,
                 checkpoint=half)
        criteria(shard3a_cfg, half, 3, 2, 0, start=0,
                 checkpoint=half)
        merge_clean = os.path.join(temp, "merge-clean.bspool")
        shard4a = os.path.join(temp, "shard4a.bspool")
        shard4b = os.path.join(temp, "shard4b.bspool")
        shard3a = os.path.join(temp, "shard3a.bspool")
        run(scanner, "scan", snapshot, merge_clean_cfg, merge_clean)
        run(scanner, "scan", snapshot, shard4a_cfg, shard4a)
        run(scanner, "scan", snapshot, shard4b_cfg, shard4b)
        run(scanner, "scan", snapshot, shard3a_cfg, shard3a)
        merged4 = os.path.join(temp, "merged4.bspool")
        merged_mixed = os.path.join(temp, "merged-mixed.bspool")
        run(scanner, "merge", merged4, shard4a, shard4b)
        run(scanner, "merge", merged_mixed, shard3a, shard4b)
        clean_reader = organizer.BSPoolReader(merge_clean)
        merged4_reader = organizer.BSPoolReader(merged4)
        mixed_reader = organizer.BSPoolReader(merged_mixed)
        if merged4_reader.schema != 4 or mixed_reader.schema != 4:
            raise AssertionError("adaptive/mixed event merge did not emit BSP4")
        clean_data = open(merge_clean, "rb").read()[
            clean_reader.header_bytes:
            clean_reader.header_bytes + clean_reader.data_bytes]
        merged4_data = open(merged4, "rb").read()[
            merged4_reader.header_bytes:
            merged4_reader.header_bytes + merged4_reader.data_bytes]
        mixed_data = open(merged_mixed, "rb").read()[
            mixed_reader.header_bytes:
            mixed_reader.header_bytes + mixed_reader.data_bytes]
        if clean_data != merged4_data or clean_data != mixed_data:
            raise AssertionError(
                "all-BSP4/mixed shard merge differs from clean BSP4 data")

        # A noncanonical rank-only input makes the entire legacy BSP2 merge
        # preserve physical block order. Canonical neighbors must not remain
        # buffered and get emitted after that middle shard.
        fixtures = organizer_fixture_module()
        rank_first = os.path.join(temp, "rank-first.bspool")
        rank_middle = os.path.join(temp, "rank-middle.bspool")
        rank_last = os.path.join(temp, "rank-last.bspool")
        rank_merged = os.path.join(temp, "rank-merged.bspool")
        fixtures.write_rank_bsp2_shard(
            rank_first, [[1, 2]], 0, 10)
        fixtures.write_rank_bsp2_shard(
            rank_middle, [[15, 16], [11, 12]], 10, 20)
        fixtures.write_rank_bsp2_shard(
            rank_last, [[21, 22]], 20, 30)
        run(scanner, "merge", rank_merged,
            rank_last, rank_first, rank_middle)
        rank_export = os.path.join(temp, "rank-merged.txt")
        run(scanner, "export", rank_merged, rank_export)
        with open(rank_export, encoding="ascii") as handle:
            rank_order = [line.strip() for line in handle if line.strip()]
        expected_rank_order = [
            organizer.rank_to_seed(rank, organizer.NATURAL_CHARSET)
            for rank in (1, 2, 15, 16, 11, 12, 21, 22)
        ]
        if rank_order != expected_rank_order:
            raise AssertionError(
                "mixed canonical/noncanonical BSP2 shards changed part order")

        upgraded = os.path.join(temp, "upgraded-v3.bspool")
        run(scanner, "upgrade", v3, upgraded)
        upgraded_reader = organizer.BSPoolReader(upgraded)
        upgraded_data = open(upgraded, "rb").read()[
            upgraded_reader.header_bytes:
            upgraded_reader.header_bytes + upgraded_reader.data_bytes]
        if upgraded_reader.schema != 4 or upgraded_data != clean_data:
            raise AssertionError(
                "streaming BSP3 upgrade differs from canonical BSP4 data")

        # Historical multi-thread BSP3 writers could commit individually
        # sorted blocks in worker-completion order. Upgrade must normalize
        # both disjoint and overlapping intervals without losing metadata.
        historical_overlap = os.path.join(
            temp, "historical-overlap.bspool")
        historical_disjoint = os.path.join(
            temp, "historical-disjoint.bspool")
        historical_boundary = os.path.join(
            temp, "historical-boundary.bspool")
        fixtures.write_out_of_order_bsp3(
            historical_overlap, True, long_opaque=True)
        fixtures.write_out_of_order_bsp3(
            historical_disjoint, False, long_opaque=True)
        fixtures.write_out_of_order_bsp3(
            historical_boundary, boundary=True)
        for source, stem in (
                (historical_overlap, "historical-overlap-v4"),
                (historical_disjoint, "historical-disjoint-v4"),
                (historical_boundary, "historical-boundary-v4")):
            target = os.path.join(temp, stem + ".bspool")
            result = run(scanner, "upgrade", source, target)
            if "normalizing historical event block order" not in result.stderr:
                raise AssertionError(
                    "historical upgrade did not report normalization")
            source_records = list(
                organizer.BSPoolReader(source).iter_records())
            records = list(organizer.BSPoolReader(target).iter_records())
            if [record.rank for record in records] != [
                    record.rank for record in source_records]:
                raise AssertionError(
                    "historical BSP3 upgrade did not globally order ranks")
            if [[occurrence.raw for occurrence in record.occurrences]
                    for record in records] != [
                    [occurrence.raw for occurrence in record.occurrences]
                    for record in source_records]:
                raise AssertionError(
                    "historical BSP3 upgrade changed event metadata")

        # The same per-input fallback must work in a mixed BSP3/BSP4 shard
        # merge, not only through the one-input ``upgrade`` alias.
        historical_second = os.path.join(
            temp, "historical-second-shard.bspool")
        canonical_second = os.path.join(
            temp, "historical-second-shard-v4.bspool")
        historical_merged = os.path.join(
            temp, "historical-mixed-merge.bspool")
        historical_reversed = os.path.join(
            temp, "historical-reversed-merge.bspool")
        fixtures.write_out_of_order_bsp3(
            historical_second, True, range_start=100, range_end=200)
        run(scanner, "upgrade", historical_second, canonical_second)
        run(scanner, "merge", historical_merged,
            historical_overlap, canonical_second)
        expected_records = list(
            organizer.BSPoolReader(historical_overlap).iter_records())
        expected_records.extend(
            organizer.BSPoolReader(canonical_second).iter_records())
        mixed_records = list(
            organizer.BSPoolReader(historical_merged).iter_records())
        if [(record.rank, [item.raw for item in record.occurrences])
                for record in mixed_records] != [
                (record.rank, [item.raw for item in record.occurrences])
                for record in expected_records]:
            raise AssertionError(
                "mixed historical BSP3/BSP4 merge changed ordered records")
        canonical_first = os.path.join(
            temp, "historical-overlap-v4.bspool")
        run(scanner, "merge", historical_reversed,
            canonical_first, historical_second)
        reversed_records = list(
            organizer.BSPoolReader(historical_reversed).iter_records())
        if [(record.rank, [item.raw for item in record.occurrences])
                for record in reversed_records] != [
                (record.rank, [item.raw for item in record.occurrences])
                for record in expected_records]:
            raise AssertionError(
                "canonical-first historical merge changed ordered records")

        duplicate = os.path.join(temp, "historical-duplicate.bspool")
        duplicate_output = os.path.join(
            temp, "historical-duplicate-v4.bspool")
        fixtures.write_out_of_order_bsp3(
            duplicate, overlapping=True, duplicate=True)
        duplicate_result = run(
            scanner, "upgrade", duplicate, duplicate_output, expect=False)
        if "duplicate" not in duplicate_result.stderr \
                or os.path.exists(duplicate_output):
            raise AssertionError(
                "duplicate historical ranks did not fail atomically")

        damaged = os.path.join(temp, "historical-damaged.bspool")
        damaged_output = os.path.join(
            temp, "historical-damaged-v4.bspool")
        shutil.copyfile(historical_overlap, damaged)
        with open(damaged, "r+b") as handle:
            handle.seek(8192 + 48)
            value = handle.read(1)
            handle.seek(8192 + 48)
            handle.write(bytes((value[0] ^ 1,)))
        run(scanner, "upgrade", damaged, damaged_output, expect=False)
        if os.path.exists(damaged_output):
            raise AssertionError(
                "corrupt historical upgrade did not fail atomically")

        v3_txt = os.path.join(temp, "v3.txt")
        v4_txt = os.path.join(temp, "v4.txt")
        run(scanner, "export", v3, v3_txt)
        run(scanner, "export", v4_one, v4_txt)
        if open(v3_txt, "rb").read() != open(v4_txt, "rb").read():
            raise AssertionError("native BSP3/BSP4 exports differ")
        if normalized_summary(scanner, v3, os.path.join(temp, "v3.summary")) \
                != normalized_summary(
                    scanner, v4_one, os.path.join(temp, "v4.summary")):
            raise AssertionError("native BSP3/BSP4 summaries differ")
        if "record_metadata_digest " in run(
                scanner, "summarize", v4_one).stdout:
            raise AssertionError(
                "normal summary unexpectedly paid for record-level audit")

        scan_resume_cfg = os.path.join(temp, "scan-resume.cfg")
        criteria(scan_resume_cfg, args.count, 4, 4, 1)
        scan_resume_clean = os.path.join(temp, "scan-resume-clean.bspool")
        run(scanner, "scan", snapshot, scan_resume_cfg, scan_resume_clean)
        scan_resumed = os.path.join(temp, "scan-resumed.bspool")
        fabricate_checkpoint(
            scan_resume_clean, scan_resumed, scan_resumed + ".state",
            args.count // 2)
        resumed = run(
            scanner, "scan", snapshot, scan_resume_cfg, scan_resumed)
        if "resuming at rank %d " % (args.count // 2) not in resumed.stderr:
            raise AssertionError("scan did not resume from BSP4 checkpoint")
        if open(scan_resume_clean, "rb").read() != open(
                scan_resumed, "rb").read():
            raise AssertionError("resumed BSP4 scan differs from clean scan")

        refilter_cfg = os.path.join(temp, "refilter.cfg")
        criteria(refilter_cfg, args.count, 4, 4, 1, refilter=True)
        refilter_clean = os.path.join(temp, "refilter-clean.bspool")
        run(scanner, "refilter", snapshot, refilter_cfg, v4_one,
            refilter_clean)
        refilter_cursor = 2048
        refilter_resumed = os.path.join(temp, "refilter-resumed.bspool")
        fabricate_checkpoint(
            refilter_clean, refilter_resumed,
            refilter_resumed + ".state", refilter_cursor,
            rank_cutoff=records4[refilter_cursor].rank)
        resumed = run(scanner, "refilter", snapshot, refilter_cfg, v4_one,
                      refilter_resumed)
        if "resuming at input record %d " % refilter_cursor \
                not in resumed.stderr:
            raise AssertionError("refilter did not resume from BSP4 checkpoint")
        if open(refilter_clean, "rb").read() != open(
                refilter_resumed, "rb").read():
            raise AssertionError(
                "resumed BSP4 refilter differs from clean refilter")

        first = reader4.blocks[0]
        rank_bad = os.path.join(temp, "rank-bad.bspool")
        mutate(v4_one, rank_bad, first.offset + first.header_bytes)
        run(scanner, "export", rank_bad, os.path.join(temp, "bad.txt"),
            expect=False)
        metadata_bad = os.path.join(temp, "metadata-bad.bspool")
        mutate(v4_one, metadata_bad,
               first.offset + first.header_bytes + first.rank_bytes)
        run(scanner, "summarize", metadata_bad, expect=False)
        index_bad = os.path.join(temp, "index-bad.bspool")
        mutate(v4_one, index_bad,
               reader4.header_bytes + reader4.data_bytes + 48)
        run(scanner, "export", index_bad, os.path.join(temp, "bad2.txt"),
            expect=False)
        footer_bad = os.path.join(temp, "footer-bad.bspool")
        mutate(v4_one, footer_bad, os.path.getsize(v4_one) - 1)
        run(scanner, "export", footer_bad, os.path.join(temp, "bad3.txt"),
            expect=False)

        ratio = reader4.data_bytes / reader3.data_bytes
        print(
            "PASS: native BSP4 fresh/deterministic/resume/refilter/export/"
            "summary/merge/upgrade/corruption; data %d -> %d bytes "
            "(%.1f%% smaller)" %
            (reader3.data_bytes, reader4.data_bytes, (1.0 - ratio) * 100.0))


if __name__ == "__main__":
    main()
