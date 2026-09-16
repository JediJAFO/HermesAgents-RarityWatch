"""Offline regression tests: never invoke monitor entrypoints or senders."""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import monitor_backup as b

class BackupTests(unittest.TestCase):
    def test_semantic_schedule_hash_input(self):
        j={'script':'run_mtd_sales_monitor.py','schedule':{'kind':'cron','expr':'0 8 * * *','display':'volatile'},'enabled':True,'no_agent':True,'last_run_at':'old','deliver':'private'}
        changed=copy.deepcopy(j); changed.update(last_run_at='new',deliver='different private',failure_streak=3)
        self.assertEqual(b.schedules('sales',[j]),b.schedules('sales',[changed]))
        changed['schedule']['expr']='0 9 * * *'
        self.assertNotEqual(b.schedules('sales',[j]),b.schedules('sales',[changed]))
    def test_daily_cap(self):
        with tempfile.TemporaryDirectory() as d:
            stamp=Path(d)/'stamp'
            self.assertTrue(b.daily_allowed(stamp,'day1'))
            stamp.write_text('day1\n')
            self.assertFalse(b.daily_allowed(stamp,'day1'))
            self.assertTrue(b.daily_allowed(stamp,'day2'))
    def test_sensitive_rejection(self):
        for secret in ['0x'+'a'*40, '9'*18, 'ghp_'+'a'*30]:
            with self.assertRaises(ValueError): b.scan(secret)
    def test_snapshots_and_missing_dependency(self):
        for kind in ('exotic','sales'):
            with tempfile.TemporaryDirectory() as d:
                root=Path(d); b.snapshot(kind,root); count=b.verify(root)
                hashes={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
                b.snapshot(kind,root)
                self.assertEqual(hashes,{p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()})
                dep=root/('price-watches/exotic_whatsapp_policy.js' if kind=='exotic' else 'price-watches/run_mcfarlane_dc_sales.py')
                saved=dep.read_bytes(); dep.unlink()
                with self.assertRaisesRegex(ValueError,'Missing or changed'): b.verify(root)
                dep.write_bytes(saved); self.assertEqual(count,b.verify(root))
    def test_local_import_closure(self):
        # Resolve imports against actual sibling source modules without importing
        # runtime modules (several execute state/network work at import time).
        local={p.stem:p for group in ('scripts','price-watches') for p in (b.HOME/group).glob('*.py')}
        for kind in ('exotic','sales'):
            sources=set(b.source_paths(kind))
            for p in sources:
                if p.suffix!='.py': continue
                for node in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
                    mods=[a.name.split('.')[0] for a in node.names] if isinstance(node,ast.Import) else ([node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module else [])
                    for mod in mods:
                        if mod in local: self.assertIn(local[mod],sources,f'{p.name} missing {mod}')
        js=(b.HOME/'price-watches/run_mcfarlane_exotic_deterministic.js').read_text()
        self.assertIn('exotic_whatsapp_policy.js',js)
        self.assertIn(b.HOME/'price-watches/exotic_whatsapp_policy.js',b.source_paths('exotic'))
        self.assertIn('node_modules/playwright',js)

if __name__=='__main__': unittest.main(verbosity=2)
