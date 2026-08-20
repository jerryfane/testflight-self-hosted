# One runner and one set of credentials for every app

Do this once, and every later app costs a bundle id and a config file. Roughly
20 minutes, plus a browser step that has no CLI equivalent.

Placeholders: `<YOUR_ORG>`, `<BUILD_USER>`, `<RUNNER_NAME>`.

> **The per-app setup cost is the thing being removed here.** A repo-scoped
> runner and repo-scoped secrets work perfectly for one app and then have to be
> redone, identically, for the second — including re-uploading the same
> distribution certificate, which is the step most likely to be done wrong the
> second time because the first one's value cannot be read back to compare.
>
> Org-level runners serve **every** repo in the org. Org-level secrets are
> inherited by every repo in the org. After this, adopting a new app touches
> nothing at this layer.

## What you are building

An organization that owns one runner registration and seven shared secrets. The
apps stay in their own repos and change nothing about how they are developed.

## 0. Prerequisites

- A working runner from `setup-runner.md`, with its session rules understood
- `gh`, authenticated as the account that will own the org
- The original credential files: the `.p8` key, the `.p12` certificate, and the
  passwords for both. **GitHub secrets are write-only** — you cannot read an
  existing repo secret back to copy it up to the org, so if the originals are
  gone, they have to be re-exported from Apple before you start

## 1. Create an organization

**A personal user account cannot own org-level runners or org-level secrets.**
That is the whole reason for this step. The free tier is enough.

Org creation is **not exposed by the REST API** for normal accounts, so this
one step is a browser step and there is no way around it:

```
https://github.com/organizations/new
```

Choose the free plan. Then move or create the app repos under `<YOUR_ORG>`
(`gh repo transfer` or the repo's Settings page). A repo left under your
personal account inherits none of what follows, and the symptom is a workflow
that cannot find a secret that visibly exists.

## 2. Grant `gh` the org scopes

```bash
gh auth refresh -h github.com -s admin:org
```

**This is an interactive device-code flow and needs a real terminal** — it
prints a code and waits for you to enter it in a browser. It cannot be run from
a script, a hook, or an agent's non-interactive shell; there it hangs until it
is killed, which reads as a network fault.

Without `admin:org`, every command below fails with `404`, not `403`. GitHub
hides resources you cannot see rather than admitting they exist, so a scope
problem looks exactly like a typo in the org name.

## 3. Register the runner at org level

Give it its **own directory**. A runner directory holds one registration; a
second `config.sh` in the same directory overwrites the first, and you silently
lose the repo-scoped runner you were keeping as a fallback.

In `<BUILD_USER>`'s GUI login session — the same Aqua requirement as
`setup-runner.md`, for the same keychain reason:

```bash
cp -R ~/actions-runner ~/actions-runner-org && cd ~/actions-runner-org
rm -f .runner .credentials .credentials_rsaparams
```

Mint the token as your admin account and use it immediately:

```bash
gh api -X POST /orgs/<YOUR_ORG>/actions/runners/registration-token --jq .token
```

```bash
./config.sh --unattended \
  --url https://github.com/<YOUR_ORG> \
  --token <TOKEN> \
  --name <RUNNER_NAME> \
  --labels self-hosted,macOS,ios \
  --replace
./svc.sh install && ./svc.sh start
```

**The URL is the org, not a repo.** A token minted at org level against a repo
URL fails with a message about the token being invalid, which sends people back
to mint another token instead of fixing the URL.

Two runners on one Mac coexist fine — separate directories, separate
LaunchAgents, separate labels if you want them routed differently. They share
one machine, so they still queue against each other in practice.

By default a new org runner joins the `Default` runner group, which is
available to all repositories in the org. Confirm that is what you want in
Settings → Actions → Runner groups before pointing a repo at it.

## 4. Set the shared secrets once

Of the nine secrets a TestFlight lane typically wants, **seven are Apple
*team*-scoped, not app-scoped**, and belong here:

| Secret | What it is |
|---|---|
| `APPLE_TEAM_ID` | The 10-character team identifier |
| `APP_STORE_CONNECT_ISSUER_ID` | API key issuer, one per team |
| `APP_STORE_CONNECT_KEY_ID` | API key id |
| `APP_STORE_CONNECT_P8_BASE64` | The `.p8` private key, base64 |
| `DIST_CERT_P12_BASE64` | Distribution certificate + key, base64 |
| `DIST_CERT_PASSWORD` | Password used when exporting that `.p12` |
| `KEYCHAIN_PASSWORD` | Password for the throwaway per-job keychain |

**One distribution certificate signs every app on the team.** This is the fact
that makes the whole arrangement work, and it is the one most often
disbelieved — people export a second certificate per app, hit Apple's limit of
two active distribution certificates, and revoke one that other machines were
using.

```bash
base64 -i AuthKey_XXXXXXXXXX.p8 | gh secret set APP_STORE_CONNECT_P8_BASE64 \
  --org <YOUR_ORG> --visibility private
base64 -i distribution.p12 | gh secret set DIST_CERT_P12_BASE64 \
  --org <YOUR_ORG> --visibility private
gh secret set APPLE_TEAM_ID --org <YOUR_ORG> --visibility private
```

Repeat for the rest. `gh secret set NAME` with no value reads from stdin, so
piping keeps values out of argv and out of shell history.

**Use `--visibility private`, not `--visibility all`.** `private` covers every
private repo in the org, which is every repo that should ever build. `all`
additionally exposes the signing certificate to public repos in the org, and a
public repo is precisely where a fork PR can run attacker-authored workflow
steps. `private` means that combination cannot arise even by accident when
somebody creates a public repo in the org next year.

Verify names, not values — values are unreadable by design:

```bash
gh secret list --org <YOUR_ORG>
```

## 5. The two per-app secrets that no longer exist

The remaining two, `PROVISIONING_PROFILE_BASE64` and
`PROVISIONING_PROFILE_NAME`, are **eliminated entirely**. The lane mints and
reuses profiles through the App Store Connect API at build time using the key
above.

That is not a convenience. A profile pasted in as base64 is a file that expires
in a year, that captures its entitlements at creation, and that nothing in the
repo can tell you is stale — the portal keeps showing it as valid while the
build keeps failing to sign. **A minted profile makes staleness impossible
rather than detectable.**

So an app needs a bundle id, and nothing else, at this layer.

## Verify

```bash
gh api /orgs/<YOUR_ORG>/actions/runners \
  --jq '.runners[] | {name, status, ephemeral, labels: [.labels[].name]}'
gh secret list --org <YOUR_ORG>
```

`status` online, `ephemeral` null or false, seven secrets listed.

Then the adversarial part — **all three must fail**:

```bash
gh secret list --org <YOUR_ORG> --json name,value          # no value field exists
gh api /orgs/<YOUR_ORG>/actions/secrets/APPLE_TEAM_ID --jq .value   # null
gh api /orgs/<YOUR_ORG>/actions/runners -H 'Authorization: Bearer bad'  # 401
```

The first two failing is the point: if you can read a secret back, so can
anything else holding your token.

Last, prove inheritance from a repo that has no secrets of its own. A repo in
the org with an empty `gh secret list` still builds, because the org values
arrive at job time. If a workflow reports a missing secret in a repo whose
`gh secret list --org` shows it, the repo is not in `<YOUR_ORG>`, or the runner
group does not include that repo.

## Known limits

**Secrets cannot be read back.** Rotation means having the originals. Keep the
`.p12`, the `.p8` and both passwords somewhere you control, or accept
re-exporting from Apple at the worst possible moment.

**Org-level means every repo in the org.** Visibility `private` is a coarse
control. If the org ever holds a repo whose contributors should not be able to
ship under this Apple identity, `--visibility selected` with an explicit repo
list is the only mechanism that expresses that.

**One runner group, one machine.** Runner groups can restrict which repos reach
which runners, but they do not isolate the jobs: everything still runs as
`<BUILD_USER>` on one Mac, with one login keychain.

**Transferring a repo does not move its secrets.** Repo-level secrets set
before the move survive the move and then **shadow the org values silently**.
After transferring, run `gh secret list --repo <YOUR_ORG>/<REPO>` and delete
the duplicates, or you will debug an org secret that is not the one being used.

**API keys are team-wide.** The App Store Connect key here can act on every app
on the team. Scope it to the smallest role that can upload builds, and record
which key is in use in `assets/this-install.md` so it can be revoked without
guesswork.
