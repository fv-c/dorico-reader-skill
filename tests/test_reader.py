import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/dorico_reader.py'
PREPARE = ROOT / 'scripts/dorico_prepare.py'

SAMPLE_XML = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE score-partwise  PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="4.0">
  <work><work-title>Test piece</work-title></work>
  <movement-title>First movement</movement-title>
  <part-list>
    <score-part id="P1"><part-name>Violin solo</part-name><part-abbreviation>Vln.</part-abbreviation></score-part>
    <score-part id="P2"><part-name>Violin II</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>24</divisions><key><fifths>0</fifths></key><time><beats>3</beats><beat-type>4</beat-type></time></attributes>
      <direction><direction-type><dynamics><p /></dynamics><words>dolce</words></direction-type><sound tempo="84"/></direction>
      <note><pitch><step>C</step><alter>1</alter><octave>4</octave></pitch><duration>24</duration><voice>1</voice><type>quarter</type><notations><articulations><staccato/></articulations></notations></note>
      <note><chord/><pitch><step>E</step><octave>4</octave></pitch><duration>24</duration><voice>1</voice><type>quarter</type></note>
      <note><pitch><step>F</step><alter>0.5</alter><octave>4</octave></pitch><duration>12</duration><voice>1</voice><type>eighth</type><tie type="start"/></note>
      <note><rest/><duration>36</duration><voice>1</voice><type>quarter</type><dot/></note>
      <backup><duration>72</duration></backup>
      <note><pitch><step>C</step><octave>3</octave></pitch><duration>72</duration><voice>2</voice><type>half</type><dot/></note>
    </measure>
    <measure number="2">
      <note><pitch><step>F</step><alter>0.5</alter><octave>4</octave></pitch><duration>24</duration><voice>1</voice><tie type="stop"/></note>
      <note><rest/><duration>48</duration><voice>1</voice></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1"><attributes><divisions>24</divisions><time><beats>3</beats><beat-type>4</beat-type></time></attributes>
      <note><rest/><duration>72</duration></note></measure>
    <measure number="2"><note><pitch><step>C</step><octave>3</octave></pitch><duration>72</duration></note></measure>
  </part>
</score-partwise>'''


def run_script(script: Path, *args, success=True):
    proc = subprocess.run([sys.executable, str(script), *map(str, args)],
                          text=True, capture_output=True, check=False)
    if success and proc.returncode:
        raise AssertionError(f'Command failed {proc.returncode}: {proc.stderr}')
    return proc, json.loads(proc.stdout if proc.returncode == 0 else proc.stderr)


class TestReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.xml = self.path / 'score.musicxml'
        self.xml.write_text(SAMPLE_XML, encoding='utf8')

    def test_musicxml_summary(self):
        _, r = run_script(SCRIPT, 'xml-info', self.xml)
        self.assertEqual(r['header']['title'], 'Test piece')
        self.assertEqual(len(r['parts']), 2)
        self.assertEqual(r['parts'][0]['measures'], 2)
        self.assertEqual(r['stats']['notes'], 6)  # 5 in P1 and 1 in P2
        self.assertEqual(r['stats']['rests'], 3)

    def test_note_query_chords_and_voices(self):
        _, result = run_script(SCRIPT, 'events', self.xml, '--part', 'Violin solo',
                               '--measure', '1', '--type', 'note', '--fields', 'm,q,pitch,d,v,chord,art')
        events = result['events']
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]['q'], '0')
        self.assertEqual(events[0]['pitch'], 'C#4')
        self.assertEqual(events[1]['q'], '0')
        self.assertEqual(events[1]['chord'], True)
        self.assertEqual(events[2]['pitch'], 'F[+1/2]4')
        self.assertEqual(events[2]['d'], '1/2')
        self.assertEqual(events[3]['q'], '0')  # voice 2 after backup
        self.assertEqual(events[3]['v'], '2')

    def test_directions(self):
        _, r = run_script(SCRIPT, 'events', self.xml, '--part', 'P1', '--type', 'direction')
        self.assertEqual([x['x'] for x in r['events']], ['dyn:p', 'words:dolce', 'tempo:84'])

    def test_pagination_and_filtered_text(self):
        _, r = run_script(SCRIPT, 'events', self.xml, '--part', 'P1', '--type', 'note', '--limit', '2')
        self.assertEqual(r['total'], 5)
        self.assertTrue(r['truncated'])
        self.assertEqual(len(r['events']), 2)
        _, r = run_script(SCRIPT, 'events', self.xml, '--text', 'dolce', '--limit', '10')
        self.assertEqual(r['total'], 1)

    def test_ambiguity_errors(self):
        proc, r = run_script(SCRIPT, 'events', self.xml, '--part', 'Violin', success=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('Ambiguous', r['error'])

    def test_index_and_prepare(self):
        index = self.path / 'index.json'
        run_script(SCRIPT, 'index', self.xml, '--out', index)
        _, r = run_script(SCRIPT, 'events', index, '--part', 'P1', '--pitch', 'C#4')
        self.assertEqual(r['total'], 1)
        self.assertEqual(r['events'][0]['pitch'], 'C#4')

    def test_statistics_and_activity(self):
        _, s = run_script(SCRIPT, 'statistics', self.xml)
        self.assertEqual(s['total'], 2)
        self.assertEqual(s['items'][0]['notes'], 5)
        self.assertEqual(s['items'][0]['rests'], 2)
        self.assertEqual(s['items'][0]['active_measures'], 2)
        _, a = run_script(SCRIPT, 'activity', self.xml, '--measure', '1', '--with-parts')
        self.assertEqual(a['total'], 1)
        self.assertEqual(a['items'][0]['notes'], 4)
        self.assertEqual(a['items'][0]['active_parts'], 1)
        self.assertEqual(a['items'][0]['parts'][0]['name'], 'Violin solo')

    def test_compressed_mxl(self):
        mxl = self.path / 'score.mxl'
        with zipfile.ZipFile(mxl, 'w') as z:
            z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="main.xml"/></rootfiles></container>')
            z.writestr('main.xml', SAMPLE_XML)
        _, r = run_script(SCRIPT, 'xml-info', mxl)
        self.assertEqual(r['stats']['notes'], 6)

    def test_native_zip_metadata_and_check(self):
        native = self.path / 'stub.dorico'
        with zipfile.ZipFile(native, 'w') as z:
            z.writestr('score.dtn', b'\x04\x00\x00\x00\x01\x00\x00\x00\x10\x00\x00\x00')
            z.writestr('scorelibrary.dtn', b'STUB')
            z.writestr('META-INF/container.xml', '<container/>')
            z.writestr('supplementary_data/scoreinfo/scoreinfo.xml',
                       '<Score><title>T</title><lastSavedVersion>6.2.30</lastSavedVersion>'
                       '<Players><Player><name>A</name></Player></Players>'
                       '<Flows><Flow><title>F</title></Flow></Flows></Score>')
        _, r = run_script(SCRIPT, 'info', native)
        self.assertEqual(r['title'], 'T')
        self.assertEqual(r['players'], 1)
        _, r = run_script(SCRIPT, 'check', native)
        self.assertTrue(r['valid_container'])
        out = self.path / 'sidecar'
        _, r = run_script(PREPARE, native, '--musicxml', self.xml, '--out', out)
        self.assertTrue((out/'manifest.json').exists())
        self.assertTrue((out/'music-index.json').exists())

    def test_optional_real_example(self):
        source = os.getenv('DORICO_TEST_FILE')
        if not source:
            self.skipTest('Set DORICO_TEST_FILE to test an external real project')
        _, r = run_script(SCRIPT, 'info', Path(source))
        self.assertGreaterEqual(r['players'], 1)
        _, audit = run_script(SCRIPT, 'check', Path(source))
        self.assertTrue(audit['valid_container'])


if __name__ == '__main__':
    unittest.main()
