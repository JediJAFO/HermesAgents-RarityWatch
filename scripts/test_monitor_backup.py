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
    def test_reviewed_deletion_gate_and_unrelated_work(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); repo=root/'repo'; (repo/'.git').mkdir(parents=True)
            saved=root/'private-copy'; saved.write_text('private legacy content')
            approval={'deletions':{'monitor/legacy.json':{'saved':str(saved),'sha256':hashlib.sha256(saved.read_bytes()).hexdigest()}}}
            (repo/'.git/monitor-backup-cleanup.json').write_text(json.dumps(approval))
            def git(*args):
                return ' D monitor/legacy.json' if args[0]=='status' else 'monitor/legacy.json'
            self.assertTrue(hasattr(b,'repository_gate'),'shared production/offline safety gate missing')
            self.assertEqual(b.repository_gate(repo,git,['snapshot-manifest.json']),['monitor/legacy.json'])
            def dirty(*args):
                return '?? unrelated.py\n D monitor/legacy.json' if args[0]=='status' else 'monitor/legacy.json'
            with self.assertRaises(RuntimeError): b.repository_gate(repo,dirty,['snapshot-manifest.json'])
            saved.write_text('changed')
            with self.assertRaises(RuntimeError): b.repository_gate(repo,git,['snapshot-manifest.json'])

    def test_semantic_schedule_hash_input(self):
        j={'script':'run_mtd_sales_monitor.py','schedule':{'kind':'cron','expr':'0 8 * * *','display':'volatile'},'enabled':True,'no_agent':True,'last_run_at':'old','deliver':'private'}
        changed=copy.deepcopy(j); changed.update(last_run_at='new',deliver='different private',failure_streak=3)
        self.assertEqual(b.schedules('sales',[j]),b.schedules('sales',[changed]))
        changed['schedule']['expr']='0 9 * * *'
        self.assertNotEqual(b.schedules('sales',[j]),b.schedules('sales',[changed]))
    def test_fingerprint_policy_changes_not_observation_churn(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'price-watches').mkdir()
            state={'collections':[{'contract':'private','name':'Example','last_successful_observation':'old'}], 'display_policy':{'show_item_identity':False}}
            path=root/'price-watches/mcfarlane-exotics.json'
            def digest():
                path.write_text(json.dumps(state))
                return b.fingerprint('exotic',[])
            with patch.object(b,'HOME',root),patch.object(b,'source_paths',return_value=[]):
                before=digest()
                state['collections'][0]['last_successful_observation']='new'
                self.assertEqual(before,digest())
                state['display_policy']['show_item_identity']=True
                self.assertNotEqual(before,digest())

    def test_daily_cap(self):
        with tempfile.TemporaryDirectory() as d:
            stamp=Path(d)/'stamp'
            self.assertTrue(b.daily_allowed(stamp,'day1'))
            stamp.write_text('day1\n')
            self.assertFalse(b.daily_allowed(stamp,'day1'))
            self.assertTrue(b.daily_allowed(stamp,'day2'))
    def test_private_job_ids_are_placeholders(self):
        text="job_id='"+'a'*12+"'"
        clean=b.sanitized_source(text)
        self.assertNotIn('a'*12,clean)
        self.assertIn('__PRIVATE_JOB_ID__',clean)

    def test_snapshot_rejects_unlisted_files(self):
        with tempfile.TemporaryDirectory() as d:
            b.snapshot('sales',d)
            (Path(d)/'private.txt').write_text('unexpected')
            with self.assertRaisesRegex(ValueError,'Unlisted'): b.verify(d)

    def test_inventory_cannot_expand_allowlist(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); b.snapshot('sales',root)
            extra=root/'private.txt'; extra.write_text('not approved')
            path=root/'snapshot-manifest.json'; data=json.loads(path.read_text())
            data['files']['private.txt']=hashlib.sha256(extra.read_bytes()).hexdigest()
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'allowlist'): b.verify(root)

    def test_daily_entrypoint_cap_precedes_all_subprocesses(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'backup').mkdir()
            today=datetime.now(ZoneInfo('America/New_York')).date().isoformat()
            (root/'backup/.last-successful-backup-date').write_text(today)
            with patch.dict('os.environ',{'MCFARLANE_SALES_BACKUP_REPO':d}), patch('sys.argv',['backup']), patch.object(b.subprocess,'run',side_effect=AssertionError('must not execute')):
                b.backup_main('sales')

    def test_entrypoint_no_change_does_not_commit_or_push(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); b.snapshot('sales',root)
            calls=[]
            def offline(command,**kwargs):
                calls.append(command)
                if command[0]=='git':
                    self.assertIn(command[1],('status','ls-files'))
                    return subprocess.CompletedProcess(command,0,stdout='',stderr='')
                self.assertTrue(command[1].endswith('refresh-mcfarlane-prds.py'))
                return subprocess.CompletedProcess(command,0,stdout='',stderr='')
            with patch.dict('os.environ',{'MCFARLANE_SALES_BACKUP_REPO':d}), patch('sys.argv',['backup']), patch.object(b.subprocess,'run',side_effect=offline):
                b.backup_main('sales')
            self.assertEqual([c[1] for c in calls if c[0]=='git'],['ls-files','status'])
            self.assertFalse((root/'backup').exists())

    def test_docx_private_metadata_rejected(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); b.snapshot('sales',root)
            name=b.manifest()['sales']['prd']+'.docx'; path=root/name
            doc=Document(path); doc.core_properties.author='0x'+'a'*40; doc.save(path)
            import hashlib
            inventory=root/'snapshot-manifest.json'; data=json.loads(inventory.read_text())
            data['files'][name]=hashlib.sha256(path.read_bytes()).hexdigest(); inventory.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'privacy'): b.verify(root)

    def test_sensitive_rejection(self):
        for secret in ['0x'+'a'*40, '9'*18, 'ghp_'+'a'*30]:
            with self.assertRaises(ValueError): b.scan(secret)

    def test_only_exact_loopback_cdp_endpoints_are_allowed(self):
        b.scan("const ipv4 = 'http://127.0.0.1:9222';")
        b.scan("const ipv6 = 'http://[::1]:9222';")
        for unsafe in ('http://'+'127.0.0.1:9223', 'http://'+'192.168.1.5:9222',
                       'http://'+'localhost:9222', 'https://'+'example.com'):
            with self.subTest(url=unsafe), self.assertRaisesRegex(ValueError, 'Non-allowlisted URL'):
                b.scan(unsafe)

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
