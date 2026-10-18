# Contributing

Thanks for helping. whystuck is intentionally small: one file, no runtime dependencies, Python 3.9+.

```bash
git clone https://github.com/mrzroot/whystuck && cd whystuck
pip install -e . pytest
pytest -q
```

## Adding or changing a detection rule

1. Write a `_check_<name>(snapshot) -> Optional[Hit]` function in `whystuck.py`. It must be a **pure function of the snapshot**
   (no I/O). Return `None` when your evidence is absent; otherwise return a `Hit(points, summary, evidence, fixes)`.
   Points are the number of independent signals that agree (1 low, 2 medium, 3+ high).
2. Add a `Rule(id, title, stuck, doc, check)` row to `RULES`. Order matters: more specific rules go first, `unknown` stays last.
3. Add test cases to `tests/test_classify.py`: at least one positive and one **negative** case (something that looks similar but must *not* match).
   The mutation-style test fails if no case depends on your rule.
4. If the rule depends on real `/proc` data, add a recording to `tests/record_fixtures.py` and run it.
5. Update the README rule table (`whystuck --rules`) and `CHANGELOG.md`.

## Ground rules

- **Honesty over coverage.** Never describe a detection that is not implemented; say so in "Limits" instead.
- Fix suggestions must be real commands/flags. If you are not sure a flag exists for a tool, suggest "check `--help`" instead.
- Never print environment variable *values* or secrets.
- Keep it stdlib-only and compatible with Python 3.9 (CI runs 3.9 to 3.13).
- Include evidence lines that let the user verify the diagnosis themselves.

## Reporting a bad diagnosis

Run again with `--json --include-snapshot` and attach the output (review it first: it contains the command lines of the process tree
and the last output of the command).
