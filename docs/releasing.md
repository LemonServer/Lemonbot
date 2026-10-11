# Release procedure

Own project version: pyproject.toml, src/lemonbot/__init__.py and root package in uv.lock.
MCP client information imports the runtime version. Never independently stamp a conflicting version.

1. Update current docs, changelog, evidence matrix and notes; archive dated experiments.
2. Run the checks in CONTRIBUTING.md and an isolated checkout demo/setup test.
3. Run `bash scripts/scan_release_tree.sh` to scan HEAD’s exported public tree with
   pinned/checksummed Gitleaks. This works with shallow checkouts and never needs a
   push-history range. No DB, private config, identifiers or secrets.
4. Drain live work before any deployment; preserve volumes, context, pause and UNKNOWN.
5. Fetch remote main, verify ancestry, resolve differences without force push.
6. Commit release records, create an annotated tag and push the exact commit/tag.
7. Check GitHub authentication locally, then create Release from the pushed tag:

```bash
git tag -a v1.0.0 -m 'Lemonbot 1.0.0'
git push origin main v1.0.0
gh release create v1.0.0 --repo LemonServer/Lemonbot --verify-tag \
  --title 'Lemonbot 1.0.0' --notes-file docs/release-1.0-notes.md
```

Only after verifying remote tag SHA and Release should publishing be marked complete.
If authentication is missing, leave the local annotated tag unchanged and finish the
same push/create steps after browser login; don't invent a published URL or recreate the tag.
Use [official CLI behavior](https://cli.github.com/manual/gh_release_create).
Source/docs only; no upstream image, installer, private state or PyPI upload.
