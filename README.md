# dorico-reader-skill (v0.1)

Read-only inspection of Dorico 6 projects, with small JSON outputs for language-model agents. Python 3.10+ standard library only.

- Native `.dorico`: title, composer, player inventories, flows, VST inventory, ZIP structural health.
- Exported MusicXML `.xml/.musicxml/.mxl`: parts, event-level note/rest/direction queries, indexes for repeated queries.
- No native `.dtn` note parser. No editing.

## Start

```bash
python3 scripts/dorico_reader.py info /path/to/score.dorico
python3 scripts/dorico_reader.py players /path/to/score.dorico --match violino
python3 scripts/dorico_reader.py playback /path/to/score.dorico
```

For notes, export a full score to MusicXML from Dorico (File > Export > MusicXML), then:

```bash
python3 scripts/dorico_reader.py index /path/to/score.musicxml --out /tmp/score-index.json
python3 scripts/dorico_reader.py xml-info /tmp/score-index.json
python3 scripts/dorico_reader.py events /tmp/score-index.json --part 'Violino solo' --start 1 --end 12 --type note --fields m,q,pitch,d --limit 24
```

To build an agent sidecar:

```bash
python3 scripts/dorico_prepare.py /path/to/score.dorico --musicxml /path/to/score.musicxml --out /tmp/my-score
```

To list a directory of projects without loading them into agent context:

```bash
python3 scripts/dorico_batch.py /path/to/projects --recursive
```

**Test:** `python3 -m unittest discover -s tests -v`.

**Agent instructions:** see `SKILL.md`, with detailed caveats in `reference/format-and-limits.md`.
