#!/usr/bin/env python3
"""Read-only, token-efficient Dorico 6 package and MusicXML reader. Python 3.10+ stdlib only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath

SCHEMA_VERSION = 1
MAX_ENTRY = 20_000_000  # XML payload limit, not the native DTN payload
MAX_SOURCE = 120_000_000
DEFAULT_LIMIT = 24
INPUT_ERRORS = (ValueError, OSError, ET.ParseError, zipfile.BadZipFile,
                zipfile.LargeZipFile, zlib.error, KeyError, RuntimeError, NotImplementedError, EOFError)
if zipfile.lzma is not None:
    INPUT_ERRORS += (zipfile.lzma.LZMAError,)


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, json.dumps({'error': message}) + '\n')


def emit_json(data: dict, *, pretty: bool = False, error: bool = False) -> None:
    # ASCII escapes keep stdout valid even on Windows terminals with legacy encodings.
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(data, indent=2 if pretty else None,
                            separators=None if pretty else (',', ':')) + '\n')


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1_048_576), b''):
            h.update(block)
    return h.hexdigest()


def validate_output(path: Path, sources: list[Path]) -> None:
    if path.suffix.lower() != '.json':
        raise ValueError('Output file must end in .json')
    for source in sources:
        if path.resolve() == source.resolve() or (
                path.exists() and source.exists() and path.samefile(source)):
            raise ValueError('Output would overwrite a source file')
    if path.is_symlink():
        raise ValueError('Output must not be a symbolic link')
    if path.exists() and not path.is_file():
        raise ValueError('Output destination is not a regular file')


def write_json(path: Path, data: dict) -> None:
    """Replace one generated JSON file atomically, leaving no partial output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=False, separators=(',', ':'))
            output.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def unsafe_member(name: str) -> bool:
    return ('\\' in name or PurePosixPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive) or '..' in PurePosixPath(name).parts)


def tag(x: str) -> str:
    return x.rsplit('}', 1)[-1]


def child(el: ET.Element | None, name: str) -> ET.Element | None:
    return next((c for c in el if tag(c.tag) == name), None) if el is not None else None


def children(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in el if tag(c.tag) == name] if el is not None else []


def value(el: ET.Element | None, name: str, default: str = '') -> str:
    node = child(el, name)
    return (node.text or '').strip() if node is not None else default


def read_zip_member(z: zipfile.ZipFile, name: str, max_bytes: int = MAX_ENTRY) -> bytes:
    if sum(info.filename == name for info in z.infolist()) > 1:
        raise ValueError('Duplicate ZIP member: ' + name)
    info = z.getinfo(name)
    if info.file_size > max_bytes:
        raise ValueError(f'Entry too large: {name} ({info.file_size} bytes)')
    return z.read(name)


def project_data(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        info_path = 'supplementary_data/scoreinfo/scoreinfo.xml'
        if info_path not in names:
            raise ValueError('Missing supplementary_data/scoreinfo/scoreinfo.xml (cannot read metadata)')
        root = ET.fromstring(read_zip_member(z, info_path))
        if tag(root.tag) != 'Score':
            raise ValueError('Unexpected scoreinfo.xml root')
        meta_fields = ('title', 'subtitle', 'dedication', 'composer', 'compositionYear',
                       'createdDate', 'lastSavedDate', 'createdVersion',
                       'lastSavedVersion', 'projectDuration')
        meta = {k: value(root, k) for k in meta_fields}
        pls = [{'id': n + 1, 'name': value(p, 'name')} for n, p in
               enumerate(children(child(root, 'Players'), 'Player'))]
        fls = [{'id': n + 1, 'title': value(f, 'title'),
                'duration': value(f, 'flowDuration')} for n, f in
               enumerate(children(child(root, 'Flows'), 'Flow'))]
        return {'source': 'dorico-package', 'filename': path.name, 'metadata': meta,
                'players': pls, 'flows': fls,
                'assets': sorted(n for n in names if
                                 n.startswith('supplementary_data/preview/') and not n.endswith('/')),
                'has_native_score': 'score.dtn' in names,
                'has_native_library': 'scorelibrary.dtn' in names}


def project_playback(path: Path) -> dict:
    """Read VST instance names only; VST internal state and per-channel gain are NOT decoded."""
    with zipfile.ZipFile(path) as z:
        name = 'supplementary_data/vstaudioengine/enginedata'
        if name not in z.namelist():
            return {'source': 'mixer-xml', 'plugins': [], 'warning': 'No engine data'}
        data = read_zip_member(z, name)
        a, b = data.find(b'<mixer'), data.find(b'</mixer>')
        if a < 0 or b < a:
            return {'source': 'mixer-xml', 'plugins': [], 'warning': 'Mixer XML unavailable'}
        root = ET.fromstring(data[a:b + len(b'</mixer>')])
        insts = []
        for item in root.findall('./instruments/instrument'):
            returns = item.findall('./instrument-channels/instrument-return-channel')
            insts.append({'name': item.get('name', ''),
                          'slot': item.get('slot', ''),
                          'return_channels': len(returns)})
        return {'source': 'mixer-xml', 'plugins': insts,
                'caveat': 'This is VST instance inventory, NOT instrument assignment or fader values.'}


def project_check(path: Path) -> dict:
    errors, warnings = [], []
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if len(names) != len(z.infolist()):
                errors.append('Duplicate ZIP entry names')
            for n in names:
                if unsafe_member(n):
                    errors.append('Unsafe ZIP entry path: ' + n)
            broken = z.testzip()
            if broken:
                errors.append('Corrupted ZIP member: ' + broken)
            for required in ('score.dtn', 'scorelibrary.dtn',
                             'META-INF/container.xml',
                             'supplementary_data/scoreinfo/scoreinfo.xml'):
                if required not in names:
                    errors.append('Missing: ' + required)
            if 'score.dtn' in names:
                with z.open('score.dtn') as fh:
                    pre = fh.read(12)
                if len(pre) < 12:
                    errors.append('score.dtn header truncated')
                elif int.from_bytes(pre[:4], 'little') != 4:
                    warnings.append('Unrecognized score.dtn header; binary format unsupported')
            for n in ('META-INF/container.xml',
                      'supplementary_data/scoreinfo/scoreinfo.xml'):
                if n in names:
                    try:
                        root = ET.fromstring(read_zip_member(z, n))
                        expected = 'Score' if n.endswith('scoreinfo.xml') else 'container'
                        if tag(root.tag) != expected:
                            raise ValueError('Unexpected root: ' + tag(root.tag))
                    except (ValueError, ET.ParseError) as e:
                        errors.append(f'Invalid XML {n}: {e}')
            if not any(n.startswith('supplementary_data/preview/') and n.lower().endswith('.pdf')
                       for n in names):
                warnings.append('No embedded preview PDF (optional)')
    except INPUT_ERRORS as e:
        errors.append(f'Not a valid Dorico ZIP: {e}')
    return {'valid_container': not errors, 'errors': errors, 'warnings': warnings,
            'limitation': 'ZIP checks are not a Dorico-level musical consistency check.'}


def read_musicxml(path: Path) -> bytes:
    if path.stat().st_size > MAX_SOURCE:
        raise ValueError('Source too large; MAX_SOURCE exceeded')
    if path.suffix.lower() != '.mxl':
        return path.read_bytes()
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if len(names) != len(z.infolist()):
            raise ValueError('Duplicate MXL entry names')
        target = None
        if 'META-INF/container.xml' in names:
            root = ET.fromstring(read_zip_member(z, 'META-INF/container.xml'))
            roots = children(child(root, 'rootfiles'), 'rootfile')
            if tag(root.tag) != 'container' or not roots or not roots[0].get('full-path'):
                raise ValueError('Invalid MXL container.xml rootfile declaration')
            if roots[0].get('media-type', 'application/vnd.recordare.musicxml+xml') != 'application/vnd.recordare.musicxml+xml':
                raise ValueError('First MXL rootfile is not MusicXML')
            target = roots[0].get('full-path')
        if not target:
            options = [n for n in names if n.lower().endswith(('.xml', '.musicxml'))
                       and not n.startswith('META-INF/')]
            if len(options) != 1:
                raise ValueError('Cannot identify unique MusicXML member in .mxl')
            target = options[0]
        if unsafe_member(target):
            raise ValueError('Unsafe MXL rootfile path')
        return read_zip_member(z, target, MAX_SOURCE)


def ratio(n: Fraction) -> str:
    return str(n.numerator) if n.denominator == 1 else f'{n.numerator}/{n.denominator}'


def decimal_fraction(raw: str, field: str, *, positive: bool = False) -> Fraction:
    try:
        if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', raw):
            raise ValueError
        number = Fraction(raw)
        if positive and number <= 0:
            raise ValueError
        return number
    except (ValueError, ZeroDivisionError):
        raise ValueError(f'Invalid {field}: {raw!r}') from None


def duration_quarters(el: ET.Element, divisions: Fraction) -> Fraction:
    raw = value(el, 'duration')
    if not raw and child(el, 'grace') is not None:
        return Fraction(0)
    return decimal_fraction(raw, 'duration', positive=True) / divisions


def parse_pitch(n: ET.Element) -> str | None:
    p = child(n, 'pitch')
    if p is None:
        return None
    step, octave = value(p, 'step'), value(p, 'octave')
    if step not in 'ABCDEFG' or len(step) != 1 or not re.fullmatch('[0-9]', octave):
        raise ValueError('Invalid MusicXML pitch step or octave')
    alter = decimal_fraction(value(p, 'alter', '0'), 'alter')
    if alter.denominator == 1 and abs(alter) <= 4:
        acc = '#' * alter.numerator if alter > 0 else 'b' * (-alter.numerator)
    else:
        acc = '[' + ('+' if alter > 0 else '') + ratio(alter) + ']'  # e.g. [+1/2]
    return f'{step}{acc}{octave}'


def parse_direction(node: ET.Element) -> list[str]:
    out = []
    for typ in children(node, 'direction-type'):
        for elt in typ:
            k = tag(elt.tag)
            if k == 'dynamics':
                out.extend('dyn:' + (''.join(e.itertext()).strip()
                                    if tag(e.tag) == 'other-dynamics' else tag(e.tag))
                           for e in elt)
            elif k in ('words', 'rehearsal', 'segno', 'coda'):
                txt = ''.join(elt.itertext()).strip()
                if txt:
                    out.append(k + ':' + txt)
            elif k == 'wedge':
                out.append('wedge:' + elt.get('type', ''))
            elif k == 'metronome':
                beat = value(elt, 'beat-unit')
                beat += '.' * len(children(elt, 'beat-unit-dot'))
                per = value(elt, 'per-minute')
                if beat or per:
                    out.append('metronome:' + beat + '=' + per)
    for sound in children(node, 'sound'):
        if sound.get('tempo'):
            out.append('tempo:' + sound.get('tempo', ''))
    return out


def parse_notations(note: ET.Element) -> list[str]:
    out = []
    for notations in children(note, 'notations'):
        for node in notations:
            k = tag(node.tag)
            if k == 'articulations':
                out.extend(tag(a.tag) for a in node)
            elif k == 'ornaments':
                out.extend('orn:' + tag(a.tag) for a in node)
            elif k == 'technical':
                out.extend('tech:' + tag(a.tag) for a in node)
            elif k in ('slur', 'tied', 'fermata', 'arpeggiate'):
                typ = node.get('type', '')
                out.append(k + (':' + typ if typ else ''))
    return out


def xml_to_index(path: Path) -> dict:
    raw = read_musicxml(path)
    root = ET.fromstring(raw)
    if tag(root.tag) != 'score-partwise':
        raise ValueError(f'Only score-partwise MusicXML is supported, got {tag(root.tag)}')
    plist = child(root, 'part-list')
    names: dict[str, dict] = {}
    for part in children(plist, 'score-part'):
        pid = part.get('id', '')
        if not pid or pid in names:
            raise ValueError('Missing or duplicate MusicXML part ID')
        names[pid] = {'id': pid, 'name': value(part, 'part-name') or pid,
                      'abbreviation': value(part, 'part-abbreviation')}
    work = child(root, 'work')
    movement_title = value(root, 'movement-title')
    header = {'title': value(work, 'work-title') or movement_title,
              'movement_title': movement_title}
    parts_out = []
    all_events = []
    stats = Counter({'notes': 0, 'rests': 0, 'directions': 0})
    seen_parts = set()
    for part in children(root, 'part'):
        pid = part.get('id', '')
        if pid not in names or pid in seen_parts:
            raise ValueError('Unknown or duplicate MusicXML part ID: ' + pid)
        seen_parts.add(pid)
        meta = names[pid]
        divisions = Fraction(1)
        measure_ct = 0
        measure_labels = []
        for measure in children(part, 'measure'):
            measure_ct += 1
            label = measure.get('number') or str(measure_ct)
            measure_labels.append(label)
            q = Fraction(0)
            last_note_q = Fraction(0)
            for elt in measure:
                kind = tag(elt.tag)
                if kind == 'attributes':
                    d = value(elt, 'divisions')
                    if child(elt, 'divisions') is not None:
                        divisions = decimal_fraction(d, 'divisions', positive=True)
                    for time in children(elt, 'time'):
                        beats = children(time, 'beats')
                        types = children(time, 'beat-type')
                        if child(time, 'senza-misura') is not None:
                            signature = 'senza-misura'
                        elif beats and len(beats) == len(types):
                            signature = '+'.join((a.text or '').strip() + '/' +
                                                 (b.text or '').strip()
                                                 for a, b in zip(beats, types))
                        else:
                            raise ValueError('Invalid MusicXML time signature')
                        event = {'p': pid, 'm': label, 'q': ratio(q),
                                 't': 'time', 'x': signature}
                        if time.get('number'):
                            event['s'] = time.get('number')
                        all_events.append(event)
                    for key in children(elt, 'key'):
                        content = {'fifths': value(key, 'fifths'), 'mode': value(key, 'mode')}
                        steps, alters = children(key, 'key-step'), children(key, 'key-alter')
                        if steps or alters:
                            if len(steps) != len(alters):
                                raise ValueError('Invalid nontraditional MusicXML key')
                            content['steps'] = [{'step': (a.text or '').strip(),
                                                 'alter': ratio(decimal_fraction(
                                                     (b.text or '').strip(), 'key-alter'))}
                                                for a, b in zip(steps, alters)]
                        event = {'p': pid, 'm': label, 'q': ratio(q), 't': 'key', 'x': content}
                        if key.get('number'):
                            event['s'] = key.get('number')
                        all_events.append(event)
                elif kind == 'backup':
                    q -= duration_quarters(elt, divisions)
                    if q < 0:
                        raise ValueError('MusicXML backup moves before the start of a measure')
                elif kind == 'forward':
                    q += duration_quarters(elt, divisions)
                elif kind == 'direction':
                    raw_offset = value(elt, 'offset')
                    direction_q = q + decimal_fraction(raw_offset, 'offset') / divisions if raw_offset else q
                    for label_value in parse_direction(elt):
                        event = {'p': pid, 'm': label, 'q': ratio(direction_q),
                                 't': 'direction', 'x': label_value}
                        staff = value(elt, 'staff')
                        if staff:
                            event['s'] = staff
                        all_events.append(event)
                        stats['directions'] += 1
                elif kind == 'note':
                    is_chord = child(elt, 'chord') is not None
                    is_grace = child(elt, 'grace') is not None
                    onset = last_note_q if is_chord else q
                    if not is_chord:
                        last_note_q = q
                    dur = duration_quarters(elt, divisions)
                    is_rest = child(elt, 'rest') is not None
                    event = {'p': pid, 'm': label, 'q': ratio(onset),
                             't': 'rest' if is_rest else 'note',
                             'd': ratio(dur)}
                    if not is_rest:
                        pitch = parse_pitch(elt)
                        if pitch is not None:
                            event['pitch'] = pitch
                        unpitched = child(elt, 'unpitched')
                        if unpitched is not None:
                            event['unpitched'] = True
                        elif pitch is None:
                            raise ValueError('MusicXML note has neither pitch, unpitched nor rest')
                    voice = value(elt, 'voice')
                    staff = value(elt, 'staff')
                    if voice:
                        event['v'] = voice
                    if staff:
                        event['s'] = staff
                    if is_chord:
                        event['chord'] = True
                    if is_grace:
                        event['grace'] = True
                    ann = parse_notations(elt)
                    if ann:
                        event['art'] = ann
                    ties = [t.get('type', '') for t in children(elt, 'tie') if t.get('type')]
                    if ties:
                        event['tie'] = ties
                    lyric = [''.join(''.join(t.itertext()) for t in ly
                                     if tag(t.tag) in ('text', 'elision')).strip()
                             for ly in children(elt, 'lyric')]
                    if lyric:
                        event['lyric'] = lyric
                    all_events.append(event)
                    stats['rests' if is_rest else 'notes'] += 1
                    if not is_chord and not is_grace:
                        q += dur
        parts_out.append({**meta, 'measures': measure_ct, 'measure_labels': measure_labels})
    if seen_parts != set(names):
        raise ValueError('MusicXML part-list and score parts do not match')
    if not parts_out:
        raise ValueError('MusicXML contains no score parts')
    return {'schema': SCHEMA_VERSION, 'source': 'musicxml', 'filename': path.name,
            'sha256': file_sha256(path), 'xml_sha256': hashlib.sha256(raw).hexdigest(), 'header': header,
            'parts': parts_out, 'stats': dict(stats), 'events': all_events}


def load_index(path: Path) -> dict:
    if path.suffix.lower() == '.json':
        if path.stat().st_size > MAX_SOURCE:
            raise ValueError('Index too large; MAX_SOURCE exceeded')
        doc = json.loads(path.read_text(encoding='utf-8'))
        validate_index(doc)
        return doc
    return xml_to_index(path)


def validate_index(doc: object) -> None:
    message = 'Not a compatible Dorico Reader index JSON'
    if (not isinstance(doc, dict) or type(doc.get('schema')) is not int
            or doc['schema'] != SCHEMA_VERSION or doc.get('source') != 'musicxml'
            or not isinstance(doc.get('filename'), str)
            or not isinstance(doc.get('parts'), list) or not isinstance(doc.get('events'), list)
            or not isinstance(doc.get('header'), dict) or not isinstance(doc.get('stats'), dict)):
        raise ValueError(message)
    ids = set()
    for part in doc['parts']:
        if (not isinstance(part, dict) or not isinstance(part.get('id'), str) or not part['id']
                or part['id'] in ids or not isinstance(part.get('name'), str)
                or type(part.get('measures')) is not int or part['measures'] < 0):
            raise ValueError(message + ': invalid part')
        labels = part.get('measure_labels')
        if labels is not None and (not isinstance(labels, list)
                                   or len(labels) != part['measures']
                                   or any(not isinstance(m, str) for m in labels)):
            raise ValueError(message + ': invalid measure labels')
        ids.add(part['id'])
    for evt in doc['events']:
        if (not isinstance(evt, dict) or not isinstance(evt.get('p'), str) or evt['p'] not in ids
                or not isinstance(evt.get('m'), str) or not isinstance(evt.get('t'), str)
                or evt['t'] not in ('note', 'rest', 'direction', 'time', 'key')
                or not isinstance(evt.get('q'), str)):
            raise ValueError(message + ': invalid event')
        try:
            Fraction(evt['q'])
            if (evt['t'] in ('note', 'rest')
                    and (not isinstance(evt.get('d'), str) or Fraction(evt['d']) < 0)):
                raise ValueError
        except (ValueError, ZeroDivisionError):
            raise ValueError(message + ': invalid event timing') from None
        for field in ('pitch', 'v', 's'):
            if field in evt and not isinstance(evt[field], str):
                raise ValueError(message + ': invalid ' + field)


def part_ids(index: dict, query: str | None) -> set[str]:
    if not query:
        return {p['id'] for p in index['parts']}
    exact_id = [p for p in index['parts'] if p['id'] == query]
    if len(exact_id) == 1:
        return {exact_id[0]['id']}
    folded_id = [p for p in index['parts'] if p['id'].casefold() == query.casefold()]
    if len(folded_id) == 1:
        return {folded_id[0]['id']}
    if len(folded_id) > 1:
        raise ValueError('Ambiguous part ID. Use exact case: ' + ', '.join(p['id'] for p in folded_id))
    exact = [p for p in index['parts'] if p['name'].casefold() == query.casefold()]
    if len(exact) == 1:
        return {exact[0]['id']}
    substring = [p for p in index['parts'] if query.casefold() in p['name'].casefold()]
    if len(substring) == 1:
        return {substring[0]['id']}
    if not exact and not substring:
        raise ValueError(f'Part not found: {query}. Available: ' +
                         ', '.join(p['name'] for p in index['parts']))
    raise ValueError('Ambiguous part selection. Choose exact name or ID: ' +
                     ', '.join(f"{p['name']} ({p['id']})" for p in (exact or substring)))


def in_range(m: str, frm: int | None, to: int | None) -> bool:
    if frm is None and to is None:
        return True
    try:
        curr = int(m)
        return (frm is None or curr >= frm) and (to is None or curr <= to)
    except ValueError:
        # Use --measure for exact non-numeric labels.
        return False


def filter_events(index: dict, args: argparse.Namespace) -> dict:
    ids = part_ids(index, args.part)
    events = []
    for evt in index['events']:
        if evt['p'] not in ids:
            continue
        if args.measure is not None and evt['m'] != args.measure:
            continue
        if not in_range(evt['m'], args.start, args.end):
            continue
        if args.type and evt['t'] != args.type:
            continue
        if args.pitch and evt.get('pitch', '').casefold() != args.pitch.casefold():
            continue
        if args.text and args.text.casefold() not in json.dumps(evt, ensure_ascii=False).casefold():
            continue
        events.append(evt)
    count = len(events)
    events = events[args.offset:args.offset + args.limit]
    if args.fields:
        flds = [f.strip() for f in args.fields.split(',') if f.strip()]
        unknown = set(flds) - {'p','m','q','t','d','pitch','v','s','chord','grace','art','tie','lyric','x','unpitched'}
        if unknown:
            raise ValueError('Invalid --fields: ' + ', '.join(sorted(unknown)))
        events = [{k: e[k] for k in flds if k in e} for e in events]
    return {'total': count, 'offset': args.offset, 'returned': len(events),
            'truncated': args.offset + len(events) < count, 'events': events}


def part_statistics(index: dict, match: str | None, offset: int, limit: int) -> dict:
    ids = part_ids(index, match)
    counts = {p['id']: Counter() for p in index['parts'] if p['id'] in ids}
    active = {pid: set() for pid in counts}
    for evt in index['events']:
        pid = evt['p']
        if pid not in counts:
            continue
        if evt['t'] in ('note', 'rest', 'direction'):
            counts[pid][evt['t']] += 1
        if evt['t'] == 'note':
            active[pid].add(evt['m'])
    items = [{'id': p['id'], 'name': p['name'], 'measures': p['measures'],
              'notes': counts[p['id']]['note'], 'rests': counts[p['id']]['rest'],
              'directions': counts[p['id']]['direction'],
              'active_measures': len(active[p['id']])}
             for p in index['parts'] if p['id'] in ids]
    return native_list(items, None, limit, offset)


def measure_activity(index: dict, args: argparse.Namespace) -> dict:
    ids = part_ids(index, args.part)
    name_by_id = {p['id']: p['name'] for p in index['parts']}
    measures: dict[str, dict] = {}

    def add_measure(m: str) -> None:
        if m not in measures:
            measures[m] = {'m': m, 'notes': 0, 'rests': 0,
                           '_active': set(), '_part_counts': Counter()}

    for part in index['parts']:
        if part['id'] in ids:
            for m in part.get('measure_labels', []):
                if in_range(m, args.start, args.end) and (args.measure is None or m == args.measure):
                    add_measure(m)
    for evt in index['events']:
        if evt['p'] not in ids or not in_range(evt['m'], args.start, args.end):
            continue
        if args.measure is not None and evt['m'] != args.measure:
            continue
        m = evt['m']
        add_measure(m)
        row = measures[m]
        if evt['t'] == 'note':
            row['notes'] += 1
            row['_active'].add(evt['p'])
            row['_part_counts'][evt['p']] += 1
        elif evt['t'] == 'rest':
            row['rests'] += 1
    rows = []
    for row in measures.values():
        data = {k: row[k] for k in ('m', 'notes', 'rests')}
        data['active_parts'] = len(row['_active'])
        if args.with_parts:
            data['parts'] = [{'name': name_by_id[pid], 'notes': count}
                             for pid, count in sorted(row['_part_counts'].items(),
                                                       key=lambda x: x[0])]
        rows.append(data)
    n = len(rows)
    subset = rows[args.offset:args.offset+args.limit]
    return {'total': n, 'offset': args.offset, 'returned': len(subset),
            'truncated': args.offset+len(subset) < n, 'items': subset}


def native_list(items: list[dict], match: str | None, limit: int, offset: int) -> dict:
    found = [x for x in items if match is None or match.casefold() in
             str(x).casefold()]
    return {'total': len(found), 'offset': offset,
            'items': found[offset:offset+limit],
            'truncated': offset+limit < len(found)}


def info_compact(p: dict) -> dict:
    m = p['metadata']
    return {'source': 'dorico-package', 'title': m['title'], 'subtitle': m['subtitle'],
            'composer': m['composer'], 'version': m['lastSavedVersion'],
            'duration': m['projectDuration'], 'players': len(p['players']),
            'flows': len(p['flows']), 'native_score': p['has_native_score'],
            'note_level_supported': False}


def parser() -> argparse.ArgumentParser:
    ap = JSONArgumentParser(description='Read Dorico ZIP metadata and MusicXML scores without opening Dorico (stdlib only).')
    subs = ap.add_subparsers(dest='command', required=True)
    for command in ('info', 'players', 'flows', 'check', 'playback', 'assets',
                    'xml-info', 'index', 'events', 'statistics', 'activity'):
        p = subs.add_parser(command)
        p.add_argument('file', type=Path)
        p.add_argument('--pretty', action='store_true', help='Indent JSON (uses more tokens)')
        if command in ('players', 'flows', 'assets'):
            p.add_argument('--match', help='Case-insensitive substring filter')
            p.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
            p.add_argument('--offset', type=int, default=0)
        if command == 'index':
            p.add_argument('--out', type=Path, required=True, help='Output reusable compact JSON index')
        if command == 'statistics':
            p.add_argument('--part', help='Exact or unambiguous part name/ID')
            p.add_argument('--offset', type=int, default=0)
            p.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
        if command == 'activity':
            p.add_argument('--part', help='Exact or unambiguous part name/ID')
            p.add_argument('--measure', help='Exact measure label')
            p.add_argument('--start', type=int, help='First measure number')
            p.add_argument('--end', type=int, help='Last measure number')
            p.add_argument('--with-parts', action='store_true', help='Include per-part note counts')
            p.add_argument('--offset', type=int, default=0)
            p.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
        if command == 'events':
            p.add_argument('--part', help='Exact or unambiguous part name/ID')
            p.add_argument('--measure', help='Exact measure label')
            p.add_argument('--start', type=int, help='First measure number, inclusive')
            p.add_argument('--end', type=int, help='Last measure number, inclusive')
            p.add_argument('--type', choices=('note', 'rest', 'direction', 'time', 'key'))
            p.add_argument('--pitch', help='Exact pitch, e.g. C#4; applies to notes')
            p.add_argument('--text', help='Literal case-insensitive substring')
            p.add_argument('--fields', help='Only these keys (comma-separated)')
            p.add_argument('--offset', type=int, default=0)
            p.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    try:
        if hasattr(args, 'limit') and (not 0 <= args.limit <= 200 or args.offset < 0):
            raise ValueError('Limit must be 0..200 and offset >= 0')
        if (getattr(args, 'start', None) is not None and args.end is not None
                and args.start > args.end):
            raise ValueError('--start must be <= --end')
        path = args.file.expanduser()
        if not path.is_file():
            raise ValueError(f'File does not exist: {path}')
        if args.command in ('info', 'players', 'flows', 'playback', 'check', 'assets'):
            if path.suffix.lower() != '.dorico':
                raise ValueError(f'{args.command} expects a .dorico file')
            if args.command == 'check':
                result = project_check(path)
            elif args.command == 'playback':
                result = project_playback(path)
            else:
                project = project_data(path)
                if args.command == 'info':
                    result = info_compact(project)
                elif args.command in ('players', 'flows'):
                    result = native_list(project[args.command], args.match, args.limit, args.offset)
                else:
                    result = native_list([{'path': x} for x in project['assets']],
                                         args.match, args.limit, args.offset)
        elif args.command == 'index':
            if path.suffix.lower() not in ('.xml', '.musicxml', '.mxl'):
                raise ValueError('index requires a MusicXML export (.xml/.musicxml/.mxl), NOT a .dorico')
            destination = args.out.expanduser()
            validate_output(destination, [path])
            index = xml_to_index(path)
            write_json(destination, index)
            result = {'written': str(destination), 'source_sha256': index['sha256'],
                      'parts': len(index['parts']), 'events': len(index['events'])}
        elif args.command in ('xml-info', 'events', 'statistics', 'activity'):
            if path.suffix.lower() not in ('.xml', '.musicxml', '.mxl', '.json'):
                raise ValueError('Requires MusicXML or an index JSON, not a native .dorico')
            index = load_index(path)
            if args.command == 'xml-info':
                result = {k: index[k] for k in ('source', 'filename', 'header', 'parts', 'stats')}
            else:
                if args.command == 'events':
                    result = filter_events(index, args)
                elif args.command == 'statistics':
                    result = part_statistics(index, args.part, args.offset, args.limit)
                else:
                    result = measure_activity(index, args)
        else:
            ap.error('Unknown command')
        emit_json(result, pretty=args.pretty)
        if args.command == 'check' and not result['valid_container']:
            return 1
        return 0
    except INPUT_ERRORS as e:
        emit_json({'error': str(e)}, error=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
