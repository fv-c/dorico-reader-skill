#!/usr/bin/env python3
"""Prepare a portable sidecar manifest and optional MusicXML note index (read-only source)."""
from __future__ import annotations

from pathlib import Path

from dorico_reader import (
    INPUT_ERRORS,
    JSONArgumentParser,
    emit_json,
    file_sha256,
    info_compact,
    project_data,
    project_playback,
    validate_output,
    write_json,
    xml_to_index,
)


def fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {'filename': path.name, 'size': stat.st_size, 'sha256': file_sha256(path)}


def main() -> int:
    ap = JSONArgumentParser(description='Prepare a small Dorico manifest and optional reusable score index.')
    ap.add_argument('project', type=Path)
    ap.add_argument('--musicxml', type=Path, help='Full-score MusicXML export (optional)')
    ap.add_argument('--out', type=Path, required=True, help='Destination directory')
    args = ap.parse_args()
    try:
        args.project = args.project.expanduser()
        args.musicxml = args.musicxml.expanduser() if args.musicxml else None
        args.out = args.out.expanduser()
        sources = [args.project] + ([args.musicxml] if args.musicxml else [])
        outputs = [args.out / 'manifest.json', args.out / 'music-index.json']
        for output in outputs:
            validate_output(output, sources)
        if outputs[0].exists() and outputs[1].exists() and outputs[0].samefile(outputs[1]):
            raise ValueError('Manifest and index destinations must be distinct files')
        if args.project.suffix.lower() != '.dorico':
            raise ValueError('Project must end in .dorico')
        data = project_data(args.project)
        manifest = {'schema': 1, 'summary': info_compact(data),
                    'players': data['players'], 'flows': data['flows'],
                    'playback': project_playback(args.project),
                    'project_fingerprint': fingerprint(args.project),
                    'musicxml': None, 'warnings': []}
        score_index = None
        if args.musicxml:
            if args.musicxml.suffix.lower() not in ('.musicxml', '.xml', '.mxl'):
                raise ValueError('--musicxml must be a valid MusicXML file')
            score_index = xml_to_index(args.musicxml)
            manifest['musicxml'] = {'file': args.musicxml.name,
                                    'sha256': score_index['sha256'],
                                    'parts': len(score_index['parts']),
                                    'events': len(score_index['events']),
                                    'index_file': 'music-index.json'}
            if args.musicxml.stat().st_mtime_ns < args.project.stat().st_mtime_ns:
                manifest['warnings'].append('MusicXML export predates the current .dorico file; possibly stale.')
            if len(data['flows']) > 1:
                manifest['warnings'].append('Multiple Dorico flows: verify which flow(s) the MusicXML export covers.')
            if len(score_index['parts']) != len(data['players']):
                manifest['warnings'].append('MusicXML part count differs from Dorico player count (may be legitimate); inspect selection.')
        else:
            manifest['warnings'].append('No MusicXML export supplied: note-level reading unavailable.')
        if score_index is not None:
            write_json(outputs[1], score_index)
        write_json(outputs[0], manifest)
        emit_json({'manifest': str(args.out/'manifest.json'),
                          'music_index': str(args.out/'music-index.json') if score_index else None,
                          'warnings': manifest['warnings']})
        return 0
    except INPUT_ERRORS as e:
        emit_json({'error': str(e)}, error=True)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
