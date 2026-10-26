import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
sys.dont_write_bytecode = True
import storage_inspector as s


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory(dir='/private/tmp', prefix='storage-skill-tests-')
        cls._old_base = s.BASE
        s.BASE = Path(cls._temp.name)
        s.build_worker()

    @classmethod
    def tearDownClass(cls):
        s.BASE = cls._old_base
        cls._temp.cleanup()

class StorageTests(FixtureCase):
    def worker(self, path):
        r=subprocess.run([str(s.WORKER),'scan',s.canonical(str(path)),'10'],capture_output=True,text=True)
        events=[json.loads(line) for line in r.stdout.splitlines()]
        return r,events

    def test_root_scope_rejects_traversal_flags_and_arbitrary_commands(self):
        for path in ['/','/Volumes/backup','/Users/../private','/Users/./sample','relative']:
            with self.subTest(path=path),self.assertRaises(ValueError):s.canonical(path)
        for command in [['/bin/sh','scan',s.DATA,'20'],[str(s.WORKER),'scan',s.DATA,'999'],
                        [str(s.WORKER),'delete',s.DATA,'20']]:
            with self.assertRaises(ValueError):s.trusted_worker(command)

    def test_native_hardlinks_sparse_file_and_symlink_are_measured_correctly(self):
        with tempfile.TemporaryDirectory(dir=s.BASE,prefix='fixture-') as name:
            root=Path(name);f=root/'one.bin';f.write_bytes(os.urandom(1024*1024))
            os.link(f,root/'same-inode.bin')
            (root/'outside-link').symlink_to('/Applications',target_is_directory=True)
            sparse=root/'sparse.bin'
            with sparse.open('wb') as stream:stream.truncate(512*1024**2)
            r,events=self.worker(root)
            self.assertEqual(r.returncode,0,r.stderr)
            done=events[-1];self.assertTrue(done['complete'])
            expected=sum(p.lstat().st_blocks*512 for p in [root,f,root/'outside-link',sparse])
            self.assertEqual(done['allocated_bytes'],expected)
            self.assertEqual(done['logical_bytes'],f.stat().st_size+sparse.stat().st_size)
            self.assertEqual(sum(e['event']=='hardlink' for e in events),1)
            self.assertFalse(any('/Applications/' in e.get('path','') for e in events))
            large=next(e for e in events if e['event']=='file' and e['path'].endswith('sparse.bin'))
            self.assertEqual(large['allocated_bytes'],0)

    def test_symlink_ancestor_cannot_redirect_admin_metadata_scan(self):
        with tempfile.TemporaryDirectory(dir=s.BASE,prefix='fixture-') as name:
            root=Path(name);(root/'link').symlink_to('/Library',target_is_directory=True)
            r,_=self.worker(root/'link/Caches')
            self.assertEqual(r.returncode,64)

    def test_denied_directory_is_an_explicit_gap(self):
        with tempfile.TemporaryDirectory(dir=s.BASE,prefix='fixture-') as name:
            root=Path(name);denied=root/'denied';denied.mkdir();denied.chmod(0)
            try:r,events=self.worker(root)
            finally:denied.chmod(0o700)
            self.assertEqual(r.returncode,1)
            self.assertFalse(events[-1]['complete'])
            self.assertTrue(any(e['event']=='error' and e['path'].endswith('/denied') for e in events))

    def test_cross_job_hardlinks_are_not_added_twice(self):
        shared={'event':'hardlink','path':s.DATA+'/a/shared','device':'1','inode':'8','allocated_bytes':1000}
        results=[{'path':s.DATA+'/'+name,'elevated':False,'summary':{'allocated_bytes':1000},
                  'events':[dict(shared,path=s.DATA+'/'+name+'/shared')],'complete':True} for name in ['a','b']]
        d=s.enrich(results,[],{'data_used_bytes':1500},s.DATA)
        self.assertEqual(d['accounting']['allocated_reference_bytes'],1000)
        self.assertEqual(d['accounting']['data_volume_difference_bytes'],500)
        root=next(x for x in d['directories'] if x['path']==s.DATA)
        self.assertEqual(root['allocated_bytes'],1000)

    def test_timeout_progress_is_a_lower_bound_and_never_marked_complete(self):
        r={'path':s.DATA+'/a','display_path':'/a','returncode':124,'seconds':10,'stderr':'','raw_file':'x',
           'summary':{'allocated_bytes':123},'events':[],'elevated':False,'complete':False}
        d=s.enrich([r],[],{'data_used_bytes':1000},s.DATA)
        self.assertEqual(d['accounting']['allocated_reference_bytes'],123)
        self.assertFalse(d['coverage']['all_jobs_complete'])
        self.assertFalse(d['directories'][0]['scan_complete'])

    def test_cloud_gap_is_preserved_on_partition_ancestors(self):
        result={'path':s.DATA+'/a','elevated':False,'complete':True,
                'summary':{'allocated_bytes':512,'skipped_cloud_directories':1},
                'events':[{'event':'cloud_directory','path':s.DATA+'/a/cloud','allocated_bytes':512}]}
        d=s.enrich([result],[],{},s.DATA)
        self.assertTrue(d['coverage']['all_local_jobs_complete'])
        self.assertFalse(d['coverage']['all_jobs_complete'])
        for row in d['directories']:
            self.assertFalse(row['scan_complete'])
            self.assertTrue(row['local_scan_complete'])
            self.assertEqual(row['skipped_cloud_directories'],1)

    def test_query_filters_and_limits_but_retains_gap_counts(self):
        with tempfile.TemporaryDirectory(dir=s.BASE,prefix='fixture-') as name:
            result={'path':s.DATA+'/a','elevated':False,'complete':True,'summary':{'allocated_bytes':0},'events':[]}
            data=s.enrich([result],[],{},s.DATA)
            for i in range(3):
                data['candidates'].append({'path':s.DATA+'/cache/'+str(i),'display_path':'/cache/'+str(i),
                    'allocated_bytes':100,'policy':'review_cache'})
            data['candidates'].append({'path':s.DATA+'/build','allocated_bytes':1000,'policy':'review_build'})
            data['errors']=[{'path':s.DATA+'/restricted/'+str(i),'message':'denied'} for i in range(4)]
            s.save(Path(name)/'analysis.json',data)
            q=s.query(name,limit=1,policy='review_cache')
            self.assertEqual(len(q['candidates']),1)
            self.assertEqual(q['candidate_totals']['review_cache']['allocated_bytes'],300)
            self.assertNotIn('review_build',q['candidate_totals'])
            self.assertEqual(q['counts_in_path']['errors'],4)
            self.assertEqual(len(q['errors']),1)
            self.assertTrue(q['evidence_file'].endswith('/analysis.json'))

    def test_candidates_preserve_unconfigured_paths_and_user_data(self):
        self.assertEqual(s.classify('/Users/sample/Unknown/target')[2], 'keep')
        self.assertEqual(s.classify('/Users/sample/Library/Application Support/Sample/history')[2], 'keep')

    def test_report_escapes_untrusted_filenames(self):
        page=s.report({'path':'</script><script>alert(1)</script>'})
        self.assertNotIn('</script><script>alert(1)',page)
        self.assertIn('\\u003c/script>',page)



from unittest.mock import patch
import hashlib
import importlib.util
import sys


class PortableStorageTests(FixtureCase):
    def test_disk_inventory_discovers_actual_physical_stores(self):
        seen = []
        def fake(argv, output, name):
            seen.append(argv)
            if name == 'apfs':
                return {'Containers': [{'ContainerReference': 'disk42', 'CapacityFree': 40,
                        'CapacityCeiling': 100, 'PhysicalStores': [{'DeviceIdentifier': 'disk7s3'}],
                        'Volumes': [{'Name': 'Data', 'CapacityInUse': 50}]}]}
            if name == 'data':
                return {'APFSContainerReference': 'disk42', 'CapacityInUse': 50}
            if name == 'physical-0':
                return {'TotalSize': 100}
            return {}
        with patch.object(s, 'run_plist', side_effect=fake):
            result = s.disk_inventory(s.BASE)
        self.assertEqual(result['physical_stores'][0]['device'], 'disk7s3')
        self.assertIn(['/usr/sbin/diskutil', 'info', '-plist', 'disk7s3'], seen)
        self.assertEqual(result['physical_bytes'], 100)

    def test_profile_protection_precedes_project_rules(self):
        saved = s.PROFILE
        try:
            s.PROFILE = {'project_roots': ['/Users/sample/Projects'],
                         'protected_paths': ['/Users/sample/Projects/active'], 'log_roots': []}
            self.assertEqual(s.classify('/Users/sample/Projects/active/target')[2], 'keep')
            self.assertEqual(s.classify('/Users/sample/Projects/other/target')[2], 'review_build')
            self.assertEqual(s.classify('/Users/sample/Unconfirmed/target')[2], 'keep')
            self.assertEqual(s.classify('/Users/sample/Projects/other/.git/objects')[2], 'keep')
        finally:
            s.PROFILE = saved

    def test_state_is_outside_skill_private_and_not_symlinked(self):
        with self.assertRaises(ValueError):
            s.prepare_state(s.SKILL / 'private')
        state = s.BASE / 'private-state'
        self.assertEqual(s.prepare_state(state).stat().st_mode & 0o777, 0o700)
        linked = s.BASE / 'linked-state'
        linked.symlink_to(state)
        try:
            with self.assertRaises(ValueError):
                s.prepare_state(linked)
        finally:
            linked.unlink()

    def test_list_single_file_has_explicit_completion(self):
        with tempfile.TemporaryDirectory(dir=s.BASE) as name:
            path = Path(name) / 'file'; path.write_bytes(b'hello')
            result = subprocess.run([str(s.WORKER), 'list', s.canonical(str(path)), '3'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout.splitlines()[-1])['complete'])

    def test_unicode_and_control_char_filename_produces_valid_json(self):
        with tempfile.TemporaryDirectory(dir=s.BASE) as name:
            unusual = Path(name) / 'name-中文-"\n<&>.txt'
            unusual.write_bytes(b'fixture')
            result = subprocess.run([str(s.WORKER), 'list', s.canonical(name), '3'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
            self.assertTrue(any(e.get('path') == s.canonical(str(unusual)) for e in events))

    def test_native_rejects_trailing_dot_component(self):
        result = subprocess.run([str(s.WORKER), 'scan', s.canonical(str(s.BASE)) + '/.', '3'], capture_output=True)
        self.assertEqual(result.returncode, 64)

    def test_budget_exhaustion_retains_unstarted_roots_as_gaps(self):
        scope = s.canonical(str(s.BASE))
        listing = {'path': scope, 'events': [{'event': 'entry', 'path': scope + '/first'},
                    {'event': 'entry', 'path': scope + '/second'}], 'complete': True}
        with patch.object(s, 'disk_inventory', return_value={'collected_at': s.now(), 'free_bytes': None}), \
             patch.object(s, 'worker_call', return_value=listing), \
             patch.object(s.time, 'monotonic', side_effect=[0, 10, 10]):
            output, data = s.collect(str(s.BASE), seconds=1, budget=1)
        self.assertEqual(data['coverage']['job_count'], 2)
        self.assertFalse(data['coverage']['all_jobs_complete'])
        self.assertEqual(len(data['coverage']['incomplete_jobs']), 2)
        self.assertIn('未取得', (output / 'summary.txt').read_text())

    def test_mount_boundary_is_not_full_coverage(self):
        result = {'path': s.DATA + '/sample', 'elevated': False, 'complete': True,
                  'summary': {'allocated_bytes': 0},
                  'events': [{'event': 'mount', 'path': s.DATA + '/sample/external'}]}
        data = s.enrich([result], [], {}, s.DATA)
        self.assertFalse(data['coverage']['all_jobs_complete'])
        self.assertEqual(data['coverage']['skipped_mount_count'], 1)

    def test_file_job_contributes_bytes_without_becoming_a_directory(self):
        root = s.DATA + '/sample'
        result = {'path': root + '/small-file', 'root_kind': 'file', 'elevated': False, 'complete': True,
                  'summary': {'allocated_bytes': 4096}, 'events': []}
        data = s.enrich([result], [], {}, root)
        self.assertEqual(data['accounting']['allocated_reference_bytes'], 4096)
        self.assertFalse(any(row['path'].endswith('/small-file') for row in data['directories']))

    def test_cancellation_saves_partial_evidence_and_marks_coverage_incomplete(self):
        scope = s.canonical(str(s.BASE))
        listing = {'path': scope, 'events': [{'event': 'entry', 'path': scope + '/first'},
                    {'event': 'entry', 'path': scope + '/second'}], 'complete': True}
        first = {'path': scope + '/first', 'elevated': False, 'complete': True,
                 'summary': {'allocated_bytes': 4096}, 'events': []}
        with patch.object(s, 'disk_inventory', return_value={'collected_at': s.now(), 'free_bytes': None}), \
             patch.object(s, 'worker_call', side_effect=[listing, first, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                s.collect(str(s.BASE), seconds=1, budget=3)
        pointer = json.loads((s.BASE / 'latest-storage.json').read_text())
        data = json.loads((Path(pointer['report']) / 'analysis.json').read_text())
        self.assertTrue(data['cancelled'])
        self.assertFalse(data['coverage']['all_jobs_complete'])
        self.assertEqual(data['accounting']['allocated_reference_bytes'], 4096)
        self.assertEqual(data['coverage']['unstarted_jobs'], [scope + '/second'])

    def test_local_scaffold_plans_and_scans_without_writing_to_skill(self):
        with tempfile.TemporaryDirectory(dir=s.BASE) as name:
            root = Path(name)
            local = s.make_local(root / 'local')
            target = root / 'target'; target.mkdir(); (target / 'file').write_bytes(b'fixture')
            profile = json.loads((local / 'profile.json').read_text())
            self.assertEqual(profile['targets'], [])
            with self.assertRaises(ValueError):
                s.make_local(local)
            profile['targets'] = [str(target)]
            (local / 'profile.json').write_text(json.dumps(profile))
            before = {str(p.relative_to(s.SKILL)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in s.SKILL.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
            command = [sys.executable, str(local / 'investigate_local.py')]
            plan = subprocess.run(command + ['plan', '--skill-dir', str(s.SKILL)], capture_output=True, text=True)
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertEqual(json.loads(plan.stdout)['targets'], [str(target)])
            scan = subprocess.run(command + ['scan', '--skill-dir', str(s.SKILL), '--seconds', '3', '--budget', '10'], capture_output=True, text=True)
            self.assertEqual(scan.returncode, 0, scan.stderr)
            pointer = json.loads((local / 'state/latest-storage.json').read_text())
            self.assertTrue((Path(pointer['report']) / 'report.html').is_file())
            after = {str(p.relative_to(s.SKILL)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in s.SKILL.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
