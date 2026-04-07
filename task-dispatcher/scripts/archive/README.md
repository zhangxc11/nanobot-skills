# Archive — V1 Legacy Code

> Archived: 2026-04-07 (T-20260407-003)

This directory contains V1 dispatcher code that has been superseded by V2.
Files are preserved for reference but are no longer part of the active codebase.

## Archived Files

| File | Lines | Description |
|------|-------|-------------|
| `scheduler_legacy.py` | 4186 | V1 full-featured scheduler (replaced by scheduler.py V2) |
| `rule_loader.py` | ~150 | V1 rule loading mechanism (V2 reads rules/*.md directly) |
| `test_scheduler_legacy.py` | 2160 | Tests for V1 scheduler |
| `test_callback_mechanism.py` | 269 | V1 callback tests |
| `test_rule_injection.py` | 541 | V1 rule injection tests |
| `test_rule_loader.py` | ~200 | Tests for rule_loader.py |
| `test_scheduler_evidence.py` | 360 | V1 evidence tests |
| `test_scheduler_notify.py` | 501 | V1 notification tests |
| `test_scheduler_status.py` | 103 | V1 status tests (concepts may apply to V2 but imports are V1-bound) |
| `test_scheduler_version.py` | 42 | V1 version tests |
| `scheduler.py.bak-phase1` | 133KB | Pre-V2 scheduler backup |

## Why Archived (Not Deleted)

- V1 code contains design patterns and edge-case handling that may inform future V2 improvements
- `test_scheduler_status.py` tests concepts applicable to V2, but would need rewrite (imports `from scheduler import get_scheduler_status` which is V1 API)
- Preserving git history is easier with move than delete

## V2 Replacements

- `scheduler_legacy.py` → `../scheduler.py` (V2, ~500 lines)
- `rule_loader.py` → Dispatcher agent reads `rules/*.md` directly
- Role guidance → `_nanobot-skills/role-flow/roles/*/ROLE.md`
