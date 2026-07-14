# memory/

Runtime data directory for the biz-analyst-agent memory system.

Three files are created automatically on first run:

| File | Purpose |
|------|---------|
| `experience.json` | Task summaries and evaluation feedback (write-heavy, read during Reflection) |
| `domain.json` | Industry analysis frameworks and patterns (read during Scope) |
| `behavior.json` | Collection/falsification/reporting strategies (read during Collect/Falsify/Report) |

**Do not commit runtime data to git.** These files contain task-specific information
generated during analysis runs. The `.gitignore` excludes them.

If upgrading from the legacy six-store layout, run the migration script once:

```bash
python scripts/migrate_memory.py
```

Old files will be backed up to `memory/_old/`.
