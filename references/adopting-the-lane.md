# Adopting the lane in an app repo

Per-app setup, once the runner and the org secrets exist. Fifteen minutes in
the repo, plus whatever the Apple portal needs.

Placeholders: `<APP_BUNDLE_ID>`, `<IOS_DIR>`, `<SHIP_BRANCH>`.

> **The repo side is short. The Apple side is where the time goes.** Every item
> in section 4 fails *late* — after the runner has been claimed, the keychain
> built, the pods installed and the archive produced. Several of them fail with
> errors that name something other than themselves. Do section 4 first if
> you are in a hurry.

## 0. Prerequisites

- An org runner online, from `setup-org.md`, or a repo runner from
  `setup-runner.md`
- The seven shared secrets set at org level, or repo level for a single app
- **A private repo.** See `security-posture.md`; a self-hosted runner on a
  public repo runs fork-authored code as the account holding your certificate
- Admin on the Apple team, for section 4

## 1. Copy the template in

From the app repo root:

```bash
cp -R <SKILL_DIR>/workflow/. .github/
git status --short .github
```

`workflow/` becomes `.github/`: the workflow at
`.github/workflows/testflight.yml`, the helper scripts at `.github/scripts/`,
and the config example alongside them.

**Copy the scripts verbatim and leave them alone.** They are identical in every
repo, and that is what makes a fix in one app a fix in all of them. A locally
patched copy diverges silently and the divergence is only discovered when the
same bug is fixed twice.

## 2. Write `.github/app-config.env`

```bash
cp .github/app-config.env.example .github/app-config.env
```

This is **the only per-app file**. The workflow `source`s it with `set -a`, so
it is shell syntax: no spaces around `=`, and quote any value containing a
space. Profile names contain spaces.

Required, always:

| Key | Meaning |
|---|---|
| `APP_BUNDLE_ID` | Exactly as it appears in the Apple portal |
| `IOS_DIR` | Path from repo root to the dir holding the `.xcodeproj` |
| `FLUTTER` | `1` for a Flutter app, `0` for native iOS |
| `HAS_EXTENSION` | `1` if the app ships an app extension, else `0` |

Optional, with defaults that are usually right: `APP_PROFILE_NAME`,
`PROFILE_TYPE`, `SCHEME` (required when `FLUTTER=0`), `XCODE_WORKSPACE`,
`XCODE_PROJECT`, `XCODEBUILD_EXTRA`, `FLUTTER_VERSION`, `KEYCHAIN_NAME`.

Only when `HAS_EXTENSION=1`: `EXTENSION_BUNDLE_ID`, `EXTENSION_PROFILE_NAME`,
`APP_GROUP`, `EXTENSION_INFO_PLIST`, `EXTENSION_POINT_ID`,
`EXTENSION_PRINCIPAL_CLASS_SUFFIX`.

**Nothing in this file is secret and the file is committed.** Every credential
arrives from org secrets. If a value feels like it needs hiding, it is in the
wrong place.

**`APP_PROFILE_NAME` is chosen once and never changed.** The lane mints a
profile under that name and reuses it forever after. Renaming it later does not
rename the profile — it mints a *second* one, and the Apple account slowly
fills with near-duplicates that are impossible to tell apart at signing time.

## 3. Point the job at your runner

In `.github/workflows/testflight.yml`:

```yaml
    runs-on: [self-hosted, macOS, ios]
```

**These labels must be a subset of the labels the runner registered with.**
GitHub matches on all of them, and a job requesting a label nobody has does not
error — it queues, indefinitely, showing "Waiting for a runner". That reads as
a broken runner rather than a typo, and people restart a perfectly healthy
service over it.

Check the actual labels before guessing:

```bash
gh api /orgs/<YOUR_ORG>/actions/runners --jq '.runners[].labels[].name'
```

While you are in the file, confirm the trigger branch is the one you intend.
The shipped lane triggers on push to `ship/testflight`. **Pushing that branch
uploads to Apple**, which is why it is named the way it is and why nobody
develops on it.

## 4. Apple-side prerequisites the lane will not do for you

These are the expensive ones. Each has cost a full build cycle at least once.

**The bundle id must already exist** in Certificates, Identifiers & Profiles.
The lane mints *profiles*; it never creates an *identifier*. A missing one
surfaces as a profile-creation failure naming the bundle id, which reads as a
permissions problem with the API key.

**If the app uses an App Group, that is two separate actions, and doing only
the first is the classic trap.** You must (a) enable the App Groups capability
on the identifier, and (b) attach the specific group to it. The portal shows
the capability as enabled after (a), which looks finished. A profile minted in
that state is valid, current, and missing the entitlement — and it signs
happily until the extension fails at runtime, or `codesign` rejects the
entitlement mismatch with an error that mentions neither the group nor the
portal.

Do both, in that order, **before** the first run. A profile captures its
entitlements at creation, so one minted between (a) and (b) stays broken
forever while continuing to look correct.

**A distribution certificate must exist on the team**, and its `.p12` is what
`DIST_CERT_P12_BASE64` holds. Apple caps active distribution certificates at
two. Revoking one to make room breaks every other machine using it, including
other developers' Xcode installs, with no notification.

**The build number must exceed the last uploaded build for that version.**
Apple rejects the upload outright — after the archive, after the signing, at
the very end. The lane derives a build number from the run number, so this
mainly bites when the repo is new, when the run counter is reset, or when a
build was uploaded from a laptop in between. Check what Apple already has
before the first run rather than after it.

## 5. Ship one build

```bash
git checkout -b <SHIP_BRANCH>
git push -u origin <SHIP_BRANCH>
gh run watch
```

The first run is the slow one: pods resolve cold, and the profile is minted
rather than reused. Subsequent runs reuse both.

## Verify

Before the first push, confirm the wiring rather than the outcome:

```bash
bash -n .github/workflows/testflight.yml 2>/dev/null || true
grep -c . .github/app-config.env
gh secret list --org <YOUR_ORG>
gh api /orgs/<YOUR_ORG>/actions/runners --jq '.runners[] | {status, labels: [.labels[].name]}'
```

Then the adversarial part — **all three must fail**:

```bash
git check-ignore .github/app-config.env      # must NOT be ignored; it is committed
grep -riE 'BEGIN (RSA |EC )?PRIVATE KEY|-----BEGIN CERT' .github/   # no keys in the repo
gh api repos/<YOUR_ORG>/<REPO> --jq '.private' | grep -q false      # must not be public
```

After the first successful run, verify the profile was **reused** rather than
re-minted on the second run. Two profiles with the same name in the portal
means `APP_PROFILE_NAME` changed between runs, and the build is now signing
with whichever one the API returns first.

## Known limits

**One bundle id per config file.** An app with several targets that each need
their own profile beyond a single extension is outside what this config
expresses.

**The lane does not create identifiers, capabilities, or certificates.** It
consumes them. Everything in section 4 stays manual, by design — those actions
change what your Apple team can do, and they are not things a push should
perform.

**Build numbers come from the run counter.** Moving the repo, or resetting the
counter, can produce a number Apple has already seen. `BUILD_OFFSET` exists as
a repo variable for exactly that recovery.

**Local builds still need their own signing setup.** Nothing here configures
Xcode on a developer's laptop; the lane's profiles live on the runner.

**A green run means Apple accepted the upload, not that the build works.**
Processing, export compliance, and TestFlight distribution all happen after the
lane's last step reports success.
