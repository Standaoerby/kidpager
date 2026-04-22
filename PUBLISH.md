# Publishing to GitHub

Quick guide for tagging and pushing a new KidPager release. Current
release: **v0.14**.

## First-time setup (only once per machine)

Go to https://github.com/new and create an **empty** repo:

- **Repository name:** `kidpager`
- **Description:** `Two-device LoRa text messenger for kids, on Raspberry Pi Zero 2 W`
- **Visibility:** Public or Private, your choice
- **DO NOT** check "Add README", "Add .gitignore", or "Choose a license"
  — this release already contains them

Click **Create repository**. Note the URL:
```
https://github.com/YOUR_USERNAME/kidpager.git
```

Initialize and push:

```powershell
cd path\to\kidpager

git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/kidpager.git
git push -u origin main
```

First push asks for credentials. Use a Personal Access Token, not your
password — generate one at https://github.com/settings/tokens (scope: `repo`).

## Release v0.14 (current)

```powershell
# Make sure everything is committed
git add .
git commit -m "Release v0.14 — terminus font + keyboard rewrite + cursor"
git push

# Tag and push the tag
git tag -a v0.14 -m "KidPager v0.14 — no more dropped keys, sharper rendering, static cursor"
git push origin v0.14
```

Then on GitHub:

1. Go to your repo → **Releases** (right sidebar) → **Draft a new release**
2. **Choose a tag:** select `v0.14`
3. **Release title:** `KidPager v0.14`
4. **Description:** copy the contents of `RELEASE_NOTES.md` into the description box
5. Click **Publish release**

## Release checklist (for any future version)

Before tagging, make sure these are consistent:

- [ ] `CHANGELOG.md` has a new section at the top for this version
- [ ] `RELEASE_NOTES.md` describes the new version (replace old content, or keep and rename to `RELEASE_NOTES_vN.md`)
- [ ] `README.md` — any new features or hardware changes reflected
- [ ] `BOM.md` — any new components in the per-unit BOM
- [ ] `PUBLISH.md` — version number in the commands matches
- [ ] `diagnose.py` — if you added a new `.py` file, it's in the file-presence check
- [ ] `deploy.ps1` `$PY_FILES` — same, new file copied to the pager
- [ ] All `.py` files parse (`python3 -m py_compile *.py`)
- [ ] `test_retry.py` passes (if you touched `ui.py`)
- [ ] Diagnose green on a live pager (`.\deploy.ps1 -Diag`)

## Future updates

For subsequent versions, bump the patch (v0.15) for bug fixes, minor (v0.20)
for feature additions, major (v1.0) once the project is considered stable:

```powershell
git add .
git commit -m "Describe what changed"
git push

git tag -a v0.15 -m "KidPager v0.15 — what's new in one sentence"
git push origin v0.15
```

Then draft a new release on GitHub for that tag with the RELEASE_NOTES.md
content pasted in.
