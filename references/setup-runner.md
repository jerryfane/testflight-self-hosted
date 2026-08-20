# Setting up a standing self-hosted runner on a Mac

First-time install. Roughly 30 minutes, most of it download and one reboot.

Placeholders: `<BUILD_USER>` (a dedicated non-admin macOS account, e.g.
`builder`), `<ADMIN>` (your admin account), `<OWNER/REPO>`, `<RUNNER_NAME>`,
`<LABEL>`.

> **The runner has to run inside `<BUILD_USER>`'s GUI login session — the
> launchd `Aqua` domain.** Nothing else in this document matters as much. In
> any other domain `codesign` cannot reach the keychain and the build dies with
> `errSecInternalComponent`, no matter how the keychain is created, unlocked,
> or partitioned.
>
> That error names nothing, so it gets read as a bad certificate, a locked
> keychain, or a missing `-A`. Whole days go into repairing a keychain that was
> never broken. The keychain is fine; the session is wrong.
>
> Everything below follows from that one requirement: a LaunchAgent rather than
> a LaunchDaemon, an interactive login rather than ssh, auto-login to survive a
> reboot, and Switch User rather than Log Out.

## What you are building

One always-on runner, owned by a non-admin account, that signs and uploads iOS
builds. It is *standing*, not ephemeral: it serves job after job, for every
repo you point at it, and it is online whether or not you are watching.

**A standing runner is a different security object from an on-demand one.** An
ephemeral runner you start by hand is gated by your attention. A standing one
picks up the oldest matching queued job by itself, seconds after that job is
queued. Read `security-posture.md` before you point a repo at it — in
particular the part about withholding the runner no longer being a gate.

## 0. Prerequisites

- Apple Silicon Mac with **Xcode installed and its licence accepted**
- macOS admin account (`<ADMIN>`) that can `sudo`
- `gh`, authenticated as a user with admin rights on `<OWNER/REPO>`
- A private repo. **Never attach a self-hosted runner to a public repo** —
  see `security-posture.md`; this one is not negotiable

> ### The build account must never hold a GitHub token
>
> Every `gh` command here runs as `<ADMIN>`. The runner authenticates with its
> own registration credential, written by `config.sh` into the runner
> directory, and that credential is scoped to taking jobs — it cannot act as
> you against other repos.
>
> A `gh` login inside `<BUILD_USER>` can, and that account is the one executing
> whatever a workflow file says. The account holds your signing identity; it
> does not also need to hold your GitHub identity.

## 1. Create the build account

In **Terminal**, not through an agent — `sudo` needs a real TTY:

```bash
sudo sysadminctl -addUser <BUILD_USER> -fullName "Build" -password -
dscl . -read /Groups/admin GroupMembership
```

`-password -` prompts interactively so the password stays out of shell history.
**Do not pass `-admin`.** The second command must not list `<BUILD_USER>`.

You will need that password again in step 7, so choose it deliberately rather
than generating something you cannot retype.

## 2. Log in graphically as `<BUILD_USER>`

Use **fast user switching** from the menu bar. Not ssh, not `sudo -u`, not
Screen Sharing into your own session — an actual login at the login window.

**This is the step people skip, and it invalidates everything after it.** The
runner service inherits the session it is installed from. Install it from a
non-GUI shell and it lands in a domain with no keychain access, and the failure
surfaces much later as a signing error in a workflow log.

Run the rest of this document in Terminal **inside that session**.

## 3. Prove the session is Aqua before going further

```bash
launchctl managername
```

It must print `Aqua`. Thirty seconds here saves a long debugging session later.

For calibration, all of these print `Background` and are therefore all useless
for installing the runner:

```bash
ssh <BUILD_USER>@localhost 'launchctl managername'   # Background
sudo -u <BUILD_USER> bash -lc 'launchctl managername'  # Background
```

The one non-interactive form that reaches the right domain is
`launchctl asuser <uid> …`, which is what `scripts/install-runner.sh` uses.
**`sudo -u` is not a substitute for `launchctl asuser`** — it changes the user
and leaves the session alone, which is exactly the half of the problem that
does not matter.

## 4. Download the runner

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
ver=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
  | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')
curl -fsSL -o runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${ver}/actions-runner-osx-arm64-${ver}.tar.gz"
tar xzf runner.tar.gz && rm runner.tar.gz
```

The runner unpacks in place and is self-contained. Keep one directory per
registration target — a repo-scoped runner and an org-scoped runner cannot
share a directory, because `config.sh` writes one `.runner` file per directory
and the second registration overwrites the first.

## 5. Register, non-ephemeral

Mint the token as `<ADMIN>` and use it immediately:

```bash
gh api -X POST repos/<OWNER/REPO>/actions/runners/registration-token --jq .token
```

```bash
cd ~/actions-runner
./config.sh --unattended \
  --url https://github.com/<OWNER/REPO> \
  --token <TOKEN> \
  --name <RUNNER_NAME> \
  --labels <LABEL> \
  --replace
```

**Registration tokens expire in about an hour and are single-use.** Never write
one into a document, a note, or a commit — by the time anyone reads it, it is
either dead or already spent, and a dead token produces a `404` from
`config.sh` that reads like a wrong repo name. Mint it inline, every time.

**Omit `--ephemeral`.** An ephemeral runner de-registers itself after exactly
one job, which looks identical to a crash: the job goes green, and the next
push queues forever against a runner that no longer exists. Omitting the flag
is what makes this runner standing.

`--replace` lets you re-run this against an existing registration of the same
name without first deleting it in the web UI.

`--labels` is how workflows select this machine. The shipped lane requests
`[self-hosted, macOS, ios]`, so `<LABEL>` must include `ios` for that lane to
land here. Prefer capability labels (`ios`, `xcode26`) over per-app ones — a
per-app label means a second app needs a second registration for no reason.

## 6. Install and start the service

```bash
./svc.sh install
./svc.sh start
./svc.sh status
```

On macOS `svc.sh install` writes a **LaunchAgent** to
`~/Library/LaunchAgents/actions.runner.*.plist` and `launchctl load -w`s it
into the per-user aqua domain. That is precisely the behaviour you want, and it
is the reason this guide uses `svc.sh` rather than a hand-written plist.

**Do not convert it to a LaunchDaemon.** A daemon starts at boot without a
login, which is the property that makes it tempting, and it has no GUI session,
which is the property that makes `codesign` fail.

**`svc.sh` refuses to run under `sudo`.** It checks for uid 0 and exits. This
is not an obstacle to work around with `RUNNER_ALLOW_RUNASROOT`; it is the
script enforcing the same session rule this whole document is about. Run it as
`<BUILD_USER>`, in `<BUILD_USER>`'s own login.

## 7. Survive reboots: enable auto-login

A LaunchAgent runs only while its user is logged in. After a reboot, with
nobody logged in, the runner is offline and every queued job waits.

```bash
sudo sysadminctl -autologin set -userName <BUILD_USER> -password -
```

Or System Settings → Users & Groups → "Automatically log in as".

**This writes `/etc/kcpassword`, which holds the account password obfuscated —
not encrypted.** Anyone with root, or with the disk, recovers it trivially. Say
that out loud before enabling it rather than discovering it in an audit.

The tradeoff is acceptable here for a narrow reason: `<BUILD_USER>` is a
non-admin sandbox whose password unlocks nothing else. It is not acceptable if
the build account is ever made an admin, or if it shares a password with any
other account. If you will not accept it, the alternative is switching into the
build account by hand after every reboot, and accepting that the runner is
offline until you do.

## 8. Switch users, never Log Out

Switch back to `<ADMIN>` with **fast user switching**. Both sessions stay
alive.

**Logging the build account out kills its LaunchAgents and takes the runner
offline**, and GitHub will show it as offline with no other explanation. This
is the single most common way a working install stops working: someone tidies
up by logging out.

## Verify

From `<ADMIN>`, confirm it is registered, online, and *standing*:

```bash
gh api repos/<OWNER/REPO>/actions/runners \
  --jq '.runners[] | {name, status, ephemeral, labels: [.labels[].name]}'
```

**Both fields matter.** `status` must be `"online"` and `ephemeral` must be
`null` or `false`. An ephemeral runner passes the online check while still
being wrong, and it disappears after the first job.

In the build account's session, confirm launchd holds it:

```bash
launchctl list | grep actions.runner
launchctl managername      # Aqua
```

Then the adversarial part — **all three must fail**:

```bash
sudo -u <BUILD_USER> cat ~<ADMIN>/.ssh/id_ed25519     # permission denied
sudo -u <BUILD_USER> -H gh auth status                # no token in that account
dscl . -read /Groups/admin GroupMembership | grep <BUILD_USER>   # no match
```

Finally, prove it is standing rather than ephemeral: run one workflow, let it
finish, and re-run the `gh api` command above. **Still `online` after a
completed job is the whole difference.** A registration that vanishes there was
configured with `--ephemeral`.

## Known limits

**Sleep takes the runner offline.** Check `pmset -g live` for
`SleepDisabled 1`; plain `pmset -g` and `pmset -g custom` do not show it, so
looking in the wrong place yields a confident, wrong answer. Also check the
battery profile separately.

**One machine is one queue.** `concurrency` in the workflow serialises, but two
repos pushing at once still contend for the same Xcode, the same derived data,
and the same login keychain. Watch wall-clock time before adding a third app.

**A reboot without auto-login is a silent outage.** Nothing alerts; jobs queue.
The runner list is the only place it shows.

**Runner updates need the service restarted.** The runner self-updates its
binaries, but a macOS upgrade or an Xcode upgrade can leave it running against
a toolchain it has not seen. After either, run `./svc.sh stop && ./svc.sh
start` from the build account's GUI session — and from nowhere else.

**Anything that can queue a job on this label runs code on this Mac.** Labels
are a routing mechanism, not a permission. The permission boundary is the
account and the repo's privacy setting.
