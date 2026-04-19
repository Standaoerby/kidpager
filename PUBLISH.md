# Publishing to GitHub

Quick guide to create a new GitHub repository and push this release as tag `v0.9`.

## 1. Create an empty repository on GitHub

Go to https://github.com/new and create a new **empty** repo:

- **Repository name:** `kidpager`
- **Description:** `Two-device LoRa text messenger for kids, on Raspberry Pi Zero 2 W`
- **Visibility:** Public or Private, your choice
- **DO NOT** check "Add README", "Add .gitignore", or "Choose a license" — this release already contains them

Click **Create repository**. On the next page GitHub will show you the URL, e.g.:
```
https://github.com/YOUR_USERNAME/kidpager.git
```

## 2. Initialize and push from the release folder

Open PowerShell (or terminal) in the folder with this release:

```powershell
cd path\to\kidpager-0.9

git init
git add .
git commit -m "Release 0.9"
git branch -M main

# Replace YOUR_USERNAME with your actual GitHub username
git remote add origin https://github.com/YOUR_USERNAME/kidpager.git
git push -u origin main
```

If this is the first time you push to GitHub from this machine, you'll be asked for credentials.
Use a Personal Access Token, not your password — generate one at:
https://github.com/settings/tokens (scope: `repo`).

## 3. Create the 0.9 tag and release

```powershell
git tag -a v0.9 -m "KidPager 0.9 — first public release"
git push origin v0.9
```

Then on GitHub:

1. Go to your repo → **Releases** (right sidebar) → **Draft a new release**
2. **Choose a tag:** select `v0.9`
3. **Release title:** `KidPager 0.9`
4. **Description:** copy the contents of `RELEASE_NOTES.md` into the description box
5. Optionally attach the release archive (e.g. `kidpager-0.9.zip`) as a binary asset
6. Click **Publish release**

## 4. Verify

Your repo now has:
- `main` branch with all source files
- Tag `v0.9` pointing at this release
- A published GitHub Release page with notes
- README rendered on the repo landing page

## Future updates

For subsequent versions:

```powershell
# make your changes
git add .
git commit -m "Describe what changed"
git push

# when ready to tag another release
git tag -a v0.10 -m "KidPager 0.10 — what's new"
git push origin v0.10
```

Then draft a new release on GitHub for tag `v0.10`.
