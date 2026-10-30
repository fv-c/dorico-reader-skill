# Format coverage, limitations and agent operating protocol

## Dorico native project

`.dorico` is a ZIP archive containing `score.dtn` and `scorelibrary.dtn`, which hold proprietary binary score data. The project may also embed XML metadata, mixer data, SVG resources and optional preview PDF(s). The binary score file is **not** MusicXML. This skill does **not** reverse-engineer the binary score representation.

The reader extracts only verified structured data:

| Command | Source | Guaranteed scope |
|---|---|---|
| `info` / `players` / `flows` | `supplementary_data/scoreinfo/scoreinfo.xml` | Project info, player names, flow names/duration when populated |
| `playback` | embedded mixer XML, if present | Instantiated VST names, plugin slots, number of return channels |
| `check` | ZIP integrity, mandatory members, metadata XML | Container inspection, **not** a Dorico semantic validator |
| `assets` | ZIP preview files | Inventory of optional preview assets, not notation OCR |
| `xml-info` | MusicXML / MXL | Part info and parsed score statistics |
| `events` | MusicXML / MXL or compact JSON index | Notes, rests, tempo/dynamic/text directions, time and key signatures |
| `statistics` | MusicXML / MXL / index | Per-part note/rest/direction counts and measure activity |
| `activity` | MusicXML / MXL / index | Per-measure active-part counts and optional note counts per part |

**Provenance:** project metadata and MusicXML exports may diverge after edits. The sidecar generator warns if MusicXML file modification time is older than the `.dorico`. A matching timestamp does not prove the contents are aligned. MusicXML may contain only selected flows/layouts and may split movements into multiple files; inspect exports and index each as necessary. A Dorico *player* does not always correspond one-to-one with a MusicXML *part*.

## Exporting MusicXML in Dorico 6

File > Export > MusicXML > select all relevant flows > export full-score layout (or leave separate-layout export unchecked) > `.musicxml` or `.mxl`. Export each flow if you need independent analyses. Use an updated export after changing the project.

## MusicXML normalization

`events` result is a compact JSON wrapper with `total`, `offset`, `returned`, `truncated`, and `events`. Event key mapping:

- `p` = MusicXML part ID; `m` = original measure label (string, supports pickups/non-numeric labels); `q` = onset in quarter-note units within measure (`0`, `1/2`, `3` etc.)
- `t` = `note`, `rest`, `direction`, `key`, `time`; `d` = duration in quarter-note units; `pitch` = spelling from written MusicXML pitch (e.g. `F#4`, `Bb3`, `C[+1/2]5`)
- `v` = voice; `s` = staff; `chord` = chord continuation at previous onset; `grace` = grace note; `art` = abbreviated MusicXML articulations/slurs/ornaments; `tie` = start/stop; `lyric` = attached text; `x` = direction/key/time content

`--part` matches exact names/IDs first, then a unique substring. Ambiguous filters fail rather than silently selecting a wrong part. `--pitch` uses exact spelling. `--start`/`--end` work as integer bounds for ordinary numeric measures; use `--measure` for nonnumeric IDs. Offset and durations are symbolic (NOT actual performed time). `note` is one notehead: a chord of three notes yields three `note` entries. A tie across a barline yields two written note events. Transposing instruments and concert-pitch settings require manual checking of Dorico export configuration.

Exact part IDs take priority over names. Decimal divisions, durations and direction offsets are converted to exact fractions, following the [MusicXML divisions type](https://www.w3.org/2021/06/musicxml40/musicxml-reference/data-types/divisions/). Invalid rhythmic values fail explicitly. Grace notes without a duration have duration `0`. Composite and staff-specific [time signatures](https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/time/) are retained. Activity includes empty measures in newly generated indexes; older indexes without `measure_labels` can report only measures represented by events.

Indexes store `sha256` for the original input file bytes and `xml_sha256` for the decoded XML payload. These differ for compressed `.mxl` files. Regenerate indexes created by older versions before comparing source hashes. Sidecar contents use filenames and relative index references, without absolute source paths; titles, composer names, player names and other requested score metadata are preserved.

### Not yet covered

Native `.dtn` musical events; native layout/engraving/edit operations; playback routing to individual staves; plugin internal preset state; reliable fader reconstruction; **tempo maps in seconds**, swing, repeats/da capo performance unfolding, tuplets as notated groupings (quantitative actual `duration` is supported), nonstandard nested polymetric MusicXML, MusicXML `score-timewise`, detailed lyric syllabic structure, braille, graphic positions and handwritten annotations. Scripts perform no score modification.

## Token-saving strategy

- Have the agent read `SKILL.md` only and execute a CLI; do not paste binary or XML.
- On an initial project query, `info` costs only a small JSON object.
- For recurring MusicXML analysis, `dorico_prepare.py` builds `music-index.json` once; reuse this for dozens of filtered queries.
- Select one part + a small measure span + field projection + a limit. Paginate using `--offset` only when needed.
- If another agent needs the data, pass the `manifest.json` path or an exact filtered JSON excerpt, not the entire MusicXML.
- Never consider an index up-to-date after editing the original .dorico or the MusicXML; compare SHA256 and regenerate.

## Security and installation

Python 3.10+ standard library; no network, plugins, native executables or destructive operations. Extraction is read-only. The sidecar and index generators write **only to user-selected destination paths**. Copy the `dorico-reader-skill` folder into your agent's configured skills directory (e.g. `~/.codex/skills/dorico-reader/` for Codex), or point the agent at `SKILL.md` for a local workflow. Run `python3 -m unittest discover -s tests -v` to check the bundled functionality.

Index destinations must end in `.json`. Generated JSON files are replaced atomically; outputs that alias an input file, including hard links and symbolic links, are rejected. Each file replacement is atomic; a sidecar's two files are not a multi-file transaction.

All CLI errors are JSON on stderr with exit status `2`. `check` returns its diagnostic JSON on stdout and status `1` when the container is invalid. Batch scans emit one JSON line per project and status `1` if any listed project failed; otherwise commands return `0`. List queries accept `--limit 0..200` and nonnegative `--offset`; batch accepts `--limit 1..1000`. `--help` prints ordinary help text. No personal projects are bundled; the optional real-project test runs only when `DORICO_TEST_FILE` is explicitly supplied.
