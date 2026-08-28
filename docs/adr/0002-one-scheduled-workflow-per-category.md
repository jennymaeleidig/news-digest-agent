# One scheduled workflow per category

Each category runs as its own GitHub Actions workflow (`digest-<id>.yml`) on a
staggered UTC schedule rather than one daily workflow looping over all
categories: a category failing then fails only its own run, the four digests
arrive as separate scheduled deliveries, and one category's schedule can move
without touching the others. The cost is four near-identical workflow files
with the cron duplicated in the category JSON and the workflow YAML; the
`categories/<id>.json` `schedule` field is the source of truth; a workflow's
cron drifting from it must be fixed in the workflow file.

Adding a category therefore means adding a third file (the workflow) beside the
category JSON and its prompt — see the README's Categories section.
