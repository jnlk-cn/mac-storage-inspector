#!/usr/bin/env python3
"""Bounded, local, read-only storage inventory for macOS APFS startup disks."""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parent
SKILL = SOURCE.parent
HOME = Path.home()
DATA = '/System/Volumes/Data'
PREFIXES = ('/Users', '/Applications', '/Library', '/private', '/opt', '/usr', '/sw', '/cores', '/pkg')
ENV = dict(os.environ, PATH='/usr/bin:/bin:/usr/sbin:/sbin', LC_ALL='en_US.UTF-8')
BASE = HOME / 'Library/Application Support/MacStorageInspector'
WORKER = None
PROFILE = {'project_roots': [], 'protected_paths': [], 'log_roots': []}


def now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def save(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.new')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write('\n')
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def prepare_state(path):
    path = Path(path).expanduser().absolute()
    # Keep generated paths and reports out of the distributable skill.
    if path.is_symlink() or path.resolve().is_relative_to(SKILL.resolve()):
        raise ValueError('State must be outside the skill and must not be a symbolic link')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid():
        raise ValueError('State directory must belong to the current user')
    path.chmod(0o700)
    (path / '.gitignore').write_text('*\n', encoding='utf-8')
    return path.resolve()


def canonical(path):
    if not isinstance(path, str) or '\0' in path or not path.startswith('/'):
        raise ValueError('An absolute startup Data-volume path is required')
    if any(part in ('.', '..') for part in path.split('/')):
        raise ValueError('Paths must not contain . or .. components')
    path = os.path.normpath(path)
    if path == DATA or path.startswith(DATA + '/'):
        return path
    if any(path == prefix or path.startswith(prefix + '/') for prefix in PREFIXES):
        return DATA + path
    raise ValueError('Only the startup APFS Data volume is supported; external volumes are not scanned')


def display(path):
    tail = path[len(DATA):] if path.startswith(DATA + '/') else path
    return tail if any(tail == p or tail.startswith(p + '/') for p in PREFIXES) else path


def inside(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def load_profile(path):
    if not path:
        return {'project_roots': [], 'protected_paths': [], 'log_roots': []}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported local profile schema')
    result = {}
    for key in ('project_roots', 'protected_paths', 'log_roots'):
        values = data.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError('Profile path lists must contain strings')
        result[key] = [display(canonical(v.replace('${HOME}', str(HOME)))) for v in values]
    return result


def build_worker():
    global WORKER
    if sys.platform != 'darwin':
        raise ValueError('Native scanning requires macOS')
    source = SOURCE / 'space_worker.c'
    tag = hashlib.sha256(source.read_bytes() + platform.machine().encode()).hexdigest()[:20]
    cache = BASE / 'cache'
    cache.mkdir(exist_ok=True, mode=0o700)
    binary = cache / ('space-worker-' + tag)
    metadata = binary.with_suffix('.json')
    if binary.exists() and metadata.exists() and not binary.is_symlink():
        info = binary.stat()
        expected = json.loads(metadata.read_text()).get('sha256')
        if (info.st_uid == os.getuid() and not info.st_mode & 0o022 and
                os.access(binary, os.X_OK) and hashlib.sha256(binary.read_bytes()).hexdigest() == expected):
            WORKER = binary
            return binary
    compiler = subprocess.run(['/usr/bin/xcrun', '--find', 'clang'], capture_output=True, text=True, timeout=15)
    if compiler.returncode:
        raise ValueError('Install Apple Command Line Tools, then retry build; installation is never automatic')
    sdk = subprocess.run(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'], capture_output=True, text=True, timeout=15)
    if sdk.returncode or not (Path(sdk.stdout.strip()) / 'usr/include/sys/types.h').is_file():
        raise ValueError('A complete macOS SDK is required; check the selected Xcode or Command Line Tools installation')
    tmp = cache / (binary.name + '.new-' + str(os.getpid()))
    result = subprocess.run([compiler.stdout.strip(), '-isysroot', sdk.stdout.strip(), '-O2', '-Wall', '-Wextra', '-Werror',
                             str(source), '-o', str(tmp)], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('Native compiler failed: ' + result.stderr[-3000:])
    tmp.chmod(0o700)
    tmp.replace(binary)
    save(metadata, {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'architecture': platform.machine()})
    WORKER = binary
    return binary


def trusted_worker(argv):
    if (WORKER is None or len(argv) != 4 or argv[0] != str(WORKER) or argv[1] not in ('list', 'scan')
            or canonical(argv[2]) != argv[2] or not str(argv[3]).isdigit()
            or not 1 <= int(argv[3]) <= 300):
        raise ValueError('Only bounded list/scan metadata operations are permitted')
    return argv


def sudo_ready():
    return subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/true'], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=ENV, timeout=5).returncode == 0


def retain_event(event, root):
    if event.get('event') != 'directory':
        return True
    depth = len(Path(event['path']).parts) - len(Path(root).parts)
    return depth <= 2 or event.get('allocated_bytes', 0) >= 1024 ** 2


def worker_call(mode, path, seconds, elevated, raw_dir):
    argv = trusted_worker([str(WORKER), mode, canonical(path), str(seconds)])
    actual = ['/usr/bin/sudo', '-n', '--'] + argv if elevated else argv
    key = hashlib.sha256((mode + '\0' + path).encode('utf-8', 'surrogateescape')).hexdigest()[:20]
    raw, errfile = raw_dir / (key + '.jsonl'), raw_dir / (key + '.stderr.txt')
    started = time.monotonic()
    with raw.open('wb') as out, errfile.open('wb') as err:
        process = subprocess.Popen(actual, stdout=out, stderr=err, env=ENV)
        try:
            code = process.wait(timeout=seconds + 3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            code = 124
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            raise
    events, malformed = [], 0
    for line in raw.read_text(encoding='utf-8', errors='strict').splitlines():
        try:
            event = json.loads(line)
            if retain_event(event, path):
                events.append(event)
        except ValueError:
            malformed += 1
    done = next((e for e in reversed(events) if e.get('event') == 'done'), None)
    progress = next((e for e in reversed(events) if e.get('event') == 'progress'), {})
    return {'path': path, 'display_path': display(path), 'mode': mode, 'elevated': elevated,
            'collected_at': now(), 'returncode': code,
            'complete': bool(done and done.get('complete') and code == 0 and not malformed),
            'seconds': round(time.monotonic() - started, 2), 'summary': done or progress,
            'events': events, 'stderr': errfile.read_text(errors='replace')[:2000],
            'malformed_lines': malformed, 'raw_file': 'raw/' + raw.name}


def run_plist(argv, output, name):
    try:
        result = subprocess.run(argv, capture_output=True, env=ENV, timeout=10)
        (output / 'raw' / (name + '.plist')).write_bytes(result.stdout)
        if result.returncode:
            return {'error': result.stderr.decode(errors='replace')[:2000]}
        return plistlib.loads(result.stdout)
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.TimeoutExpired) as error:
        return {'error': str(error)}


def disk_inventory(output):
    apfs = run_plist(['/usr/sbin/diskutil', 'apfs', 'list', '-plist'], output, 'apfs')
    info = run_plist(['/usr/sbin/diskutil', 'info', '-plist', DATA], output, 'data')
    container = next((c for c in apfs.get('Containers', [])
                      if c.get('ContainerReference') == info.get('APFSContainerReference')), {})
    stores = []
    for index, store in enumerate(container.get('PhysicalStores', [])):
        device = store.get('DeviceIdentifier')
        if device:
            entry = run_plist(['/usr/sbin/diskutil', 'info', '-plist', device], output, 'physical-' + str(index))
            stores.append({'device': device, 'bytes': entry.get('TotalSize'), 'error': entry.get('error')})
    snapshots = run_plist(['/usr/sbin/diskutil', 'apfs', 'listSnapshots', '-plist', DATA], output, 'snapshots')
    free, ceiling = container.get('CapacityFree'), container.get('CapacityCeiling')
    volumes = [{'name': v.get('Name', ''), 'device': v.get('DeviceIdentifier'),
                'roles': v.get('Roles', []), 'used_bytes': v.get('CapacityInUse', 0)}
               for v in container.get('Volumes', [])]
    errors = {k: value['error'] for k, value in [('apfs', apfs), ('data', info), ('snapshots', snapshots)]
              if 'error' in value}
    return {'collected_at': now(), 'physical_bytes': sum(s['bytes'] for s in stores) if stores and all(s['bytes'] is not None for s in stores) else None,
            'physical_stores': stores, 'container_device': container.get('ContainerReference'),
            'container_bytes': ceiling, 'free_bytes': free, 'data_used_bytes': info.get('CapacityInUse'),
            'volumes': volumes, 'snapshots': snapshots, 'errors': errors,
            'container_metadata_difference_bytes': ceiling - free - sum(v['used_bytes'] for v in volumes)
            if ceiling is not None and free is not None else None}


def classify(path):
    p = display(path)
    pieces = Path(p).parts
    if any(inside(p, root) for root in PROFILE['protected_paths']):
        return ('本机保护数据', '保留；按本机配置单独调查', 'keep')
    if p.startswith(('/Library/', '/private/', DATA + '/System')):
        return ('系统管理数据', '通过系统或对应软件处理', 'keep')
    if any(marker in p for marker in ('/Library/Application Support/', '/Library/Containers/', '/Library/Group Containers/')):
        return ('应用数据', '保留；可能包含记录、数据库与素材', 'keep')
    if '.git' in pieces or any(part.endswith('.app') for part in pieces[:-1]):
        return ('版本历史或应用内部数据', '保留；先确认所属仓库或应用', 'keep')
    if '/Library/Caches/' in p or p.endswith('/Library/Caches'):
        return ('应用缓存', '核对所属应用及是否可重建', 'review_cache')
    if '/Library/Logs/' in p or p.endswith('/Library/Logs') or any(inside(p, root) for root in PROFILE['log_roots']):
        return ('应用日志', '先调查增长来源和保留需求', 'review_logs')
    if '/.Trash' in p:
        return ('废纸篓', '先核对内容', 'review_trash')
    is_project = any(inside(p, root) for root in PROFILE['project_roots'])
    if is_project and any(part in pieces for part in ('node_modules', '.venv', 'venv')):
        return ('项目依赖', '检查项目、锁文件及使用状态', 'review_dependencies')
    if is_project and any(part in pieces for part in ('build', 'dist', 'target', 'DerivedData')):
        return ('构建输出', '确认可重建、无唯一源文件且未被进程使用', 'review_build')
    if p.startswith('/Applications/') and p.endswith('.app'):
        return ('已安装应用', '核对用途后决定是否卸载', 'review_app')
    return ('个人文件与其他数据', '保留；结合目标电脑用途调查', 'keep')


def owner(path):
    p = display(path)
    if '.app/' in p or p.endswith('.app'):
        return p.split('.app', 1)[0].rsplit('/', 1)[-1] + '.app'
    for marker in ('/Library/Caches/', '/Library/Application Support/', '/Library/Containers/', '/Library/Group Containers/'):
        if marker in p:
            return p.split(marker, 1)[1].split('/', 1)[0]
    return classify(path)[0]


def report(data):
    payload = json.dumps(data, ensure_ascii=True).replace('<', '\\u003c').replace('&', '\\u0026')
    return (SKILL / 'assets/report.html').read_text(encoding='utf-8').replace('__PAYLOAD__', payload)


def publish(output, data):
    compact_inventory(data)
    save(output / 'analysis.json', data)
    (output / 'report.html').write_text(report(data), encoding='utf-8')
    save(BASE / 'latest-storage.json', {'report': str(output), 'created': data['created'], 'scope': data['scope']})
    if data['scope'] == DATA:
        save(BASE / 'latest-full-storage.json', {'report': str(output), 'created': data['created'], 'scope': DATA})
    free = data['disk'].get('free_bytes')
    lines = ['Mac 存储空间分析', data['created'], '', '容器可用：' + ('未取得' if free is None else '%.2f GB' % (free / 1e9)),
             '扫描分配块下界：%.2f GB' % (data['accounting']['allocated_reference_bytes'] / 1e9),
             '完整任务：%d/%d；访问错误：%d；云端目录缺口：%d' %
             (data['coverage']['complete_jobs'], data['coverage']['job_count'], data['coverage']['error_count'],
              data['coverage']['skipped_cloud_directory_count']), '', '候选占用不等于保证可回收量。',
             '结构化证据：' + str(output / 'analysis.json'), '报告：' + str(output / 'report.html')]
    (output / 'summary.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def collect(path, elevated=False, seconds=30, budget=120):
    scope = canonical(path)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output = BASE / 'reports' / (stamp + ('-admin' if elevated else '-user'))
    output.mkdir(parents=True, mode=0o700)
    raw = output / 'raw'; raw.mkdir(mode=0o700)
    print('扫描：' + scope + '\n结果：' + str(output), flush=True)
    disk = disk_inventory(output)
    deadline = time.monotonic() + budget
    listing = worker_call('list', scope, min(10, budget), elevated, raw)
    entries = [e for e in listing['events'] if e.get('event') == 'entry']
    root_kinds = {e['path']: e.get('kind') for e in entries}
    # One level only: no assumptions about project directories or another user's home.
    jobs = [e['path'] for e in entries] if listing['complete'] and entries else [scope]
    # Scan large local entries first. Cross-device roots remain explicit incomplete jobs.
    if jobs != [scope]:
        jobs = [e['path'] for e in sorted(entries, key=lambda e: e.get('allocated_bytes', 0), reverse=True)]
    results = []
    try:
        for job in jobs:
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                results.append({'path': job, 'display_path': display(job), 'returncode': 124,
                                'seconds': 0, 'stderr': 'Overall scan budget reached; not started',
                                'raw_file': '', 'summary': {}, 'events': [], 'elevated': elevated,
                                'complete': False, 'collected_at': now()})
                continue
            result = worker_call('scan', job, min(seconds, remaining), elevated, raw)
            result['root_kind'] = root_kinds.get(job)
            results.append(result)
            save(output / 'progress.json', {'state': 'scanning', 'jobs': len(jobs), 'finished': len(results)})
    except KeyboardInterrupt:
        data = enrich(results, [listing], disk, scope)
        data.update(elevated=elevated, cancelled=True)
        data['coverage']['all_jobs_complete'] = False
        data['coverage']['all_local_jobs_complete'] = False
        data['coverage']['unstarted_jobs'] = jobs[len(results):]
        publish(output, data)
        raise
    data = enrich(results, [listing], disk, scope)
    data.update(elevated=elevated, scan_started=disk['collected_at'], scan_finished=now(),
                per_job_seconds=seconds, scan_budget_seconds=budget)
    publish(output, data)
    save(output / 'progress.json', {'state': 'finished', 'finished': len(results), 'jobs': len(jobs)})
    print('已保存：' + str(output / 'analysis.json'), flush=True)
    return output, data


def doctor():
    output = BASE / 'preflight' / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output.mkdir(parents=True, mode=0o700); (output / 'raw').mkdir(mode=0o700)
    data = {'created': now(), 'platform': sys.platform, 'architecture': platform.machine(),
            'macos': platform.mac_ver()[0], 'python': sys.version.split()[0],
            'data_volume_present': Path(DATA).is_dir(), 'state_directory': str(BASE),
            'disk': disk_inventory(output),
            'permission_note': 'sudo and Full Disk Access are separate. Access gaps are unknown, not empty directories.'}
    save(output / 'doctor.json', data)
    print(json.dumps(data, ensure_ascii=True, indent=2))
    return data


def make_local(destination):
    destination = Path(destination).expanduser().absolute()
    if destination.resolve().is_relative_to(SKILL.resolve()):
        raise ValueError('Local customizations must be outside the distributable skill')
    if destination.exists():
        raise ValueError('Destination already exists; choose a new local directory to avoid overwriting custom work')
    destination.mkdir(parents=True, mode=0o700)
    for name in ('profile.json', 'investigate_local.py'):
        shutil.copyfile(SKILL / 'assets' / name, destination / name)
        (destination / name).chmod(0o600)
    (destination / '.gitignore').write_text('*\n', encoding='utf-8')
    print(str(destination))
    return destination


def enrich(results, listings, disk, scope):
    directories = {}
    files = {}
    errors = []
    cloud_directories={}
    hardlinks = {}
    corrections=collections.defaultdict(lambda:[0,0])
    repeated = 0
    measured = 0
    for r in sorted(results, key=lambda x: x['path']):
        measured += r['summary'].get('allocated_bytes', 0)
        for e in r.get('additional_events',[])+r['events']:
            kind = e.get('event')
            if kind in ('directory', 'file'):
                item = dict(e, scan_root=r['path'], scan_complete=r['complete'] and e.get('error_count',0)==0 and not e.get('skipped_cloud_directories',0),
                            local_scan_complete=r['complete'] and e.get('error_count',0)==0,
                            display_path=display(e['path']))
                category, action, policy = classify(e['path'])
                item.update(category=category, action=action, policy=policy, owner=owner(e['path']))
                target=directories if kind=='directory' else files
                previous=target.get(e['path'])
                if not (previous and previous['scan_complete'] and not item['scan_complete']):
                    target[e['path']]=item
            elif kind == 'hardlink':
                if e.get('supplemental'):continue
                key = (e['device'], e['inode'])
                if key in hardlinks:
                    repeated += e['allocated_bytes']
                    common=os.path.commonpath([hardlinks[key]['path'],e['path']])
                    while common==scope or common.startswith(scope+'/'):
                        corrections[common][0]+=e['allocated_bytes']
                        corrections[common][1]+=e.get('logical_bytes',0)
                        if common==scope:break
                        common=str(Path(common).parent)
                else:
                    hardlinks[key] = e
            elif kind == 'error':
                errors.append(dict(e, scan_root=r['path'], elevated=r['elevated']))
            elif kind == 'cloud_directory':
                cloud_directories[e['path']]=dict(e,display_path=display(e['path']))
        # A timeout retains a lower-bound progress total rather than a false zero.
        if r['path'] not in directories and r.get('root_kind') not in ('file', 'symlink', 'other'):
            s = r['summary']
            directories[r['path']] = dict(path=r['path'], display_path=display(r['path']),
                allocated_bytes=s.get('allocated_bytes', 0), logical_bytes=s.get('logical_bytes', 0),
                error_count=s.get('error_count', 0), scan_complete=r['complete'] and not s.get('skipped_cloud_directories',0),
                local_scan_complete=r['complete'], skipped_cloud_directories=s.get('skipped_cloud_directories',0),
                category=classify(r['path'])[0], action=classify(r['path'])[1], policy=classify(r['path'])[2],
                owner=owner(r['path']), scan_root=r['path'], incomplete_reason='partial' if not r['complete'] else None)
    # Restore partition ancestors from disjoint job totals. Do not add nested native directory rows.
    job_paths = {r['path'] for r in results}
    ancestors = set()
    for path in job_paths:
        parent = str(Path(path).parent)
        while parent == scope or parent.startswith(scope+'/'):
            ancestors.add(parent)
            if parent == scope: break
            parent = str(Path(parent).parent)
    for path in sorted(ancestors, key=len, reverse=True):
        descendants = [r for r in results if r['path'].startswith(path+'/')]
        if not descendants: continue
        directories[path] = dict(path=path, display_path=display(path),
            allocated_bytes=sum(r['summary'].get('allocated_bytes', 0) for r in descendants),
            logical_bytes=sum(r['summary'].get('logical_bytes', 0) for r in descendants),
            scan_complete=all(r['complete'] and not r['summary'].get('skipped_cloud_directories',0) for r in descendants),
            local_scan_complete=all(r['complete'] for r in descendants),
            error_count=sum(r['summary'].get('error_count',0) for r in descendants),
            skipped_cloud_directories=sum(r['summary'].get('skipped_cloud_directories',0) for r in descendants),
            category=classify(path)[0],
            action=classify(path)[1], policy=classify(path)[2], owner=owner(path), synthetic=True)
    for path,(blocks,logical) in corrections.items():
        if path in directories:
            directories[path]['allocated_bytes']=max(0,directories[path]['allocated_bytes']-blocks)
            directories[path]['logical_bytes']=max(0,directories[path]['logical_bytes']-logical)
    candidates = []
    for d in sorted(directories.values(), key=lambda x: (len(x['path'].split('/')), -x['allocated_bytes'])):
        if d['allocated_bytes'] < 128*1024**2 or d['policy'] == 'keep': continue
        if d['policy'] == 'review_app' and not d['path'].endswith('.app'): continue
        if any(d['path'].startswith(c['path']+'/') for c in candidates): continue
        candidates.append(dict(d, recoverable_bytes=None, estimate='目录分配块占用；不是保证可回收量'))
    candidates.sort(key=lambda x: x['allocated_bytes'], reverse=True)
    groups = collections.defaultdict(list)
    for f in files.values():
        if f['logical_bytes']>=32*1024**2 and f['allocated_bytes']>0 and not f.get('dataless'):
            groups[f['logical_bytes']].append(f)
    duplicate_candidates = []
    for size, members in groups.items():
        unique = {(f.get('device'),f.get('inode')) for f in members}
        if len(unique)>1:
            duplicate_candidates.append({'logical_bytes':size, 'count':len(members),
                                         'verified':False, 'files':members[:100]})
    mounts = [e for r in results for e in r['events'] if e.get('event') == 'mount']
    incomplete = [{k:r.get(k) for k in ['path','display_path','returncode','seconds','stderr','raw_file']}
                  for r in results if not r['complete']]
    account = max(0, measured-repeated)
    gap = disk['data_used_bytes']-account if scope==DATA and disk.get('data_used_bytes') is not None else None
    return {'schema_version':3, 'created':now(), 'scope':scope, 'disk':disk,
            'accounting':{'allocated_reference_bytes':account, 'cross_job_hardlink_bytes_removed':repeated,
                          'data_volume_difference_bytes':gap,
                          'note':'分配块按硬链接去重；APFS 克隆共享块、快照、元数据、权限和采样时序使其不能等同于物理独占空间。'},
            'coverage':{'job_count':len(results), 'complete_jobs':len(results)-len(incomplete),
                        'incomplete_jobs':incomplete, 'error_count':len(errors),
                        'all_jobs_complete':not incomplete and not cloud_directories and not mounts, 'all_local_jobs_complete':not incomplete,
                        'file_count':sum(r['summary'].get('files',0) for r in results),
                        'skipped_cloud_directory_count':len(cloud_directories), 'skipped_mount_count':len(mounts)},
            'directories':sorted(directories.values(),key=lambda x:x['allocated_bytes'],reverse=True),
            'large_files':sorted(files.values(),key=lambda x:x['allocated_bytes'],reverse=True),
            'candidates':candidates, 'duplicate_candidates':sorted(duplicate_candidates,key=lambda x:x['logical_bytes']*x['count'],reverse=True),
            'errors':errors, 'jobs':[{k:v for k,v in r.items() if k not in ('events','additional_events')} for r in results],
            'cloud_directories':list(cloud_directories.values()), 'skipped_mounts':mounts,
            'listings':[{k:v for k,v in r.items() if k!='events'} for r in listings]}


def compact_inventory(data):
    retained=[]
    markers={'node_modules','.git','.venv','venv','target','build','dist','DerivedData','Caches','Cache','cache','logs','Logs','pkgs'}
    for row in data['directories']:
        root=row.get('scan_root',row['path'])
        depth=len(Path(row['path']).parts)-len(Path(root).parts)
        if (row.get('synthetic') or depth<=2 or row['allocated_bytes']>=1024**2
                or Path(row['path']).name in markers):
            retained.append({k:v for k,v in row.items() if k in ['path','display_path','allocated_bytes','logical_bytes',
                'scan_complete','local_scan_complete','owner','category','error_count','synthetic','skipped_cloud_directories']})
    data['directories']=retained
    return data


def query(report_dir,path=None,limit=15,policy=None,full=False):
    if not 1<=limit<=100:raise ValueError('查询条数为 1 至 100')
    pointer=BASE/'latest-storage.json'
    output=Path(report_dir) if report_dir else Path(json.loads(pointer.read_text())['report'])
    data=json.loads((output/'analysis.json').read_text())
    if full and not path and not policy:return data
    p=canonical(path) if path else data['scope']
    inside=lambda item:item['path']==p or item['path'].startswith(p+'/')
    fields=['display_path','allocated_bytes','logical_bytes','category','owner','policy','action',
            'scan_complete','local_scan_complete','error_count','skipped_cloud_directories','dataless']
    concise=lambda row:row if full else {k:row[k] for k in fields if k in row}
    candidates=[x for x in data['candidates'] if inside(x) and (not policy or x['policy']==policy)]
    groups=collections.defaultdict(lambda:{'count':0,'allocated_bytes':0})
    for c in candidates:
        group=groups[c['policy']];group['count']+=1;group['allocated_bytes']+=c['allocated_bytes']
    directories=sorted([x for x in data['directories'] if x['path']==p or str(Path(x['path']).parent)==p],
                       key=lambda row:row['allocated_bytes'],reverse=True)
    files=[x for x in data['large_files'] if inside(x) and (not policy or x.get('policy')==policy)]
    errors=[x for x in data['errors'] if inside(x)]
    clouds=[x for x in data.get('cloud_directories',[]) if inside(x)]
    incomplete=[x for x in data['coverage']['incomplete_jobs'] if inside(x)]
    disk={k:v for k,v in data['disk'].items() if k not in ['raw','snapshots']}
    coverage={k:v for k,v in data['coverage'].items() if k!='incomplete_jobs'}
    return {'report':str(output),'evidence_file':str(output/'analysis.json'),'created':data['created'],
            'freshness':{'disk_collected_at':data['disk'].get('collected_at'),
                         'retained_directory_snapshot':data.get('retained_directory_snapshot'),
                         'mixed_permission_sources':data.get('mixed_permission_sources',False)},
            'path':p,'policy_filter':policy,'disk':disk,'accounting':data['accounting'],'whole_scan_coverage':coverage,
            'counts_in_path':{'directories':len(directories),'large_files':len(files),'candidates':len(candidates),
                              'errors':len(errors),'cloud_directories':len(clouds),'incomplete_jobs':len(incomplete)},
            'candidate_totals':dict(groups),'limit':limit,'directories':[concise(x) for x in directories[:limit]],
            'candidates':[concise(x) for x in candidates[:limit]],'large_files':[concise(x) for x in files[:limit]],
            'errors':errors[:limit],'cloud_directories':[concise(x) for x in clouds[:limit]],
            'incomplete_jobs':incomplete[:limit]}


def main():
    global BASE, PROFILE
    os.umask(0o077)
    if os.geteuid() == 0:
        raise ValueError('Run the coordinator as the login user; elevation is limited to the metadata worker')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=str(BASE))
    parser.add_argument('--profile', help='Optional local profile, never part of the shared skill')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor'); sub.add_parser('build')
    scan = sub.add_parser('collect')
    scan.add_argument('--path', default=str(HOME)); scan.add_argument('--elevated', action='store_true')
    scan.add_argument('--seconds', type=int, default=30); scan.add_argument('--budget', type=int, default=120)
    query_parser = sub.add_parser('query'); query_parser.add_argument('--report'); query_parser.add_argument('--path')
    query_parser.add_argument('--limit', type=int, default=15); query_parser.add_argument('--policy')
    query_parser.add_argument('--full', action='store_true')
    local = sub.add_parser('make-local'); local.add_argument('--destination', required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise ValueError('This skill scans macOS APFS startup disks')
    BASE = prepare_state(args.state_dir)
    PROFILE = load_profile(args.profile)
    if args.command == 'doctor':
        doctor()
    elif args.command == 'build':
        print(build_worker())
    elif args.command == 'make-local':
        make_local(args.destination)
    elif args.command == 'collect':
        if not 1 <= args.seconds <= 300 or not 1 <= args.budget <= 3600:
            raise ValueError('Per-job seconds: 1..300; overall traversal budget: 1..3600')
        if args.elevated and not sudo_ready():
            raise ValueError('Authorize sudo in your own Terminal with sudo -v, then rerun; never send passwords to the agent')
        build_worker()
        collect(args.path, args.elevated, args.seconds, args.budget)
    elif args.command == 'query':
        print(json.dumps(query(args.report, args.path, args.limit, args.policy, args.full), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped. Completed evidence has been kept locally.', file=sys.stderr); sys.exit(130)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Storage inspector: ' + str(error), file=sys.stderr); sys.exit(2)
