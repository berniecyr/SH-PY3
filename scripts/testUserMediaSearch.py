"""Search semantics and saved-record matching, with a temporary database."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backEnd.UserMediaDb import UserMediaDb
from backEnd.UserMediaSearch import SearchSyntaxError


class SearchChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = UserMediaDb().open(str(self.root/'db.sqlite'))
        self.photo = self.root/'photos'/'family.jpg'
        self.copy = self.root/'copies'/'family-copy.jpg'
        self.dog = self.root/'photos'/'sub'/'dog.jpg'
        self.textOnly = self.root/'photos'/'notes.jpg'
        for path, data in ((self.photo,b'family'), (self.copy,b'family'), (self.dog,b'dog'), (self.textOnly,b'notes')):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(data)
        self.db.saveResult(str(self.photo),dict(kind='image',detections=[
            dict(type='person',faceName='Bernie'), dict(type='person',faceName='Rosemary')]))
        self.db.registerContent(str(self.copy))
        self.db.saveDescriptions(str(self.photo),'Sailing; FAMILY','A blue sail with René. 100% fun_under_sun.')
        self.db.saveResult(str(self.dog),dict(kind='image',detections=[dict(type='animal',subType='dog')]))
        self.db.saveDescriptions(str(self.dog),'Beach','A black and white dog.')
        self.db.saveDescriptions(str(self.textOnly),'Notes','No detections here.')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def search(self, query, **kwargs):
        return self.db.pathsMatching(str(self.root/'photos'),query=query,recursive=True,**kwargs)

    def test_contains_casefold_and_implicit_and(self):
        self.assertEqual(self.search('sail RENÉ'),{str(self.photo)})
        self.assertEqual(self.search('CONTAINS sailing'),{str(self.photo)})
        self.assertEqual(self.search('tags CONTAINS family'),{str(self.photo)})

    def test_every_table_column_available_and_qualified(self):
        fields = {r['field'] for r in self.db.getSearchFields()}
        self.assertTrue({'files.uid', 'detections.uid', 'detections.fileUid',
                         'file_locations.mtimeNs', 'file_locations.contentHash'} <= fields)
        for field in fields:
            self.db.compileSearch(field + ':"test"')
        uid = self.db.getFile(str(self.photo))['uid']
        self.assertEqual(self.search('files.uid:eq:' + str(uid)), {str(self.photo)})
        self.assertEqual(self.search('file_locations.path:word:copies'), {str(self.photo)})
        self.assertEqual(self.search('has:file_locations.mtimeNs'),
                         {str(self.photo), str(self.dog)})

    def test_exact_person_and_tag(self):
        self.assertEqual(self.search('person:exact:bernie'), {str(self.photo)})
        self.assertEqual(self.search('person:exact:Bern'), set())
        self.assertEqual(self.search('tags:tag:FAMILY'), {str(self.photo)})
        self.assertEqual(self.search('tags:tag:FAM'), set())
        self.assertEqual(self.search('tags:exact:FAMILY'), set())

    def test_numeric_range_uses_one_detection_value(self):
        self.db._conn.execute('UPDATE detections SET conf=CASE faceName WHEN ? THEN .1 ELSE .9 END', ('Bernie',))
        self.db._conn.commit()
        self.assertEqual(self.search('detections.conf:between:"0.3,0.6"'), set())
        self.assertEqual(self.search('detections.conf:between:"0.8,1"'), {str(self.photo), str(self.dog)})
        self.assertEqual(self.search('detections.conf:lt:0.2'), {str(self.photo)})
        for query in ('ai:ge:10', 'all:ge:2', 'files.size:ge:nan',
                      'files.size:ge:inf', 'files.size:between:"10,2"', 'person:tag:Bernie'):
            with self.subTest(query=query), self.assertRaises(SearchSyntaxError): self.search(query)

    def test_whole_word_keeps_record_with_both_door_and_outdoor(self):
        self.db.saveDescriptions(str(self.photo), 'Door; outdoor', 'A front door outdoors.')
        self.db.saveDescriptions(str(self.dog), 'Outdoor; doorbell', 'Indoors and outdoors.')
        self.assertEqual(self.search('door'), {str(self.photo), str(self.dog)})
        self.assertEqual(self.search('word:door'), {str(self.photo)})
        self.assertEqual(self.search('ai:word:door'), {str(self.photo)})
        self.assertEqual(self.search('word:door AND NOT word:doorbell'), {str(self.photo)})
        self.assertEqual(self.search('word:door OR tags:beach'), {str(self.photo)})
        self.assertEqual(self.search('tags:word:"front door"'), set())
        self.assertEqual(self.search('ai:word:"front door"'), {str(self.photo)})

    def test_whole_word_boundaries_case_unicode_and_literal_punctuation(self):
        self.db.saveDescriptions(str(self.photo), 'DOOR_frame; René; a+b', 'doorway doors door2')
        self.assertEqual(self.search('tags:word:door'), {str(self.photo)})
        self.assertEqual(self.search('ai:word:door'), set())
        self.assertEqual(self.search('word:RENÉ'), {str(self.photo)})
        self.assertEqual(self.search('tags:word:"a+b"'), {str(self.photo)})
        self.assertEqual(self.search('tags:word:"a.b"'), set())

    def test_whole_word_detection_and_duplicate_paths(self):
        self.assertEqual(self.search('person:word:Bernie'), {str(self.photo)})
        self.assertEqual(self.search('person:word:Bern'), set())
        self.assertEqual(self.search('filename:word:copy'), {str(self.photo)})
        self.assertEqual(self.search('filename:word:copy', allFolders=True),
                         {str(self.photo), str(self.copy)})
        self.assertEqual(self.search('word:dog', types=['animal']), {str(self.dog)})

    def test_quotes_mean_whole_word_and_explicit_contains_still_works(self):
        self.db.saveDescriptions(str(self.photo), 'word', 'outdoor')
        self.assertEqual(self.search('word'), {str(self.photo)})
        self.assertEqual(self.search('ai:"door"'), set())
        self.assertEqual(self.search('"door"'), set())
        self.assertEqual(self.search('ai CONTAINS "door"'), {str(self.photo)})
        self.assertEqual(self.search('CONTAINS "door"'), {str(self.photo)})
        self.assertEqual(self.search('ai:word:door'), set())
        self.db.saveDescriptions(str(self.photo), 'outdoor; doorbell', 'A DOOR, outdoors.')
        self.assertEqual(self.search('"door"'), {str(self.photo)})
        self.assertEqual(self.search('ai:"door" AND NOT "indoors"'), {str(self.photo)})

    def test_quoted_phrase_boundaries_and_reserved_words(self):
        self.db.saveDescriptions(str(self.photo), 'test', 'A front doorway and outdoor scene')
        self.assertEqual(self.search('"front door"'), set())
        self.assertEqual(self.search('ai CONTAINS "front door"'), {str(self.photo)})
        self.assertEqual(self.search('ai:"and"'), {str(self.photo), str(self.dog)})
        self.db.saveDescriptions(str(self.photo), 'test', 'A front door, outside')
        self.assertEqual(self.search('"front door"'), {str(self.photo)})

    def test_invalid_whole_word_queries(self):
        for query in ('word:', 'word:""', 'ai:word:', 'has:word:door', 'bogus:word:door'):
            with self.subTest(query=query), self.assertRaises(SearchSyntaxError):
                self.search(query)

    def test_phrases_groups_and_precedence(self):
        self.assertEqual(self.search('person:Bernie AND (tags:beach OR ai:"blue sail")'),{str(self.photo)})
        self.assertEqual(self.search('sailing OR beach AND NOT filename:dog'),{str(self.photo)})
        self.assertEqual(self.search('NOT (tags:beach OR person:Bernie)'),{str(self.textOnly)})

    def test_independent_detection_terms(self):
        self.assertEqual(self.search('person:Bernie AND person:Rosemary'),{str(self.photo)})

    def test_location_scope_and_aliases(self):
        self.assertEqual(self.search('filename:copy'),{str(self.photo)})
        self.assertEqual(self.search('filename:copy',allFolders=True),{str(self.photo),str(self.copy)})
        self.assertEqual(self.search('path:copies'),{str(self.photo)})
        self.assertEqual(self.search('beach'),{str(self.dog)})

    def test_description_only_without_detections(self):
        self.assertEqual(self.search('ai:"no detections"'),{str(self.textOnly)})

    def test_existing_filters_combine_with_text(self):
        self.assertEqual(self.search('blue',faceName='Bernie'),{str(self.photo)})
        self.assertEqual(self.search('blue',types=['animal']),set())
        self.assertEqual(self.search('white',types=['animal']),{str(self.dog)})

    def test_literals_do_not_become_sql_wildcards(self):
        self.assertEqual(self.search('100%'),{str(self.photo)})
        self.assertEqual(self.search('fun_under'),{str(self.photo)})
        self.assertEqual(self.search('"x\' OR 1=1 --"'),set())

    def test_bad_queries_raise_instead_of_widening_search(self):
        for query in ('beach AND', 'OR beach', '(beach', 'beach)', '"open', 'unknown:beach', 'tags:', '""'):
            with self.subTest(query=query), self.assertRaises(SearchSyntaxError):
                self.search(query)

    def test_global_empty_query_and_old_no_filter_behavior(self):
        self.assertEqual(len(self.search('',allFolders=True)),4)
        self.assertIsNone(self.search(''))

    def test_nonblank_tags_and_blank_ai(self):
        for blank in ('', '  \t\r\n'):
            self.db.saveDescriptions(str(self.textOnly),'Notes',blank)
            self.assertEqual(self.search('has:tags AND NOT has:ai'),{str(self.textOnly)})
            self.assertEqual(self.search('has:description_tags AND empty:description_ai'),{str(self.textOnly)})
        self.db._conn.execute('UPDATE files SET description_ai=NULL WHERE path=?',(str(self.textOnly),))
        self.db._conn.commit()
        self.assertEqual(self.search('has:tags AND empty:ai'),{str(self.textOnly)})
        self.db.saveDescriptions(str(self.textOnly),' \t\n','')
        self.assertEqual(self.search('has:tags AND empty:ai'),set())

    def test_boolean_query_with_spaces_and_quoted_phrase(self):
        self.assertEqual(self.search('person:Bernie AND ai:blue'), {str(self.photo)})
        self.assertEqual(self.search('person:Bernie AND ai:"blue sail"'), {str(self.photo)})

    def test_has_person_across_detection_rows_and_invalid_field(self):
        self.assertEqual(self.search('has:person'),{str(self.photo)})
        self.assertEqual(self.search('empty:person'),{str(self.dog),str(self.textOnly)})
        with self.assertRaises(SearchSyntaxError):
            self.search('has:unknown_field')


if __name__ == '__main__':
    unittest.main(verbosity=2)
