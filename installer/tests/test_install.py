import importlib.util, tempfile, pathlib, sys, unittest, os
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
if __name__ == '__main__':
 unittest.main()
