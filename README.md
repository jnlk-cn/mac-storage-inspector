# Mac Storage Inspector

A local, read-only macOS storage inspection skill for coding agents. It measures APFS volume usage and file allocation, preserves scan gaps, and helps the agent create investigation scripts for the current Mac.

## Install

Clone this repository into your agent's skills directory. For Codex, use:

```sh
# Set REPOSITORY_URL to this repository's Git clone URL.
SKILL_DEST="${CODEX_HOME:-$HOME/.codex}/skills/mac-storage-inspector"
git clone "$REPOSITORY_URL" "$SKILL_DEST"
```

Use a new destination if a skill with this name is already installed. Other agents can use their own skills directory or read [SKILL.md](SKILL.md) directly.

## Use

Ask the agent: `使用 $mac-storage-inspector 检查这台 Mac 的存储空间，并根据检查结果生成本地定制调查脚本。`

The workflow checks the current environment, scans a bounded scope, and creates a separate local investigation case. Requirements: Python 3.9+, a modern macOS APFS startup Data volume, and installed Apple Command Line Tools for native compilation. Binaries are built on the target Mac; none are included here.

```sh
python3 "$SKILL_DEST/scripts/storage_inspector.py" doctor
python3 "$SKILL_DEST/scripts/storage_inspector.py" build
python3 "$SKILL_DEST/scripts/storage_inspector.py" collect --path "$HOME" --seconds 30 --budget 120
python3 "$SKILL_DEST/scripts/storage_inspector.py" query --limit 15
```

The traversal budget excludes environment checks, compilation and volume queries, which have separate timeouts. Intel runtime behavior and all macOS/Python version combinations have not been verified; cross-compilation is not a substitute for testing on the target machine. HFS+ and external volumes need a different inspection method.

## Local customization and privacy

- `assets/profile.json` contains empty path lists; `assets/investigate_local.py` is a generic scaffold, not a configured machine-specific script.
- Actual machine profiles, custom scripts, reports and build caches belong outside this repository and outside the installed skill. See [the customization workflow](references/customization.md).
- The scanner reads file metadata, does not follow symbolic links or cross filesystem boundaries, and records inaccessible or cloud-only directories as coverage gaps.
- Inspection does not authorize deletion. The tool has no cleanup command and does not upload reports or install privileged background services.
- Allocated bytes and same-size duplicate candidates are not guaranteed reclaimable space. APFS clones, snapshots and metadata affect accounting.

## Verify

```sh
python3 "$SKILL_DEST/scripts/test_storage_inspector.py"
```

Tests use temporary fixtures and require no sudo. The repository contains reusable instructions, source code and empty templates; it does not contain personal scan results, machine profiles or historical cleanup scripts.
