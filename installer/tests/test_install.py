import importlib.util, tempfile, pathlib, sys, unittest, os, subprocess, json
spec=importlib.util.spec_from_file_location('installer', pathlib.Path(__file__).resolve().parents[1]/'install.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
source=pathlib.Path(__file__).resolve().parents[2]/'ai-verse-social-media'
class InstallTests(unittest.TestCase):
 def test_install_repeat_and_update_preserve_workspace(self):
  with tempfile.TemporaryDirectory() as d:
   base=pathlib.Path(d);root=base/'skills';work=base/'private';work.mkdir();(work/'history').write_text('keep')
   target=m.install(source,root,work,sys.executable)
   self.assertTrue((target/'src/social_video_ops/engine.py').is_file())
   self.assertTrue((root/'ai-verse-social-onboard/SKILL.md').is_file())
   m.install(source,root,work,sys.executable)
   (target/'__pycache__').mkdir();(target/'__pycache__/a.pyc').write_bytes(b'cache')
   m.install(source,root,work,sys.executable)
   with self.assertRaises(ValueError):m.install(source,root,base/'new-private',sys.executable)
   m.install(source,root,base/'new-private',sys.executable,True)
   self.assertEqual((work/'history').read_text(),'keep');self.assertTrue(list((root/'.ai-verse-backups').iterdir()))
   (target/'SKILL.md').write_text('custom')
   with self.assertRaises(ValueError):m.install(source,root,work,sys.executable,True)
   self.assertEqual((target/'SKILL.md').read_text(),'custom')
 def test_foreign_conflict_is_atomic(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d)/'skills';conflict=root/'ai-verse-social-onboard';conflict.mkdir(parents=True);(conflict/'mine').write_text('keep')
   with self.assertRaises(ValueError):m.install(source,root,pathlib.Path(d)/'workspace',sys.executable)
   self.assertFalse((root/m.NAME).exists());self.assertEqual((conflict/'mine').read_text(),'keep')
 def test_symlink_refused(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d)/'skills';root.mkdir();(root/m.NAME).symlink_to(source,target_is_directory=True)
   with self.assertRaises(ValueError):m.install(source,root,pathlib.Path(d)/'workspace',sys.executable)
 def test_host_paths(self):
  for host in m.HOSTS:self.assertTrue(str(m.host_root(host)).endswith('/skills'))
  old=os.environ.get('HERMES_HOME');os.environ['HERMES_HOME']='/tmp/member-profile'
  try:self.assertEqual(m.host_root('hermes'),pathlib.Path('/tmp/member-profile/skills'))
  finally:
   if old is None:os.environ.pop('HERMES_HOME')
   else:os.environ['HERMES_HOME']=old
class RevisionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.base=pathlib.Path(self.temp.name);self.checkout=self.base/'repo';self.checkout.mkdir()
  self.source=self.checkout/m.NAME;self.source.mkdir();(self.source/'scripts').mkdir()
  (self.source/'SKILL.md').write_text('skill');(self.source/'scripts/social.py').write_text('pass')
  self.git('init','-q');self.git('remote','add','origin','https://github.com/aiverse-filmmakers/ai-verse-social.git')
  self.git('add','.');self.git('-c','user.name=Offline Test','-c','user.email=offline@example.invalid','commit','-qm','fixture')
 def git(self,*args):
  return subprocess.run(['git','-C',str(self.checkout),*args],capture_output=True,text=True,check=True).stdout.strip()
 def metadata(self):
  target=m.install(self.source,self.base/'skills',self.base/'private',sys.executable)
  return json.loads((target/'LOCAL-SETUP.json').read_text())
 def test_clean_official_checkout_records_exact_revision(self):
  self.assertEqual(self.metadata()['revision'],self.git('rev-parse','HEAD'))
 def test_modified_checkout_does_not_claim_commit_bytes(self):
  (self.source/'SKILL.md').write_text('locally changed')
  self.assertNotIn('revision',self.metadata())
 def test_untracked_skill_file_does_not_claim_commit_bytes(self):
  (self.source/'personal.md').write_text('custom')
  self.assertNotIn('revision',self.metadata())
 def test_ignored_skill_file_does_not_claim_commit_bytes(self):
  (self.checkout/'.gitignore').write_text('.env\n');(self.source/'.env').write_text('fake test data')
  self.assertNotIn('revision',self.metadata())
 def test_other_origin_cannot_claim_official_revision(self):
  self.git('remote','set-url','origin','https://github.com/example/other.git')
  self.assertNotIn('revision',self.metadata())

if __name__ == '__main__':
 unittest.main()
