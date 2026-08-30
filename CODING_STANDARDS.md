# Coding standards

## Tests are local only

`tests/` is gitignored — regression tests never get committed. Write and
run them on your machine (`python -m pytest tests`); the repo and CI carry
none. When a change relies on a test to prove it, the test still lives
only in your local `tests/` — say so in the commit message instead of
committing the file.

Everything else follows the normal rules: code, configs, prompts, docs,
and the `data/` state files all get committed.
