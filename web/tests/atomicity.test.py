import sqlite3,unittest,pathlib,json
class Atomicity(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.db.executescript(pathlib.Path('drizzle/0000_lyrical_morlocks.sql').read_text());self.db.execute("INSERT INTO workspaces VALUES ('owner',0,'{}','now')");self.db.commit()
 def apply(self,revision,key,payload='next',owner='owner'):
  with self.db:
   a=self.db.execute('UPDATE workspaces SET payload = ?, revision = revision + 1, updated_at = ? WHERE owner = ? AND revision = ?', (payload,'now',owner,revision)).rowcount
   b=self.db.execute('INSERT INTO operations (key,owner,request_hash,response,created_at) SELECT ?,?,?,?,? WHERE changes() = 1',(key,owner,'hash','{}','now')).rowcount
  return a,b
 def test_success_persists_state_and_idempotency_record_together(self):
  self.assertEqual(self.apply(0,'one'),(1,1));self.assertEqual(self.db.execute('select revision from workspaces').fetchone()[0],1)
 def test_stale_revision_cannot_record_success(self):
  self.apply(0,'one');self.assertEqual(self.apply(0,'two','bad'),(0,0));self.assertEqual(self.db.execute('select count(*) from operations').fetchone()[0],1);self.assertEqual(self.db.execute('select payload from workspaces').fetchone()[0],'next')
 def test_duplicate_operation_rolls_back_entire_state_write(self):
  self.apply(0,'one');
  with self.assertRaises(sqlite3.IntegrityError):self.apply(1,'one','bad')
  self.assertEqual(self.db.execute('select revision,payload from workspaces').fetchone(),(1,'next'))
 def test_owner_mismatch_cannot_write(self):
  self.assertEqual(self.apply(0,'one',owner='other'),(0,0));self.assertEqual(self.db.execute('select revision from workspaces').fetchone()[0],0)
 def test_distinct_operations_from_same_revision_conflict(self):
  self.assertEqual(self.apply(0,'one'),(1,1));self.assertEqual(self.apply(0,'two'),(0,0));self.assertEqual(self.apply(1,'two'),(1,1))
if __name__=='__main__':unittest.main()
