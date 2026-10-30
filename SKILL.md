---
name: dorico-reader
description: Read Dorico 6 .dorico projects and MusicXML exports with compact, targeted CLI queries. Use for Dorico project metadata, instrumentation, flows, VST inventory, and score-note analysis without dumping files into context.
---

# Dorico Reader — low-token protocol

**Read-only.** Never edit a `.dorico` ZIP or its `score.dtn` / `scorelibrary.dtn` members. Use Python 3.10+; no dependencies. Run from this skill directory:

```bash
python3 scripts/dorico_reader.py info /path/project.dorico
```

## Decision rule

1. **Native `.dorico`**: run `info`, then only the smallest necessary query (`players --match`, `flows`, `playback`, `check`). Metadata is from `supplementary_data/scoreinfo/scoreinfo.xml`; VST inventory is read from the embedded mixer XML.
2. **Notes, measures, articulations, directions, pitch, durations**: require a corresponding Dorico **MusicXML export**, preferably uncompressed `.musicxml` or compressed `.mxl`. Never infer notes from binary `.dtn` or from embedded SVG graphics. If export is missing, report that limitation and request/export MusicXML.
3. **Repeated score queries**: create an index once, then run narrowly filtered `events` queries against it. Index and queries print **JSON only**, compact by default.
4. **Accuracy**: MusicXML represents the *export*, not every Dorico-specific engraving property. Pay attention to export freshness, layout/flow selections, transpositions and written vs concert pitches. No claim of verified fader values from `playback`.

## Minimum commands

```bash
python3 scripts/dorico_reader.py info score.dorico
python3 scripts/dorico_reader.py players score.dorico --match 'Violino'
python3 scripts/dorico_reader.py flows score.dorico
python3 scripts/dorico_reader.py playback score.dorico
python3 scripts/dorico_reader.py check score.dorico
python3 scripts/dorico_reader.py xml-info export.musicxml
python3 scripts/dorico_reader.py index export.musicxml --out /tmp/dorico-index.json
python3 scripts/dorico_reader.py events /tmp/dorico-index.json --part 'Violino solo' --start 1 --end 8 --type note --fields m,q,pitch,d,art --limit 24
python3 scripts/dorico_reader.py events /tmp/dorico-index.json --part 'Violino solo' --pitch C#4 --limit 15
python3 scripts/dorico_reader.py statistics /tmp/dorico-index.json
python3 scripts/dorico_reader.py activity /tmp/dorico-index.json --start 1 --end 10
python3 scripts/dorico_prepare.py score.dorico --musicxml export.musicxml --out /tmp/score-sidecar
python3 scripts/dorico_batch.py /path/to/projects --recursive
```

**Do not cat or paste** entire MusicXML, indexes, ZIP listings, or `.dtn` in the agent's context. Filter by part/measure/type; use `--limit`, `--offset` and `--fields`. Use `--pretty` only for debugging. Query `--help` for additional flags. For MusicXML, `q` and `d` are quarter-note units (fractions as strings), not seconds.

Use a `.json` destination for `index --out`; source aliases are rejected. Exit `2` reports an input error as JSON on stderr. Exit `1` from `check` or batch reports failed checks/projects in JSON on stdout. Index `sha256` hashes the source file bytes, including the container for `.mxl`; regenerate the index after source changes.

Read `reference/format-and-limits.md` only when troubleshooting a specific format issue.
