# testflight-self-hosted

An [Agent Skill](https://agentskills.io/specification) for shipping iOS apps to
TestFlight from a GitHub Actions self-hosted runner on your own Mac — unattended,
with no per-app credentials.

Uploading to TestFlight means code signing, and code signing means a Mac holding
your distribution certificate. Running that job on hardware you already own is
easy to configure and surprisingly hard to make work: the runner registers, the
job starts, the app compiles, and then `codesign` fails with eight words that
name nothing at all.

The cause is usually not the keychain. **A runner started the ordinary way sits
in a launchd session with no access to the Security Server**, and no certificate,
partition-list or profile fix will help until that changes. This repository is
the working configuration, and — more usefully — the list of what each opaque
failure actually means.

## Install

**On the Mac** (for the agent operating the build box):

```bash
git clone <this-repo> ~/.claude/skills/testflight-self-hosted
```

The directory name must be `testflight-self-hosted` — the spec requires `name`
in `SKILL.md` to match the parent directory.

**For a remote agent**, copy the directory into the repo that agent works in, so
it travels with the code:

```bash
cp -R testflight-self-hosted <consuming-repo>/skills/
```

## Layout

```
SKILL.md                      router + reference-loading table
references/
  gotchas.md                  what each opaque signing failure actually means
  setup-runner.md             a standing runner that can code-sign
  setup-org.md                one runner and one credential set for N apps
  adopting-the-lane.md        wiring the workflow into an app repository
  security-posture.md         what stands between a push and an upload
workflow/                     the lane itself — the skill ships what it documents
  testflight.yml              sign · mint · archive · upload · clean up
  app-config.env.example      the single per-app config file
  scripts/                    App Store Connect client, guards, checkers
scripts/
  install-runner.sh           host-side installer
assets/
  this-install.md             the only deployment-specific file
```

## Retargeting

Everything is written with `<PLACEHOLDER>` values. `assets/this-install.md` is
the one file you fill in — the build account, the Apple team, which repositories
the runner serves, and who can push a branch that ships.

Per app, `workflow/app-config.env.example` becomes `.github/app-config.env` and
holds four required keys. There are no per-app secrets: the lane mints its own
provisioning profiles through the App Store Connect API, so a new app needs a
bundle identifier and nothing else.

## Quick check

```bash
launchctl managername      # Aqua = can sign.  Background = cannot.
```

Run it in the same context the runner runs in. If it prints `Background`, that
is the whole bug, whatever else the log says.

## Known issues

Stated plainly, because a skill that hides its own state is worse than one with
gaps.

**The lane is untested against a real Apple account.** Every App Store Connect
call in `workflow/scripts/` — minting, reuse, bundle-id lookup, certificate
listing, the beta-ready poll — is exercised against a fake client only. No JWT
has been signed against a live account by this code.

**The native `xcodebuild` archive path has never run anywhere.** The lane this
was generalised from was Flutter-only. That branch is the least-proven code in
the repository and the most likely first failure.

**The pre-mint App Group attachment check cannot always be made.** Apple does
not reliably report which groups are attached to a bundle identifier. Where it
does not, the lane emits a warning that explicitly states it is not a pass and
defers the property to a post-mint check against the profile's own entitlements.
That is honest, not equivalent.

**Version agreement between app and extension is checked, not enforced.** The
lane reads the built artifact and fails on a mismatch; it does not set the
values.

**`CURRENT_PROJECT_VERSION` only reaches `CFBundleVersion` if the Info.plist
references it.** A native project that hardcodes the version will ignore the
computed build number, and Apple will reject the upload for not incrementing.

The five properties documented in `workflow/README.md` under *"do not simplify
these away"* each carry a one-line consequence. They look like defensive
verbosity and are not; every one of them is a failure that happened.

## Origin

Extracted from a real first ship — eight attempts, six of which failed at the
same step with the same eight words. `references/gotchas.md` is the part worth
reading even if you never install this: a signing error that is really a login
session, a warning mistaken for an error for two attempts, an upload rejected by
a plausible extension-point identifier that does not exist, a provisioning
profile that captured stale entitlements and stayed broken while looking
perfect, and the recurring cost of checking that something is present when what
mattered was what it contained.

Sibling skill: [mac-build-gate](https://github.com/jerryfane/mac-build-gate) —
the pre-merge, unsigned, simulator half. It answers *did it build and what does
it look like*; this one answers *ship it*.

## Licence

MIT.
