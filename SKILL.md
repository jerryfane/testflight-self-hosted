---
name: testflight-self-hosted
description: Ship iOS apps to TestFlight from a GitHub Actions self-hosted runner on your own Mac, unattended. A standing runner inside the build account's login session signs with your distribution certificate and mints its own provisioning profiles, so an app needs a bundle id and no per-app credentials. Use when setting up a self-hosted runner that has to code-sign, when a build fails with errSecInternalComponent or an opaque CodeSign error, when an upload is rejected by App Store Connect, when sharing one Apple team's credentials across several app repositories, or when deciding how much human approval should stand between a push and an upload.
license: MIT
compatibility: Build host requires macOS with Xcode, an Apple Developer account, and a dedicated non-admin account that can log in. Repositories must be private.
metadata:
  version: "0.1"
---

# TestFlight from a self-hosted Mac

Uploading to TestFlight means signing, and signing means a Mac with your
distribution certificate on it. Hosted CI can do this — you upload the
certificate to the CI provider and pay per minute. The alternative is to run the
job on a Mac you already own.

That part is easy to set up and surprisingly hard to make work. The runner
registers, the job starts, the app compiles, and then `codesign` fails with
eight words that name nothing. The reason is almost never the keychain: **a
runner started the ordinary way is in a launchd session that cannot reach the
Security Server at all.**

This skill is the working configuration and, more usefully, the list of what
each opaque failure actually means.

```
    push to <SHIP_BRANCH>
             │
             ▼
    GitHub queues the job
             │
             ▼
    standing runner in the build account's AQUA session   ← the part that matters
             │
             ├── import cert into a throwaway keychain
             ├── prove the archive can sign          (before anything irreversible)
             ├── mint or reuse provisioning profiles (App Store Connect API)
             ├── archive · export
             ├── upload to App Store Connect
             └── remove every trace from the machine (always)
             │
             ▼
        build in TestFlight
```

## Which half are you?

| You are | Start with |
|---|---|
| Standing up a Mac to build for the first time | **[setup-runner.md](references/setup-runner.md)** |
| Adding a second, third, tenth app | **[setup-org.md](references/setup-org.md)** |
| Adding the lane to one app repository | **[adopting-the-lane.md](references/adopting-the-lane.md)** |
| Staring at a signing failure right now | **[gotchas.md](references/gotchas.md)** |

## Reference loading guide

Load a reference if there is any chance its content applies. Missing a gotcha
here costs a signed archive at best and a stray artifact in someone's Apple
developer account at worst.

| Reference | Load when |
|---|---|
| **[gotchas.md](references/gotchas.md)** | **Any signing or upload failure**; `errSecInternalComponent`; a bare `Command CodeSign failed`; an App Store Connect rejection; anything where a check passed and the thing it checked was still broken |
| **[setup-runner.md](references/setup-runner.md)** | Installing the runner; it registers but jobs fail to sign; it disappears after one job; it does not survive a reboot |
| **[setup-org.md](references/setup-org.md)** | More than one app; sharing one Apple team's credentials; deciding what belongs at organisation level rather than per repository |
| **[adopting-the-lane.md](references/adopting-the-lane.md)** | Wiring the workflow into an app; filling in `app-config.env`; the Apple-side prerequisites the lane deliberately will not create for you |
| **[security-posture.md](references/security-posture.md)** | Before making a runner permanent; deciding whether a push should ship without approval; any question about public repositories |

## The fastest thing you can do

```bash
python3 .github/scripts/preflight.py --repo OWNER/REPO --user <BUILD_USER>
```

Ten seconds, read-only, and it asks every question a fifteen-minute build would
have answered the expensive way: is the session right, is the runner standing and
labelled to match `runs-on`, is the repository private, are all seven secrets
present, does the bundle id exist, is the App Group attached, will the build
number be accepted. It prints the fix beside each failure and changes nothing.

Anything it could not check reports `SKIP` with a reason. **A SKIP is not a
pass** — that distinction is the whole design, and it is why the signing probe
deliberately reports SKIP rather than green: the identity lives in a keychain
that only exists during a job.

If you have not adopted the lane yet, the one question that matters most is:

```bash
launchctl managername      # Aqua = can sign.  Background = cannot, whatever else is true.
```

Run it in the same context the runner runs in. If it prints `Background`, stop
reading everything else and fix that first — no keychain, certificate or profile
change will make signing work from there.

`assets/this-install.md` is the only file that is specific to one machine. Fill
it in when you set the box up.

## Rules that are not negotiable

**Private repositories only.** A self-hosted runner executes workflow files that
contributors can edit, on your machine, as the account holding your signing
identity. On a public repository that is a stranger's code with your certificate
in reach.

**Verify content, not presence.** A capability being enabled is not a group
being attached. A profile being `ACTIVE` is not its entitlements being current.
`find-identity` calling an identity valid is not `codesign` being able to use
it. Six of the thirteen gotchas in this skill are this single mistake wearing
different clothes.

**Put every checkable precondition before the first irreversible step.** Minting
a provisioning profile writes to an Apple developer account and cannot be undone
from CI. A lane that mints and then discovers it cannot sign has done permanent
work to earn a failure it could have had for free.

**Withholding the runner is not a gate once the runner is standing.** A
permanent runner takes the oldest matching queued job, seconds after it appears.
Any safety that rested on "no runner is online" evaporates the moment one always
is, and nothing announces the change.

## What this cannot do for you

It does not create bundle identifiers, enable capabilities, attach App Groups,
or issue certificates. Those are one-time actions in the Apple Developer portal,
and the lane refuses with instructions rather than guessing — creating things in
someone's developer account is not a decision a CI job should make.

It does not review a build. It puts one in TestFlight; whether the build is
worth installing is a separate question, and mac-build-gate is the sibling skill
for looking at a build before it gets this far.

## Status

**The lane is untested against a real Apple account.** Every App Store Connect
call in `workflow/scripts/` is exercised against a fake client only. The
**native `xcodebuild` archive path has never run anywhere** — the lane this came
from was Flutter-only, so that branch is the least-proven code here.

The *configuration* in `references/` and everything in `gotchas.md` is the
opposite: all of it was established by shipping a real build, and most of it by
failing to, repeatedly, first.

`workflow/scripts/preflight.py` is also tested — against a live runner, both
repo- and organisation-scoped, with negative cases (missing repository, unknown
build account, no credentials) and with the macOS checks forced off to confirm
they report `SKIP` rather than passing quietly.

See `## Known issues` in the README.
