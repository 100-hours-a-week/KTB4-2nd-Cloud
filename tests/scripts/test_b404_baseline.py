"""Guard the baseline against accidentally invoking the full migration runner."""
import ast
from pathlib import Path
import unittest

SCRIPT=Path(__file__).resolve().parents[2]/'scripts/run-b404-source-baseline.py'
class BaselineContract(unittest.TestCase):
    def test_no_migration_or_target_execution(self):
        tree=ast.parse(SCRIPT.read_text())
        phases=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='phase']
        forbidden={'preflight','start-consumer','enable-consumer','fence','promote','source-restore','target-snapshot','consume'}
        for call in phases:
            values={n.value for n in call.args if isinstance(n,ast.Constant) and isinstance(n.value,str)}
            self.assertFalse(values & forbidden)
            self.assertNotIn('target',values)
        self.assertNotIn('execute(case',SCRIPT.read_text())
    def test_only_transport_topics(self):
        tree=ast.parse(SCRIPT.read_text())
        body=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='topics')
        text=ast.get_source_segment(SCRIPT.read_text(),body)
        self.assertIn("'.requests',8",text)
        self.assertNotIn("'.trips'",text)
        self.assertNotIn('register(',text)
    def test_fixed_input_and_checkpoint(self):
        text=SCRIPT.read_text()
        self.assertIn("'duration':900",text)
        self.assertIn("'checkpointSeconds':1020",text)
        self.assertIn("state['startedAt']+1020",text)
        self.assertIn("'--count','140'",text)
        self.assertIn('max_poll_records=32 enable_auto_commit=false concurrency=4',text)
if __name__=='__main__':unittest.main()
