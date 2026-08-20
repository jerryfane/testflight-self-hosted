# TestFlight CI template

A drop-in lane that builds an iOS app on the org's self-hosted Mac and uploads
it to TestFlight, with **zero per-app credentials**. Generalised from the lane
this came from — the one that actually shipped a build — with everything
app-specific moved into a single config file.

```
.github/workflows/testflight.yml   copy verbatim
.github/app-config.env             the ONLY file you write (from the .example)
.github/scripts/*.py               copy verbatim
```

---

## Status

Read this before adopting the lane. Two parts of it have never been run against
the real thing.

- **Untested against a real Apple account.** Every App Store Connect call in
  `ensure_profiles.py` and `verify_beta_ready.py` is exercised only against a
  scripted fake client. No request in this repo has been sent to Apple. Response
  shapes, error codes, and rate-limiting behaviour are taken from the API
  documentation and from the origin lane, not from observed traffic here.
- **The native `xcodebuild` archive path has never run anywhere.** The origin
  project was Flutter-only, so the entire `FLUTTER=0` branch — `xcodebuild
  archive`, `-exportArchive`, and the `CURRENT_PROJECT_VERSION` build-number
  handling that goes with it — is the least-proven code in the repo. It has not
  produced an `.ipa` on any machine.

The Flutter path, the keychain and certificate handling, and the guards
documented below come from a lane that did ship builds. Treat the first run of
this template as a test of it.

---

## What the org already provides

These are org-level secrets. Set them once on the organisation (`<YOUR_ORG>`);
every repo inherits them, and you do not create, copy, or reference anything
per-app:

| Secret | What it is |
| --- | --- |
| `APPLE_TEAM_ID` | the Apple Developer team |
| `APP_STORE_CONNECT_ISSUER_ID` | ASC API issuer |
| `APP_STORE_CONNECT_KEY_ID` | ASC API key id |
| `APP_STORE_CONNECT_P8_BASE64` | the ASC API `.p8`, base64 |
| `DIST_CERT_P12_BASE64` | the Apple Distribution certificate + key, base64 `.p12` |
| `DIST_CERT_PASSWORD` | that `.p12`'s password |
| `KEYCHAIN_PASSWORD` | password for the throwaway keychain this job creates |

**There is no `PROVISIONING_PROFILE_BASE64` and no `PROVISIONING_PROFILE_NAME`.**
The lane this came from had both, and that is the biggest change here: the app's
own provisioning profile is now **minted through the App Store Connect API at
build time**, exactly as that lane already did for its extension's profile. An
exported profile is a per-app credential, it goes stale, and a profile exported
against a *different* distribution certificate than the one in
`DIST_CERT_P12_BASE64` is precisely what broke the first real ship — with no
diagnostic beyond
`Command CodeSign failed`. A profile minted against the certificates the account
actually reports authorises the right one by construction.

Optionally set the repository **variable** `BUILD_OFFSET` if the app already has
builds in App Store Connect numbered above this repo's run number. The build
number is `BUILD_OFFSET + github.run_number`.

---

## Adopting it in a new app

### 1. Copy the files

Copy `.github/workflows/testflight.yml` and all of `.github/scripts/` into the
repo unchanged. Copy `.github/app-config.env.example` to
`.github/app-config.env`.

### 2. Fill in `.github/app-config.env`

Minimum for a **native iOS app** with `ios/` at the repo root:

```sh
APP_BUNDLE_ID=com.example.myapp
IOS_DIR=ios
FLUTTER=0
HAS_EXTENSION=0
SCHEME=MyApp
APP_PROFILE_NAME="My App App Store"
```

Minimum for a **Flutter app** in a monorepo:

```sh
APP_BUNDLE_ID=com.example.myapp
IOS_DIR=apps/mobile/ios
FLUTTER=1
HAS_EXTENSION=0
APP_PROFILE_NAME="My App App Store"
```

With an **app extension** (a Call Directory extension, say), add:

```sh
HAS_EXTENSION=1
EXTENSION_BUNDLE_ID=com.example.myapp.calldirectory
EXTENSION_PROFILE_NAME="My App Call Directory App Store"
APP_GROUP=group.com.example.myapp
EXTENSION_INFO_PLIST=ios/MyAppCallDirectory/Info.plist
EXTENSION_POINT_ID=com.apple.callkit.call-directory
```

Every key is documented in `.github/app-config.env.example`. Two rules worth
repeating:

- **Quote values containing spaces.** The file is `source`d by bash.
- **`APP_PROFILE_NAME` and `EXTENSION_PROFILE_NAME` are permanent.** They are
  what the lane looks profiles up by. Changing one after a successful run mints
  a *second* profile in the Apple account rather than renaming the first.

### 3. Configure signing in the Xcode project

The lane installs profiles and writes `ExportOptions.plist`; it does **not**
rewrite your project file. Set manual signing **per target** in Xcode:

| Build setting | Value |
| --- | --- |
| `CODE_SIGN_STYLE` | `Manual` |
| `CODE_SIGN_IDENTITY` (Release) | `Apple Distribution` |
| `DEVELOPMENT_TEAM` | your team id |
| `PROVISIONING_PROFILE_SPECIFIER` (Release) | exactly `APP_PROFILE_NAME`, and the extension target's exactly `EXTENSION_PROFILE_NAME` |

This is a real improvement over the lane this came from, and worth understanding:
there the profile name was a *secret*, so it could never be committed and had to
be threaded through the build as an environment variable. Here the name is chosen
by you, is not secret, and can live in the project file.

**Do not set `PROVISIONING_PROFILE_SPECIFIER` on the `xcodebuild` command line
(i.e. in `XCODEBUILD_EXTRA`).** A specifier there is global: it is applied to
CocoaPods dependency targets that "do not support provisioning profiles", and
the archive fails. Per-target, in the project file, is the only placement that
works with Pods.

### 4. Apple-side setup — the part this lane will not do for you

`ensure_profiles.py` mints profiles. It deliberately does **not** create bundle
ids, enable capabilities, add app groups, or delete anything from the account.
A profile minted against a bundle id that lacks the capabilities the entitlements
demand builds, signs, installs, and then fails on a device, silently. So the
script stops with an instruction instead. Before the first run:

1. **The bundle id must exist** in Certificates, Identifiers & Profiles —
   `APP_BUNDLE_ID`, and `EXTENSION_BUNDLE_ID` too when `HAS_EXTENSION=1`.
2. **If you set `APP_GROUP`:** enable the **App Groups** capability on *both*
   bundle ids **and actually attach each of them to that group**. Those are two
   separate actions in the portal, and doing only the first is the trap: Apple
   will mint a profile against a capability-enabled bundle id with no group
   attached, and that profile grants nothing while looking perfect. The lane
   checks the attachment before minting where Apple reports it, checks the
   minted profile's own entitlements immediately after, and refuses to reuse an
   existing profile that does not grant the group — so a half-done setup fails
   with an instruction rather than at CodeSign. Leave `APP_GROUP` empty if the
   app has no app group; empty disables the check, it does not mean "any group".
3. **The App Store Connect app record must exist.** It cannot be created through
   the API (`POST /v1/apps` returns 403). Without it the upload may succeed and
   the `Verify it became beta-ready` step will still fail, because it cannot find
   the app.
4. **A distribution certificate matching `DIST_CERT_P12_BASE64` must exist on the
   account**, because that is what the minted profiles are made to authorise.
5. **A TestFlight beta group with at least one tester.** Nothing in this lane
   assigns builds to a group. `verify_beta_ready.py` fails if the build lands in
   no group, because "Apple finished processing it" is a claim about Apple's
   pipeline, not about anyone's phone.

### 5. Ship

```sh
git push origin HEAD:ship/testflight
```

**Pushing that branch uploads a build to Apple.** That is the whole trigger —
there is no confirmation step. The branch is named to be unmistakable and is not
one anybody works on; you push to it only when you mean it.

There is also a `workflow_dispatch` trigger with a required "reason" input, but
GitHub only exposes `workflow_dispatch` for workflows on the **default** branch,
so until this file is merged to `main` the push branch is the only way to run it.

If `ship/testflight` is already taken in your repo, change it in
`.github/workflows/testflight.yml` under `on: push: branches:` — that one value
cannot come from the config file, because Actions cannot interpolate anything
into an `on:` trigger.

---

## What the lane does, in order

1. **Load the app configuration** — sources `.github/app-config.env`, validates
   it, derives `APP_DIR`, `KEYCHAIN_NAME`, `IPA_DIR`, `EXPORT_OPTIONS_PATH`, and
   exports everything. A missing `SCHEME` stops the job here, in seconds, rather
   than after a certificate is in the keychain and a profile is in the account.
2. **Select Xcode** — newest in `/Applications`, via `DEVELOPER_DIR` rather than
   `sudo xcode-select`, because this is a persistent machine and `xcode-select`
   changes would outlive the job.
3. **Flutter setup** (`FLUTTER=1` only), version from `FLUTTER_VERSION`, else
   `.mise.toml`, else latest stable.
4. **Dependencies** — `flutter pub get` when Flutter, `pod install` when there is
   a Podfile.
5. **Install the signing certificate** into a throwaway, run-unique keychain:
   WWDR intermediate (fatal, content-checked), Apple Root (best-effort),
   `security import -A`, `set-key-partition-list -S apple-tool:,apple:,codesign:`,
   set as default keychain with the previous default and search list saved.
6. **Prove the archive can sign, before anything is minted** — valid identity,
   WWDR present, and a **real test signature**. This runs *before* the mint on
   purpose: the mint writes to the org's Apple account, and the archive is where
   signing fails, and the mint cannot be moved after the archive because the
   archive needs the profiles.
7. **Mint or reuse the provisioning profiles** — the app's, and the extension's
   when `HAS_EXTENSION=1`. Installs both, records their paths for cleanup, writes
   `ExportOptions.plist` naming both.
8. **Cross-check each profile against the keychain identity** — the app's, then
   the extension's. The lane this came from checked one profile's certificates
   and the other profile's entitlements and never crossed them; that is what its
   second ship attempt died on.
9. **Extension point check** (`HAS_EXTENSION=1` and `EXTENSION_INFO_PLIST` set).
10. **Archive** — `flutter build ipa --release -v`, or `xcodebuild archive` +
    `-exportArchive`.
11. **Check the built artifact's versions agree** — read out of the real `.ipa`.
12. **Upload** with `altool`.
13. **Verify it became beta-ready** — polls until the build is in a beta group.
14. **Remove every trace** (`always()`): keychain, default keychain, search list,
    `.p12`, ASC key, both profiles.

## `ensure_profiles.py`: five properties — do not simplify these away

This script is the one thing in the lane that **writes to the org's Apple
account**. Each property below exists because its absence cost a real ship
attempt. A future generalisation that reads more cleanly and loses one of them
will cost a launch the first time an account is not in the state its author
imagined. If you touch this file, keep all five.

**1. Die on any non-200 from App Store Connect. Never read a failed lookup as
"nothing exists".**
*Dropped:* a 401, a scope 403 or a transient 500 on the profile lookup returns
"no profile found", which the caller reads as permission to mint — so a network
blip silently creates a **duplicate profile** in the account, and every later run
picks whichever one Apple returns first.

**2. Require `profileState == ACTIVE` *and* a matching `profileType`.**
*Dropped:* an `IOS_APP_DEVELOPMENT` profile that happens to carry the right name
gets reused for an App Store build. It fails much later, at signing, where the
cause is far harder to see than it is here.

**3. Reuse path — verify what the profile GRANTS, not what it is NAMED.**
*Dropped:* profiles capture entitlements **at creation**, so one minted before
the app group was attached stays broken forever while looking perfect in the
portal by name, state and type. Reusing it reproduces the original CodeSign
failure exactly, which reads as "the fix did not work" rather than as "you are
holding a stale profile".

**4. Mint path — verify what the bundle id ATTACHES, and what the minted profile
GRANTS. `APP_GROUPS` in the capability list is PRESENCE, not CONTENT.**
*Dropped:* the capability can be switched on with **no group attached at all**,
or with a **different** group attached, and Apple will happily mint a profile
that grants nothing useful. It has the right name, the right state, the right
type, it passes every metadata check in the file — and then fails at CodeSign
with a bare unhelpful error. This is property 3's mistake one level up, and it
cost multiple ship attempts on its own.
Three parts, all load-bearing: capabilities are attributed to *this* bundle id
(Apple's `filter[identifier]` is a **contains** match, so the **extension's**
enabled capability must never be allowed to vouch for the **app**); the specific
group is required to be attached wherever Apple reports the attachment; and —
because that API does **not** reliably report attached groups — the minted
profile's **own entitlements** are checked immediately after the mint, which is
the earliest place the truth can be known at all.

**5. Announce unverifiable checks. Never quietly downgrade to the weaker claim.**
*Dropped:* when Apple does not report attached groups, the check silently asserts
"the capability is enabled" while *claiming* "the group is attached" — and in a
green log that is indistinguishable from the strong property having held. The
script emits a `::warning::` saying **"This is NOT a pass"** and naming where the
property is actually established instead. Two relatives of the same principle
live here too: **never delete anything** from the account (the script tells a
human to delete a broken profile; it will not do it), and **parse the mint
response defensively** with a message saying the profile **MAY NOW EXIST** — a
`KeyError` traceback right after the one irreversible write invites a blind
re-run, which is how a second profile gets made.

## The other safety properties, and why each one is there

Every one of these is load-bearing too. They are kept verbatim from the lane
this came from because each records a failure that cost a real runner launch.

- **WWDR intermediate, fatal and content-checked.** Four archives died on
  `unable to build chain to self-signed root`. `security find-identity -v` said
  "1 valid identity" the whole time: its notion of valid is *narrower* than
  codesign's chain-to-trusted-root requirement, and believing one tool's word for
  the other's cost two launches. The Apple Root is best-effort at
  `https://www.apple.com/appleca/AppleIncRootCertificate.cer` (the `/appleca/`
  path — `/certificateauthority/` 404s for the root and cost a launch as a bare
  "exit code 56").
- **`security import ... -A`, not `-T /usr/bin/codesign`.** With `codesign:`
  already in the partition list and the keychain unlocked and default, the
  archive *still* failed with `errSecInternalComponent`. Acceptable because the
  keychain is created and destroyed inside this one job.
- **`set-key-partition-list -S apple-tool:,apple:,codesign:`.** Its absence is
  what actually failed: a list without `codesign:` grants access to everything
  except the one binary doing the signing.
- **The throwaway keychain becomes the *default* keychain**, previous default and
  search list saved and restored in `always()`. codesign resolves identities
  through the default keychain in a non-interactive session, and leaving a
  deleted keychain in a real user's search list is litter that outlives the job.
- **A real test signature**, not an assertion about the partition list. Asserting
  the known cause only guards the known cause; signing a throwaway Mach-O
  exercises the whole path and survives a cause nobody has met yet.
- **`shopt -s nullglob` and "exactly one `.ipa`".** Unmatched, an unresolved glob
  passes a literal `*.ipa` and the error is about a file by that name; matched
  twice, a checker reads only the first path and silently covers half of what it
  claims to.
- **`AuthKey_<id>.p8` written into `~/.appstoreconnect/private_keys` at
  `umask 077`.** `altool --apiKey` takes a key *id* and then looks for that file
  on disk. The lane this came from never wrote it anywhere, so its upload could
  not have worked — and would have failed at the very last step, after all the
  expensive work.
- **`-v` on the Flutter build.** Four runs died on a bare `Command CodeSign
  failed with a nonzero exit code` and nothing else, because Flutter swallows
  xcodebuild's output on failure. Do not remove it.
- **`always()` cleanup removing every artifact.** The failure paths are exactly
  the ones that strand things, and each removal is independent so none can abort
  the others. Only the exact recorded paths are removed, never a glob, because
  `~/Library/MobileDevice/Provisioning Profiles` holds real profiles too.

## Differences from the original lane

| | original | template |
| --- | --- | --- |
| App profile | `PROVISIONING_PROFILE_BASE64` secret | minted via ASC API |
| Profile names | secret | in `app-config.env`, committable into the pbxproj |
| Pre-mint guard | full check against the supplied app profile | identity + chain + **test signature** (no profile exists yet); the profile cross-check moved to just after the mint, for *both* profiles |
| `runs-on` | `[self-hosted, macOS, <per-app label>]` | `[self-hosted, macOS, ios]` |
| `environment:` | `testflight` | none (org secrets are inherited at repo level) |
| Working dir | hardcoded `apps/mobile` | `IOS_DIR` in config, `APP_DIR` derived from it |
| Keychain | one fixed `<app>.keychain` | `ci-<run id>.keychain`, or `KEYCHAIN_NAME` |
| Call Directory ruby step | generated the target into the pbxproj | **dropped** — repo-specific |
| Extension checks | always | only when `HAS_EXTENSION=1` |
| `check_extension_point.py` | hardcoded plist path and Call Directory value | `EXTENSION_INFO_PLIST` / `EXTENSION_POINT_ID`, and it **refuses to run without an expected value** rather than defaulting |
| `check_bundle_versions.py` | a missing `.appex` is always fatal | fatal only under `--expect-extension`, which the lane passes when `HAS_EXTENSION=1` |
| Build | Flutter only | Flutter *or* `xcodebuild archive` + `-exportArchive` |
| Installed profile filename | `<uuid>.mobileprovision` | `ci-<run id>-<app\|extension>-<uuid>.mobileprovision`, so it cannot collide with a profile the machine's owner already has |
| Bundle-id / app lookup | `data[0]` | exact-match filter — Apple's `filter[identifier]` is a *contains* match, so asking for `com.example.app` can answer with `com.example.app.extension` |
| App Group check on the mint path | `"APP_GROUPS" in capabilities` — presence only, over a capability list that could belong to a *different* bundle id | capabilities attributed to this bundle id; the **specific group** required to be attached where Apple reports it; the **minted profile's entitlements** checked straight after the mint; and an explicit `::warning::` when Apple reports nothing to check |

## Known limits

- **`CURRENT_PROJECT_VERSION` (native path).** The build number is passed as that
  build setting, which only becomes `CFBundleVersion` if the target's Info.plist
  says `$(CURRENT_PROJECT_VERSION)`. That is Xcode's default for projects created
  since Xcode 13 but is not universal. A project that ignores it fails at
  `check_bundle_versions.py` with the real number printed — not silently.
- **Extension version agreement is checked, not enforced.** Apple rejects an
  upload whose extension `CFBundleVersion` differs from its app's. The lane
  *detects* that in the built `.ipa`; keeping the two in step is the project's
  job (the lane this came from did it in a generator step that is deliberately
  not in this template).
- **No `-allowProvisioningUpdates`.** Deliberate: that flag lets Xcode create
  profiles in the account behind this lane's back.
- **The app-group entitlement check on the mint path can only be *pre*-mint when
  Apple reports the attachment.** The App Store Connect API does not reliably
  list which app groups are attached to a bundle id, so when it reports none the
  lane cannot distinguish "no groups attached" from "groups not reported". It
  says so with a `::warning::` and falls back to reading the **minted profile's**
  entitlements — which is correct but happens *after* the profile has been
  created. In that case you get a clear failure naming the profile and telling
  you to attach the group and delete it, not a prevented write. This limit is a
  property of Apple's API, not of the script; if a future API version exposes the
  attached groups, `_group_identifiers()` will pick them up and the check becomes
  fully pre-mint with no other change.
- **The ASC calls themselves are untested against Apple.** Every safety path in
  `ensure_profiles.py` was exercised against a scripted fake client, not a real
  account.
