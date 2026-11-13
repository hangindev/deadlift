---
name: pr
description: "Use when writing a PR body."
metadata:
  credits:
    skill: show-me
    author: Dex Horthy
    organisation: Humanlayer
    url: "https://github.com/humanlayer/skills/blob/main/plugins/show-me/skills/show-me/SKILL.md"
---

Write the PR body to this template. Use the domain language from `GLOSSARY.md`.

```markdown
**Issue:** #<n>

<One or two sentences: what changes, and why.>

<optional: one visual>

## Steps to Test

1. <action> → <expected result>

## Supporting Resources

- <link>
```

## Sections

Write it as an **inverted pyramid**: the most important thing first, detail after. A reviewer who stops after the first line knows what the PR does. Keep it brief: no preamble, no restating the diff.

**Issue** links the issue the PR resolves or belongs to. When there is none, the line goes.

**Description** is the unheaded opening: one or two sentences, what changes and why. Add one visual only when two sentences can't show the shape of the change (a new flow, a file layout, a decision's diagram). Pick it from [`VISUALS.md`](VISUALS.md).

**Steps to Test** is the heart of the PR: what the reviewer does to verify it. Number the steps, each one `action → expected result`, with commands they can paste. Cover each case the PR affects. For a docs-only PR, each step names what to read and what to check for in it.

**Supporting Resources** links what the reviewer may want beside the diff: ADRs, specs, docs, upstream issues. Delete the section when there's nothing to link.

The PR is ready when its first sentence says what it does and every case it affects has a step to test it.
