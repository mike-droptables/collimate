---
name: release-notes
description: Draft release notes from a repository's git log and changelog.
---
# Release notes

A worked example of the Agent Skills format a runner can drop into the agent's
`_skills` config folder. Frontmatter carries `name` + `description` (a superset
— claude-code reads any extra keys, other harnesses use just these two); the
body below is the full instruction the model loads on demand.

## Steps

1. Read `CHANGELOG.md` (if present) and the recent `git log` to gather what
   changed since the last tag.
2. Group changes into Added / Changed / Fixed / Removed.
3. Write concise, user-facing notes — one bullet per change, no internal jargon.

Bundled helper scripts (if any) live beside this file under `scripts/`; running
them still rides the run's exec surface, never a new grant.
