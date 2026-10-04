import importlib.util, sys, tempfile, unittest, os, sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name, path)
    mod=importlib.util.module_from_spec(spec)
    sys.modules[name]=mod
    spec.loader.exec_module(mod)
    return mod
dbmod=load('backEnd.UserMediaDb',ROOT/'backEnd/UserMediaDb.py')
imp=load('media_import',ROOT/'scripts/import.py')
enroll=load('backEnd.UserMediaFaceEnrollment',ROOT/'backEnd/UserMediaFaceEnrollment.py')

class Checks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.a=self.root/'one'/'photo.jpg'; self.a.parent.mkdir(); self.a.write_bytes(b'original bytes')
        self.b=self.root/'two'/'photo.jpg'; self.b.parent.mkdir(); self.b.write_bytes(self.a.read_bytes())
        self.db=dbmod.UserMediaDb().open(str(self.root/'db.sqlite'))
        self.result=dict(kind='image',modelSig='test',detections=[dict(type='person',faceName='Test',faceDetConf=.9)])
    def tearDown(self):
        self.db.close(); self.temp.cleanup()
    def save(self,p): return self.db.saveResult(str(p),self.result)
    def test_duplicates_tags_ai_alias_filters_and_enrollment(self):
        uid=self.save(self.a)
        self.db.saveDescriptions(str(self.a),'One; Two','Paragraph')
        self.assertEqual(self.save(self.b),uid)
        self.assertEqual(self.db.stats(),(1,1))
        self.assertEqual(self.db.getDescriptions(str(self.b))['description_ai'],'Paragraph')
        self.assertEqual(self.db.pathsMatching(str(self.root),faceName='Test',recursive=True),{str(self.a),str(self.b)})
        det=self.db.getDetections(str(self.b))[0]
        row=self.db.getFile(str(self.b))
        self.assertEqual(enroll.resolveDetection(str(self.root/'db.sqlite'),str(self.b),det['uid'],row['analyzedMs'])['path'],str(self.b))
    def test_same_name_different_bytes(self):
        self.b.write_bytes(b'different data')
        self.assertNotEqual(self.save(self.a),self.save(self.b))
        self.assertEqual(self.db.stats(),(2,2))
    def test_stale_hash_does_not_merge_changed_existing_copy(self):
        self.save(self.a)
        self.a.write_bytes(b'replaced externally')
        self.save(self.b)
        self.assertNotEqual(self.db.getFile(str(self.a))['uid'],self.db.getFile(str(self.b))['uid'])
        self.assertTrue(self.db.needsAnalysis(str(self.a),'test'))
    def test_changed_canonical_copy_detaches(self):
        self.save(self.a); self.save(self.b)
        self.a.write_bytes(b'changed file')
        self.assertTrue(self.db.needsAnalysis(str(self.a),'test'))
        self.save(self.a)
        self.assertNotEqual(self.db.getFile(str(self.a))['uid'],self.db.getFile(str(self.b))['uid'])
        self.assertEqual(self.db.stats(),(2,2))
    def test_changed_noncanonical_copy_detaches(self):
        self.save(self.a); self.save(self.b)
        self.b.write_bytes(b'changed file')
        self.save(self.b)
        self.assertEqual(self.db.stats(),(2,2))
    def test_singleton_changes_then_matches_existing(self):
        self.b.write_bytes(b'different')
        self.save(self.a); self.save(self.b)
        self.b.write_bytes(self.a.read_bytes()); self.save(self.b)
        self.assertEqual(self.db.stats(),(1,1))
    def test_description_only_before_analysis(self):
        self.db.saveDescriptions(str(self.a),'A','Text A')
        self.db.saveDescriptions(str(self.b),'B','Text B')
        self.save(self.a); self.save(self.b)
        text=self.db.getDescriptions(str(self.a))
        self.assertEqual(text['description_tags'],'A; B')
        self.assertIn('Text A',text['description_ai']); self.assertIn('Text B',text['description_ai'])
        self.assertEqual(self.db.stats(),(1,1))
    def test_legacy_records_merge_preserving_text(self):
        c=self.db._conn
        for path,text in ((self.a,'A'),(self.b,'B')):
            c.execute('INSERT INTO files(path,size,description_tags) VALUES (?,?,?)',(str(path),path.stat().st_size,text))
        c.commit(); self.db.close(); self.db.open(str(self.root/'db.sqlite'))
        self.save(self.a)
        self.assertEqual(self.db.stats(),(1,1))
        self.assertEqual(self.db.getDescriptions(str(self.b))['description_tags'],'A; B')
    def plan(self):
        rows=[{'filename':p.name,'path':str(p.parent),'tag':tag} for p,tag in ((self.a,'One'),(self.a,'Two'),(self.b,'One'))]
        ai=[dict(filename='photo.jpg',gemini='A paragraph')]
        return imp.buildPlan(rows,ai)
    def test_plan_combines_exact_duplicates_and_tags(self):
        plan=self.plan(); self.assertEqual(len(plan['records']),1)
        r=plan['records'][0]; self.assertEqual(len(r['locations']),2)
        self.assertEqual(r['tags'],'One; Two'); self.assertEqual(r['description_ai'],'A paragraph')
    def test_plan_ambiguous_filename_withholds_ai(self):
        self.b.write_bytes(b'other bytes')
        plan=self.plan(); self.assertEqual(len(plan['records']),2)
        self.assertTrue(all(not r['description_ai'] for r in plan['records']))
    def test_verified_duplicate_combines_distinct_gemini_descriptions(self):
        rows=[dict(filename=p.name,path=str(p.parent),tag='One') for p in (self.a,self.b)]
        ai=[dict(filename=self.a.name,gemini=text) for text in ('First description', 'Second\nparagraph', 'First description')]
        plan=imp.buildPlan(rows,ai)
        self.assertEqual(len(plan['records']),1)
        self.assertEqual(plan['records'][0]['description_ai'],'First description\n\nSecond\nparagraph')
        self.assertFalse(plan['issues'])
    def test_merge_descriptions_preserves_existing_and_is_repeatable(self):
        candidates=['First\n\nSecond paragraph','Another description']
        merged=imp.mergeDescriptions('Existing user text',*candidates)
        self.assertEqual(merged,'Existing user text\n\nFirst\n\nSecond paragraph\n\nAnother description')
        self.assertEqual(imp.mergeDescriptions(merged,*candidates),merged)
        self.assertEqual(imp.mergeDescriptions(candidates[1],*candidates),
                         candidates[1]+'\n\n'+candidates[0])
    def test_missing_location_withholds_ai(self):
        self.b.unlink(); plan=self.plan()
        self.assertFalse(plan['records'][0]['description_ai'])
        self.assertTrue(plan['issues'])

if __name__=='__main__': unittest.main(verbosity=2)

