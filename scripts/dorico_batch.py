#!/usr/bin/env python3
"""Scan many Dorico projects; produce one compact JSON line per project."""
from pathlib import Path

from dorico_reader import (
    INPUT_ERRORS,
    JSONArgumentParser,
    emit_json,
    info_compact,
    project_data,
)


def main():
    ap = JSONArgumentParser(description='List metadata summaries of many .dorico files as JSONL.')
    ap.add_argument('directory', type=Path)
    ap.add_argument('--recursive', action='store_true')
    ap.add_argument('--limit', type=int, default=100)
    args = ap.parse_args()
    if args.limit < 1 or args.limit > 1000:
        ap.error('--limit must be 1..1000')
    try:
        args.directory = args.directory.expanduser()
        if not args.directory.is_dir():
            ap.error('Not a directory')
        glob = args.directory.rglob if args.recursive else args.directory.glob
        files = sorted(f for f in glob('*') if f.is_file() and f.suffix.lower() == '.dorico')[:args.limit]
    except INPUT_ERRORS as e:
        emit_json({'error': str(e)}, error=True)
        return 2
    failed = False
    for file in files:
        try:
            summary = info_compact(project_data(file))
            summary['file'] = str(file)
        except INPUT_ERRORS as e:
            summary = {'file': str(file), 'error': str(e)}
            failed = True
        emit_json(summary)
    return 1 if failed else 0

if __name__ == '__main__':
    raise SystemExit(main())
