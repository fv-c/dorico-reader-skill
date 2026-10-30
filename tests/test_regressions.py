"""Regression and CLI integration tests using generated, anonymous score fixtures."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_reader import PREPARE, ROOT, SAMPLE_XML, SCRIPT, run_script

BATCH = ROOT / 'scripts/dorico_batch.py'
spec = importlib.util.spec_from_file_location('reader', SCRIPT)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)

NOTE = '<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>'


def native_file(path, *, extra=None, metadata=None):
    entries = {
        'score.dtn': b'\x04\x00\x00\x00' + b'\x00' * 8,
        'scorelibrary.dtn': b'STUB',
        'META-INF/container.xml': '<container/>',
        'supplementary_data/scoreinfo/scoreinfo.xml': metadata or (
            '<Score><title>Example</title><composer>Example composer</composer>'
            '<Players><Player><name>Part</name></Player></Players>'
            '<Flows><Flow><title>Movement</title></Flow></Flows></Score>'),
    }
    entries.update(extra or {})
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def corrupt_deflate(path, name, data):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
        info = archive.getinfo(name)
    raw = bytearray(path.read_bytes())
    raw[info.header_offset + 30 + len(name)] = 255  # Invalid DEFLATE block type.
    path.write_bytes(raw)


class TestRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.xml = self.path / 'score.musicxml'
        self.xml.write_text(SAMPLE_XML, encoding='utf-8')
        self.native = native_file(self.path / 'example.dorico')

    def score(self, body, *, attributes='', extra_measures=''):
        self.xml.write_text(
            '<score-partwise><part-list><score-part id="P1"><part-name>Part</part-name>'
            '</score-part></part-list><part id="P1"><measure number="1">'
            + attributes + body + '</measure>' + extra_measures + '</part></score-partwise>',
            encoding='utf-8')
        return self.xml

    def assert_cli_error(self, script, *args):
        proc, data = run_script(script, *args, success=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, '')
        self.assertNotIn('Traceback', proc.stderr)
        self.assertIsInstance(data['error'], str)
        return data

    def test_native_queries_and_pagination(self):
        for command, field, expected in (('players', 'name', 'Part'),
                                         ('flows', 'title', 'Movement')):
            with self.subTest(command=command):
                _, result = run_script(SCRIPT, command, self.native, '--match', expected.lower())
                self.assertEqual(result['items'][0][field], expected)
                _, empty = run_script(SCRIPT, command, self.native, '--offset', 1)
                self.assertEqual(empty['items'], [])
        _, assets = run_script(SCRIPT, 'assets', self.native)
        self.assertEqual(assets['total'], 0)

    def test_native_preview_and_mixer_inventory(self):
        native_file(self.native, extra={
            'supplementary_data/preview/page.pdf': b'%PDF-fixture',
            'supplementary_data/vstaudioengine/enginedata': (
                b'prefix<mixer><instruments><instrument name="Example VST" slot="2">'
                b'<instrument-channels><instrument-return-channel/>'
                b'<instrument-return-channel/></instrument-channels></instrument>'
                b'</instruments></mixer>suffix'),
        })
        _, result = run_script(SCRIPT, 'playback', self.native)
        self.assertEqual(result['plugins'], [{'name': 'Example VST', 'slot': '2', 'return_channels': 2}])
        _, assets = run_script(SCRIPT, 'assets', self.native, '--match', 'pdf')
        self.assertEqual(assets['items'][0]['path'], 'supplementary_data/preview/page.pdf')
        _, check = run_script(SCRIPT, 'check', self.native)
        self.assertEqual(check['warnings'], [])

    def test_missing_and_unavailable_mixer(self):
        _, result = run_script(SCRIPT, 'playback', self.native)
        self.assertEqual(result['plugins'], [])
        self.assertIn('No engine', result['warning'])
        native_file(self.native, extra={'supplementary_data/vstaudioengine/enginedata': b'binary'})
        _, result = run_script(SCRIPT, 'playback', self.native)
        self.assertIn('unavailable', result['warning'])

    def test_invalid_native_check_has_failure_status(self):
        self.native.write_bytes(b'not a ZIP')
        proc = subprocess.run([sys.executable, str(SCRIPT), 'check', str(self.native)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, '')
        self.assertFalse(json.loads(proc.stdout)['valid_container'])

    def test_native_check_rejects_bad_metadata_and_unsafe_entries(self):
        for name in ('../escape', '/absolute', 'C:/drive', '..\\escape'):
            with self.subTest(name=name):
                native_file(self.native, extra={name: b'fixture'}, metadata='<NotScore/>')
                result = reader.project_check(self.native)
                self.assertFalse(result['valid_container'])
                self.assertTrue(any('Unsafe' in e for e in result['errors']))
                self.assertTrue(any('Unexpected root' in e for e in result['errors']))

    def test_native_check_detects_duplicate_entries(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.native, 'a') as archive:
                archive.writestr('score.dtn', b'duplicate')
        self.assertIn('Duplicate ZIP entry names', reader.project_check(self.native)['errors'])

    def test_corrupt_deflate_streams_return_diagnostics(self):
        mxl = self.path / 'corrupt.mxl'
        corrupt_deflate(mxl, 'score.xml', SAMPLE_XML)
        self.assert_cli_error(SCRIPT, 'xml-info', mxl)
        corrupt_deflate(self.native, 'supplementary_data/scoreinfo/scoreinfo.xml', '<Score/>')
        self.assert_cli_error(PREPARE, self.native, '--out', self.path / 'sidecar')
        self.assertFalse(reader.project_check(self.native)['valid_container'])
        proc = subprocess.run([sys.executable, str(BATCH), str(self.path)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, '')
        self.assertIn('error', json.loads(proc.stdout))

    def test_native_check_detects_missing_members_and_truncated_header(self):
        with zipfile.ZipFile(self.native, 'w') as archive:
            archive.writestr('score.dtn', b'bad')
        errors = reader.project_check(self.native)['errors']
        self.assertIn('score.dtn header truncated', errors)
        self.assertIn('Missing: scorelibrary.dtn', errors)

    def test_prepare_bad_archive_and_xml_return_json_errors(self):
        self.native.write_bytes(b'not a ZIP')
        self.assert_cli_error(PREPARE, self.native, '--out', self.path / 'sidecar')
        native_file(self.native, metadata='<Score>')
        self.assert_cli_error(PREPARE, self.native, '--out', self.path / 'sidecar')
        native_file(self.native)
        self.xml.write_text('<score-partwise>')
        self.assert_cli_error(PREPARE, self.native, '--musicxml', self.xml,
                              '--out', self.path / 'sidecar')
        self.assertFalse((self.path / 'sidecar').exists())

    def test_decimal_divisions_durations_offsets_and_cursor(self):
        note = NOTE.replace('<duration>1</duration>', '<duration>2.5</duration>')
        self.score(note + '<direction><direction-type><words>late</words></direction-type>'
                   '<offset>-1.25</offset></direction><backup><duration>2.5</duration></backup>'
                   '<forward><duration>1.25</duration></forward>' + note,
                   attributes='<attributes><divisions>2.5</divisions></attributes>')
        _, result = run_script(SCRIPT, 'events', self.xml)
        self.assertEqual([e['q'] for e in result['events']], ['0', '1/2', '1/2'])
        self.assertEqual(result['events'][0]['d'], '1')
        self.assertEqual(result['events'][2]['d'], '1')

    def test_invalid_rhythm_is_reported_instead_of_zero(self):
        cases = [('<attributes><divisions>0</divisions></attributes>', NOTE),
                 ('<attributes><divisions>bad</divisions></attributes>', NOTE),
                 ('', NOTE.replace('<duration>1</duration>', '')),
                 ('', NOTE.replace('<duration>1</duration>', '<duration>-1</duration>')),
                 ('', NOTE.replace('<duration>1</duration>', '<duration>bad</duration>')),
                 ('', '<backup><duration>1</duration></backup>' + NOTE),
                 ('', '<direction><offset>bad</offset></direction>' + NOTE)]
        for attributes, body in cases:
            with self.subTest(attributes=attributes, body=body):
                self.score(body, attributes=attributes)
                self.assert_cli_error(SCRIPT, 'events', self.xml)

    def test_invalid_pitch_is_reported(self):
        for replacement in ('<step>H</step><octave>4</octave>',
                             '<step>C</step><alter>bad</alter><octave>4</octave>',
                             '<step>C</step>'):
            with self.subTest(replacement=replacement):
                self.score(NOTE.replace('<step>C</step><octave>4</octave>', replacement))
                self.assert_cli_error(SCRIPT, 'events', self.xml)

    def test_grace_unpitched_and_lyrics(self):
        self.score('<note><grace/><pitch><step>D</step><octave>4</octave></pitch></note>'
                   + NOTE.replace('</note>', '<lyric><syllabic>single</syllabic>'
                                  '<text>la</text></lyric></note>')
                   + '<note><unpitched><display-step>C</display-step>'
                   '<display-octave>5</display-octave></unpitched><duration>1</duration></note>')
        _, result = run_script(SCRIPT, 'events', self.xml)
        grace, note, percussion = result['events']
        self.assertTrue(grace['grace'])
        self.assertEqual(grace['d'], '0')
        self.assertEqual(note['q'], '0')
        self.assertEqual(note['lyric'], ['la'])
        self.assertTrue(percussion['unpitched'])

    def test_compound_and_staff_time_and_key(self):
        self.score(NOTE, attributes=(
            '<attributes><time number="1"><beats>2</beats><beat-type>4</beat-type>'
            '<beats>3</beats><beat-type>8</beat-type></time>'
            '<time number="2"><senza-misura/></time>'
            '<key number="2"><key-step>F</key-step><key-alter>0.5</key-alter></key></attributes>'))
        _, result = run_script(SCRIPT, 'events', self.xml)
        first, second, key = result['events'][:3]
        self.assertEqual((first['x'], first['s']), ('2/4+3/8', '1'))
        self.assertEqual((second['x'], second['s']), ('senza-misura', '2'))
        self.assertEqual(key['x']['steps'], [{'step': 'F', 'alter': '1/2'}])

    def test_dotted_metronome_and_text_dynamics(self):
        self.score('<direction><direction-type><metronome><beat-unit>quarter</beat-unit>'
                   '<beat-unit-dot/><per-minute>72</per-minute></metronome>'
                   '<dynamics><other-dynamics>quiet</other-dynamics></dynamics>'
                   '</direction-type></direction>')
        _, result = run_script(SCRIPT, 'events', self.xml)
        self.assertEqual([e['x'] for e in result['events']], ['metronome:quarter.=72', 'dyn:quiet'])

    def test_empty_measures_are_present_in_activity_and_cached_index(self):
        self.score(NOTE, extra_measures='<measure number="2"/>')
        _, result = run_script(SCRIPT, 'activity', self.xml)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['items'][1], {'m': '2', 'notes': 0, 'rests': 0, 'active_parts': 0})
        index = self.path / 'index.json'
        run_script(SCRIPT, 'index', self.xml, '--out', index)
        _, cached = run_script(SCRIPT, 'activity', index)
        self.assertEqual(cached, result)

    def test_zero_statistics_are_explicit(self):
        self.score('')
        _, result = run_script(SCRIPT, 'xml-info', self.xml)
        self.assertEqual(result['stats'], {'notes': 0, 'rests': 0, 'directions': 0})

    def test_measure_ranges_pickups_and_invalid_bounds(self):
        self.score(NOTE, extra_measures='<measure number="pickup">' + NOTE + '</measure>')
        _, result = run_script(SCRIPT, 'events', self.xml, '--measure', 'pickup')
        self.assertEqual(result['total'], 1)
        _, result = run_script(SCRIPT, 'events', self.xml, '--start', 1, '--end', 1)
        self.assertEqual(result['total'], 1)
        self.assert_cli_error(SCRIPT, 'events', self.xml, '--start', 'pickup')
        self.assert_cli_error(SCRIPT, 'activity', self.xml, '--start', 2, '--end', 1)

    def test_pagination_validation_applies_to_every_list(self):
        for command in ('players', 'flows', 'assets', 'events', 'statistics', 'activity'):
            source = self.native if command in ('players', 'flows', 'assets') else self.xml
            for option, value in (('--limit', -1), ('--limit', 201), ('--offset', -1)):
                with self.subTest(command=command, option=option, value=value):
                    self.assert_cli_error(SCRIPT, command, source, option, value)
            _, result = run_script(SCRIPT, command, source, '--limit', 0)
            self.assertEqual(result.get('events', result.get('items')), [])

    def test_invalid_fields_and_unknown_part(self):
        self.assert_cli_error(SCRIPT, 'events', self.xml, '--fields', 'typo')
        self.assert_cli_error(SCRIPT, 'events', self.xml, '--part', 'Missing')

    def test_part_id_takes_priority_over_colliding_display_name(self):
        self.xml.write_text(SAMPLE_XML.replace('<part-name>Violin II</part-name>',
                                               '<part-name>P1</part-name>'))
        _, result = run_script(SCRIPT, 'events', self.xml, '--part', 'P1', '--type', 'note')
        self.assertEqual(result['total'], 5)

    def test_part_ids_preserve_case_when_ids_differ_only_by_case(self):
        self.xml.write_text(SAMPLE_XML.replace('id="P2"', 'id="p1"'))
        for query, expected in (('P1', 5), ('p1', 1)):
            with self.subTest(query=query):
                _, result = run_script(SCRIPT, 'events', self.xml, '--part', query, '--type', 'note')
                self.assertEqual(result['total'], expected)

    def test_duplicate_and_unlisted_parts_fail(self):
        for text in (SAMPLE_XML.replace('<part id="P2">', '<part id="P1">'),
                     SAMPLE_XML.replace('<part id="P2">', '<part id="P3">'),
                     SAMPLE_XML.replace('<score-part id="P2">', '<score-part id="P1">')):
            with self.subTest(text=text[:40]):
                self.xml.write_text(text)
                self.assert_cli_error(SCRIPT, 'xml-info', self.xml)

    def test_unsupported_timewise_is_explicit(self):
        self.xml.write_text('<score-timewise/>')
        result = self.assert_cli_error(SCRIPT, 'xml-info', self.xml)
        self.assertIn('score-partwise', result['error'])

    def test_malformed_index_shapes_return_json_errors(self):
        good = reader.xml_to_index(self.xml)
        bad_event = {**good, 'events': [{'p': 'P1', 'm': '1', 't': 'note', 'q': '0', 'd': None}]}
        cases = [[], None, {'schema': 1, 'parts': [], 'events': []},
                 {**good, 'parts': [None]}, {**good, 'parts': {}},
                 {**good, 'events': [None]}, {**good, 'events': [{'p': []}]},
                 {**good, 'events': [{**good['events'][0], 'q': '1/0'}]}, bad_event,
                 {**good, 'events': [{**good['events'][2], 'pitch': []}]},
                 {**good, 'parts': [{**good['parts'][0], 'measure_labels': [1]}]}]
        index = self.path / 'invalid.json'
        for case in cases:
            with self.subTest(case=repr(case)[:60]):
                index.write_text(json.dumps(case))
                self.assert_cli_error(SCRIPT, 'events', index)

    def test_mxl_hash_matches_container_bytes(self):
        mxl = self.path / 'score.mxl'
        with zipfile.ZipFile(mxl, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('META-INF/container.xml',
                             '<container><rootfiles><rootfile full-path="score.xml"/></rootfiles></container>')
            archive.writestr('score.xml', SAMPLE_XML)
        result = reader.xml_to_index(mxl)
        self.assertEqual(result['sha256'], hashlib.sha256(mxl.read_bytes()).hexdigest())
        self.assertEqual(result['xml_sha256'], hashlib.sha256(SAMPLE_XML.encode()).hexdigest())

    def test_mxl_without_manifest_and_ambiguous_members(self):
        mxl = self.path / 'score.mxl'
        with zipfile.ZipFile(mxl, 'w') as archive:
            archive.writestr('score.xml', SAMPLE_XML)
        _, result = run_script(SCRIPT, 'xml-info', mxl)
        self.assertEqual(result['stats']['notes'], 6)
        with zipfile.ZipFile(mxl, 'a') as archive:
            archive.writestr('another.xml', SAMPLE_XML)
        self.assert_cli_error(SCRIPT, 'xml-info', mxl)

    def test_mxl_missing_and_unsafe_rootfiles(self):
        mxl = self.path / 'score.mxl'
        for target in ('missing.xml', '../score.xml', '/score.xml', 'C:/score.xml', '..\\score.xml'):
            with self.subTest(target=target):
                with zipfile.ZipFile(mxl, 'w') as archive:
                    archive.writestr('META-INF/container.xml',
                                     '<container><rootfiles><rootfile full-path="' + target + '"/></rootfiles></container>')
                    archive.writestr('score.xml', SAMPLE_XML)
                self.assert_cli_error(SCRIPT, 'xml-info', mxl)

    def test_mxl_duplicate_members_fail(self):
        mxl = self.path / 'score.mxl'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(mxl, 'w') as archive:
                archive.writestr('score.xml', SAMPLE_XML)
                archive.writestr('score.xml', SAMPLE_XML)
        self.assert_cli_error(SCRIPT, 'xml-info', mxl)

    def test_mxl_invalid_container_manifest_fails(self):
        mxl = self.path / 'score.mxl'
        for manifest in ('<not-container/>', '<container><rootfiles/></container>',
                         ('<container><rootfiles><rootfile full-path="score.xml" media-type="application/pdf"/>'
                          '</rootfiles></container>')):
            with self.subTest(manifest=manifest):
                with zipfile.ZipFile(mxl, 'w') as archive:
                    archive.writestr('META-INF/container.xml', manifest)
                    archive.writestr('score.xml', SAMPLE_XML)
                self.assert_cli_error(SCRIPT, 'xml-info', mxl)

    def test_source_and_member_size_limits(self):
        with patch.object(reader, 'MAX_SOURCE', 10), self.assertRaisesRegex(ValueError, 'too large'):
            reader.read_musicxml(self.xml)
        with zipfile.ZipFile(self.native) as archive, self.assertRaisesRegex(ValueError, 'too large'):
            reader.read_zip_member(archive, 'score.dtn', max_bytes=1)

    def test_index_cannot_overwrite_source_or_use_score_extension(self):
        original = self.xml.read_bytes()
        self.assert_cli_error(SCRIPT, 'index', self.xml, '--out', self.xml)
        self.assert_cli_error(SCRIPT, 'index', self.xml, '--out', self.native)
        self.assertEqual(self.xml.read_bytes(), original)
        self.assertTrue(reader.project_check(self.native)['valid_container'])

    def test_index_rejects_symlink_and_hardlink_to_input(self):
        original = self.xml.read_bytes()
        linked = self.path / 'alias.json'
        os.link(self.xml, linked)
        self.assert_cli_error(SCRIPT, 'index', self.xml, '--out', linked)
        linked.unlink()
        try:
            linked.symlink_to(self.xml)
        except OSError:
            return  # Symlink creation needs extra privileges on some Windows systems.
        self.assert_cli_error(SCRIPT, 'index', self.xml, '--out', linked)
        self.assertEqual(self.xml.read_bytes(), original)

    def test_failed_index_preserves_previous_output(self):
        index = self.path / 'index.json'
        index.write_text('previous output')
        self.xml.write_text('<score-partwise>')
        self.assert_cli_error(SCRIPT, 'index', self.xml, '--out', index)
        self.assertEqual(index.read_text(), 'previous output')

    def test_failed_atomic_write_keeps_previous_file_and_cleans_temporary(self):
        index = self.path / 'index.json'
        index.write_text('previous output')
        original_files = set(self.path.iterdir())
        with (patch.object(reader.os, 'replace', side_effect=OSError('write failure')),
              self.assertRaises(OSError)):
            reader.write_json(index, {'test': True})
        self.assertEqual(index.read_text(), 'previous output')
        self.assertEqual(set(self.path.iterdir()), original_files)

    def test_prepare_rejects_directory_outputs_before_writing(self):
        out = self.path / 'sidecar'
        (out / 'manifest.json').mkdir(parents=True)
        self.assert_cli_error(PREPARE, self.native, '--musicxml', self.xml, '--out', out)
        self.assertFalse((out / 'music-index.json').exists())

    def test_prepare_protects_sources_from_alias_outputs(self):
        out = self.path / 'sidecar'
        out.mkdir()
        os.link(self.xml, out / 'music-index.json')
        original = self.xml.read_bytes()
        self.assert_cli_error(PREPARE, self.native, '--musicxml', self.xml, '--out', out)
        self.assertEqual(self.xml.read_bytes(), original)
        self.assertFalse((out / 'manifest.json').exists())

    def test_prepare_manifest_is_portable_and_has_provenance(self):
        out = self.path / 'sidecar'
        os.utime(self.xml, ns=(1, 1))
        _, result = run_script(PREPARE, self.native, '--musicxml', self.xml, '--out', out)
        manifest = json.loads((out / 'manifest.json').read_text())
        self.assertTrue(any('predates' in w for w in result['warnings']))
        self.assertEqual(manifest['project_fingerprint']['sha256'],
                         hashlib.sha256(self.native.read_bytes()).hexdigest())
        self.assertEqual(manifest['musicxml']['sha256'], hashlib.sha256(self.xml.read_bytes()).hexdigest())
        self.assertNotIn(str(self.path), (out / 'manifest.json').read_text())
        self.assertNotIn(str(self.path), (out / 'music-index.json').read_text())

    def test_prepare_without_musicxml(self):
        out = self.path / 'sidecar'
        _, result = run_script(PREPARE, self.native, '--out', out)
        self.assertIsNone(result['music_index'])
        self.assertFalse((out / 'music-index.json').exists())
        self.assertIn('note-level reading unavailable', result['warnings'][0])

    def test_batch_recursion_limits_and_bad_project_status(self):
        nested = self.path / 'nested'
        nested.mkdir()
        native_file(nested / 'other.DORICO')
        (nested / 'z-broken.dorico').write_bytes(b'not zip')
        proc = subprocess.run([sys.executable, str(BATCH), str(self.path), '--recursive'],
                              capture_output=True, text=True, check=False)
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(rows), 3)
        self.assertIn('error', rows[-1])
        self.assertEqual(proc.stderr, '')
        proc, result = run_script(BATCH, self.path, '--recursive', '--limit', 1)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(result['title'], 'Example')

    def test_batch_empty_directory_and_invalid_arguments(self):
        empty = self.path / 'empty'
        empty.mkdir()
        proc = subprocess.run([sys.executable, str(BATCH), str(empty)],
                              capture_output=True, text=True, check=False)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, '', ''))
        self.assert_cli_error(BATCH, self.path, '--limit', 0)
        self.assert_cli_error(BATCH, self.path / 'missing')

    def test_tilde_expansion_in_all_entrypoints(self):
        # A temporary HOME confines shell-independent expansion to generated fixtures.
        env = {**os.environ, 'HOME': str(self.path), 'USERPROFILE': str(self.path)}
        commands = [(SCRIPT, 'index', '~/score.musicxml', '--out', '~/index.json'),
                    (PREPARE, '~/example.dorico', '--out', '~/sidecar'),
                    (BATCH, '~')]
        for script, *args in commands:
            with self.subTest(script=script.name):
                proc = subprocess.run([sys.executable, str(script), *args], env=env,
                                      capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                json.loads(proc.stdout)

    def test_json_output_supports_ascii_only_terminal(self):
        native_file(self.native, metadata='<Score><title>Etude \u2014 \u03b1</title></Score>')
        proc = subprocess.run([sys.executable, str(SCRIPT), 'info', str(self.native)],
                              env={**os.environ, 'PYTHONIOENCODING': 'ascii'},
                              capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['title'], 'Etude \u2014 \u03b1')

    def test_help_and_argument_errors_for_all_entrypoints(self):
        for script in (SCRIPT, PREPARE, BATCH):
            with self.subTest(script=script.name):
                proc = subprocess.run([sys.executable, str(script), '--help'],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0)
                self.assertIn('usage:', proc.stdout)
                self.assert_cli_error(script)

    def test_missing_files_and_wrong_format(self):
        self.assert_cli_error(SCRIPT, 'info', self.path / 'missing.dorico')
        self.assert_cli_error(SCRIPT, 'info', self.xml)
        self.assert_cli_error(SCRIPT, 'index', self.native, '--out', self.path / 'index.json')
        self.assert_cli_error(SCRIPT, 'events', self.native)


if __name__ == '__main__':
    unittest.main()
