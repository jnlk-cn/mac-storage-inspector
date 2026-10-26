#!/usr/bin/env python3
"""Local investigation scaffold. Default action prints a plan; scan reads metadata."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'scan'], nargs='?', default='plan')
    parser.add_argument('--skill-dir', help='Installed mac-storage-inspector directory')
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--budget', type=int, default=120, help='Shared traversal budget across all targets')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300 or not 1 <= args.budget <= 3600:
        parser.error('seconds must be 1..300 and budget must be 1..3600')
    local = Path(__file__).resolve().parent
    profile_path = local / 'profile.json'
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    if profile.get('schema_version') != 1:
        raise ValueError('Unsupported profile schema')
    home = str(Path.home())
    targets = profile.get('targets', [])
    if not isinstance(targets, list) or not all(isinstance(p, str) and p.startswith(('/', '${HOME}/')) for p in targets):
        raise ValueError('targets must be absolute paths or use ${HOME}/')
    targets = [p.replace('${HOME}', home) for p in targets]
    candidates = ([Path(args.skill_dir).expanduser()] if args.skill_dir else
                  [Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'skills/mac-storage-inspector',
                   Path.home() / '.agents/skills/mac-storage-inspector'])
    skill = next((p for p in candidates if (p / 'scripts/storage_inspector.py').is_file()), None)
    if skill is None:
        raise ValueError('Locate the installed skill and pass --skill-dir')
    helper = skill / 'scripts/storage_inspector.py'
    # Import path validation without performing a scan or preparing state.
    import importlib.util
    spec = importlib.util.spec_from_file_location('storage_inspector_local', helper)
    inspector = importlib.util.module_from_spec(spec); spec.loader.exec_module(inspector)
    validated_profile = inspector.load_profile(profile_path)
    targets = [inspector.display(inspector.canonical(p)) for p in targets]
    state = local / 'state'
    if args.action == 'plan':
        print(json.dumps({'action': 'plan', 'targets': targets, 'state_directory': str(state),
                          'profile': validated_profile, 'per_job_seconds': args.seconds,
                          'shared_budget_seconds': args.budget, 'changes_to_source_files': False}, ensure_ascii=True, indent=2))
        return
    if not targets:
        raise ValueError('No targets configured. The agent must select exact paths from current evidence first.')
    # Each target shares this invocation's budget; never execute profile strings as shell commands.
    import time
    deadline = time.monotonic() + args.budget
    for target in targets:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise ValueError('Shared investigation budget reached; remaining targets were not scanned')
        command = [sys.executable, str(helper), '--state-dir', str(state), '--profile', str(profile_path),
                   'collect', '--path', target, '--seconds', str(args.seconds), '--budget', str(remaining)]
        result = subprocess.run(command)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('Local investigation: ' + str(error), file=sys.stderr); sys.exit(2)
