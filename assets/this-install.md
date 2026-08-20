# This install

The one file that is specific to your deployment. **Fill it in when you set the
runner up** — everything else in the skill uses placeholders and needs no
editing.

Every row below is something that is obvious while you are installing and
unrecoverable six months later. The headings are prompts; replace the
placeholders with real values and delete the ones that do not apply.

Placeholders: `<HOSTNAME>`, `<VERSION>`, `<ADMIN>`, `<BUILD_USER>`,
`<RUNNER_NAME>`, `<LABEL>`, `<YOUR_ORG>`, `<OWNER>`, `<REPO>`, `<TEAM_ID>`,
`<KEY_ID>`, `<SHIP_BRANCH>`, `<N>`.

## Build host

| | |
|---|---|
| Machine | `<HOSTNAME>`, Apple Silicon, macOS `<VERSION>`, `<N>` GB RAM |
| Build account | `<BUILD_USER>` (standard user, **not** admin) |
| Admin account | `<ADMIN>` |
| Auto-login enabled | `<yes/no>` — if yes, `/etc/kcpassword` exists |
| Skill install | `~/.claude/skills/testflight-self-hosted` |

## Record the Xcode version

| | |
|---|---|
| Xcode | `<VERSION>` |
| iOS SDK | `<VERSION>` |
| Command line tools | `<VERSION>` |

When a commit builds on a laptop and fails on the runner, the toolchain is the
first thing you will want to compare, and **nobody remembers it after the
fact**. An Xcode upgrade on this machine changes every app's build at once, so
record the date of the upgrade too.

```bash
xcodebuild -version && xcrun --show-sdk-version
```

## Record which Apple team signs these apps

| | |
|---|---|
| Team name | `<TEAM_NAME>` |
| Team ID | `<TEAM_ID>` |
| Distribution certificate expires | `<DATE>` |
| App Store Connect key ID | `<KEY_ID>` |
| Key holder / who can revoke it | `<PERSON>` |

**Apple allows two active distribution certificates per team**, and revoking one
to make room breaks every other machine using it, with no notification. Knowing
which certificate this runner holds is what makes that decision safe.

Record the API key ID for the same reason in reverse: a key that has to be
revoked in a hurry cannot be revoked if nobody knows which of the team's keys is
the one in the secrets.

Record the certificate's expiry date explicitly. It expires silently and the
first symptom is a signing failure on a day you were shipping something else.

## Record which repos this runner serves

| Repo | Bundle id | Labels requested | Ship branch |
|---|---|---|---|
| `<OWNER>/<REPO>` | `<com.example.app>` | `<LABEL>` | `<SHIP_BRANCH>` |

| | |
|---|---|
| Registration scope | `<repo or org>` |
| Organization | `<YOUR_ORG>` |
| Runner name | `<RUNNER_NAME>` |
| Runner directory | `~/actions-runner` (in `<BUILD_USER>`'s home) |

**Every repo in this list can run code on this Mac as the account holding the
signing certificate.** That is the actual blast radius, and it is invisible from
any single repo. Nothing in a repo tells you what else shares its runner, and
this table is the only place the full list exists.

Confirm it rather than trusting the note:

```bash
gh api /orgs/<YOUR_ORG>/actions/runners \
  --jq '.runners[] | {name, status, ephemeral, labels: [.labels[].name]}'
```

**Every repo listed here must be private.** Record the check, not the intention:

```bash
gh api repos/<OWNER>/<REPO> --jq '.private'    # must be true
```

## Record who can push the ship branch

| | |
|---|---|
| Ship branch | `<SHIP_BRANCH>` |
| Ship gate posture | `<a hands-off / b required reviewer / c SHIP_ARMED>` |
| Approvers, if posture (b) | `<PERSON>, <PERSON>` |
| Who has write access to the repo | `<PERSON>, <PERSON>` |

**Anyone on the last row can ship to Apple under the team above**, unless
posture (b) is in force and actually enforced. Write it down: the list is small
and obvious today and grows without anyone deciding it should.

If posture (b) is chosen, record whether it was **verified to pause a real
run**. Environment protection rules on private repos require a paid GitHub
plan; on the free tier the setting is visible, saves, and does nothing — which
is worse than absent, because it reads as configured.

## Record the paths on the build host

| | |
|---|---|
| Runner (repo-scoped) | `~/actions-runner/` |
| Runner (org-scoped) | `~/actions-runner-org/` |
| LaunchAgent | `~/Library/LaunchAgents/actions.runner.<...>.plist` |
| Runner diagnostic logs | `~/actions-runner/_diag/` |
| Work directory | `~/actions-runner/_work/<repo>/` |

The `_diag` path matters when a job never starts: the GitHub side shows only
"Waiting for a runner", and the reason it declined the job is on this machine.

## Record the operating requirements you have set

**Sleep stops the runner**, and the GitHub side shows nothing but a queued job.

```bash
sudo pmset -a disablesleep 1
pmset -g live | grep SleepDisabled     # must read 1
```

It reads back only via `pmset -g live` — **not** `pmset -g custom` or plain
`pmset -g`, which do not show it at all. Looking in the wrong one produces a
confident, wrong "it is not set". Check the battery profile separately.

| | |
|---|---|
| `disablesleep` set | `<yes/no>` |
| Machine stays on AC | `<yes/no>` |
| Auto-login enabled | `<yes/no>` |
| Last verified after a reboot | `<DATE>` |

Record that last date. **A reboot without auto-login is a silent outage** —
jobs queue, nothing alerts, and the state looks identical to nobody having
pushed.

## Record what you measured

Fill in from this machine rather than copying anyone else's numbers.

| | |
|---|---|
| First run, cold pods, profile minted | `<N>` min |
| Steady-state run, warm | `<N>` min |
| Peak memory pressure during a run | `<level>` |
| Two apps building back to back | `<N>` min total |

The steady-state number is the one that decides whether a second app is
comfortable here or merely possible.

## Per-app quirks

Record anything an app needs that a clean checkout does not have.

| App | Quirk |
|---|---|
| `<REPO>` | `<e.g. App Group attached to the identifier on DATE>` |
| `<REPO>` | `<e.g. BUILD_OFFSET set to N after repo transfer>` |

Two that recur:

**App Group attachment.** Enabling the capability and attaching the specific
group are two separate portal actions. Record the date both were done — a
profile minted between them is permanently missing the entitlement while
continuing to look correct in the portal.

**Build number floor.** Record the highest build number Apple has accepted for
each app. Apple rejects anything at or below it, at the end of the run, after
the archive.

## Known local issues

Record the ones this install has, with the workaround actually in use. An issue
recorded here is one the next person does not rediscover at 2am.

- `<issue>` — `<workaround>`, since `<DATE>`
