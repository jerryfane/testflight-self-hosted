# Security posture: what this arrangement actually costs

Read this before the first repo is pointed at the runner, not after. Every
tradeoff here is real and stated plainly; none of them is presented as solved.

Placeholders: `<BUILD_USER>`, `<ADMIN>`, `<OWNER/REPO>`, `<SHIP_BRANCH>`.

## What you have built, stated honestly

A Mac that is always online, holding a certificate that can sign software as
your Apple team, wired to execute whatever a file in a git repo says. **The
security boundary is the account and the repo's privacy setting.** It is not
the network, not the labels, and not anyone's intention to be careful.

## Private repos only. This one is a hard rule.

**A self-hosted runner on a public repo means any fork pull request can run
arbitrary code on your Mac, as the account holding your signing identity.**

The mechanism is not exotic and needs no vulnerability. A stranger forks the
repo, edits the workflow in the fork, opens a pull request, and GitHub offers
that job to your runner. GitHub's own documentation says not to do this. The
attacker's code runs as `<BUILD_USER>`, which can read the keychain the lane
built, read the decoded `.p8`, and sign anything.

There is no configuration that makes this safe. Approval-required-for-fork-PRs
narrows the window and depends on a human never clicking approve on a
plausible-looking contribution. **If a repo must be public, it does not use
this runner.** Move the lane to a private mirror, or use GitHub-hosted runners
and accept their constraints.

Check it, do not assume it:

```bash
gh api repos/<OWNER/REPO> --jq '.private'    # must be true
```

## Withholding the runner is not a gate once the runner is standing

This is the trap people walk into at exactly the moment they make the runner
permanent, and it deserves stating flatly.

**A standing runner picks up the oldest matching queued job automatically.**
Not when you look. Not when you start it. Seconds after the job is queued.

While the runner was started by hand, "it only ships when I turn the runner on"
was a real gate, and it was doing more work than anyone noticed: it caught
accidental pushes, it caught a workflow that triggered on the wrong branch, and
it caught the run that was queued while everybody was asleep. Making the runner
permanent removes that gate silently. Nothing announces it, and the queue that
used to hold jobs now drains instantly.

Every safety that depended on the runner being offline evaporates the moment it
is always online. If that gate mattered, replace it deliberately with one of
the three postures below.

## The ship gate: three postures, chosen on purpose

By default, a push to `<SHIP_BRANCH>` signs and uploads with no human step.
That is a legitimate choice; so are the others. Choose one deliberately rather
than inheriting the default.

**(a) Fully hands-off.** Push ships. Fastest, and the only one with no friction
on a good day. The cost: anything that can push to `<SHIP_BRANCH>` can ship
under your Apple identity — a merged PR, a stale local branch pushed by
mistake, a script, an agent with repo write access. There is no undo; a build
uploaded to Apple is uploaded.

**(b) A required-reviewer rule on the deployment environment.** Add
`environment: testflight` to the upload job and configure a required reviewer
on that environment. The run reaches the gate, pauses, and waits for one
approval click.

This is the only posture **enforced by GitHub rather than by convention**. It
holds when branch protection is misconfigured, when someone force-pushes, when
a workflow is edited, and when every local habit fails, because the enforcement
is on the deployment rather than on the branch. It is the right default for
anything shipping to real testers.

**Note that environment protection rules on private repos require a paid GitHub
plan.** On the free tier the setting is visible and does not apply, which is
worse than absent, because it reads as configured. Verify that a run actually
pauses before relying on it.

**(c) An armed switch on the upload job.** Guard the upload with
`if: vars.SHIP_ARMED == '1'` and set the variable when you mean to ship:

```bash
gh variable set SHIP_ARMED --repo <OWNER/REPO> --body 1     # arm
gh variable set SHIP_ARMED --repo <OWNER/REPO> --body 0     # disarm
```

Deliberate and auditable: the variable change is recorded, and the build still
runs and still proves it compiles while the upload is skipped. Its weakness is
that it is a *mode*, and modes get left on. Anyone with repo write access can
also flip it, so it is a guard against accident, not against intent.

**(b) and (c) compose.** Arming is the intent, approval is the enforcement.

## What the build account should and should not hold

**It holds the signing identity, in a throwaway keychain created and destroyed
per job.** The keychain is created at the start of the run under a name that
cannot collide, the certificate is imported into it, and it is deleted in an
`always()` cleanup step. Nothing persists between jobs, so a leaked keychain
password is worth nothing after the run ends.

**It must not hold a GitHub token.** No `gh auth login` in `<BUILD_USER>`, ever.
The runner has its own registration credential, scoped to taking jobs. A `gh`
token in that account acts as you, with write access, across every repo you
own — and that account is the one executing workflow-authored code. Every `gh`
command in these guides runs as `<ADMIN>` for this reason.

If it has already happened:

```bash
gh auth logout -h github.com     # in the BUILD account
rm -rf ~/.config/gh
```

**It must not hold your personal credentials.** Not your ssh keys, not your
password manager, not your Apple ID signed into Xcode, not a copy of the repo
with your commit identity. `chmod 700` your own home directory before the build
account exists — macOS does not always default to that, and a `755` home lets
the build account walk into shell history and agent transcripts, which
routinely contain exported API keys.

**Auto-login is a real cost, taken knowingly.** `/etc/kcpassword` holds the
build account's password obfuscated, not encrypted. It is acceptable because
that password unlocks a non-admin sandbox and nothing else. It stops being
acceptable the moment that account is granted admin, or shares a password with
another account.

## Verify

```bash
gh api repos/<OWNER/REPO> --jq '.private'
gh api repos/<OWNER/REPO>/actions/runners --jq '.runners[].status'
gh variable list --repo <OWNER/REPO>
```

Then the adversarial part — **all three must fail**:

```bash
sudo -u <BUILD_USER> -H gh auth status                    # no token there
sudo -u <BUILD_USER> cat ~<ADMIN>/.ssh/id_ed25519         # permission denied
sudo -u <BUILD_USER> -H security find-identity -v -p codesigning \
  | grep -i 'Apple Distribution'                          # nothing between jobs
```

The third one failing is the point of the throwaway keychain. **A distribution
identity that is present when no job is running means a cleanup step did not
run**, and the certificate is now sitting in a keychain that outlives the job.

And confirm the gate you chose is the gate you have. For (b), open a run and
watch it pause; a run that sails through is a run with no protection rule in
effect, whatever the settings page displays.

## Known limits

**One machine, one account, all apps.** Jobs from different repos run as the
same user with the same home directory. A malicious or compromised dependency
in one app's build can reach another app's checkout. Runner groups route jobs;
they do not isolate them.

**Workflow files are code, and they are reviewed like documentation.** A change
to `.github/workflows/` is a change to what runs on your Mac with your
certificate. Protect that path explicitly if the repo has contributors.

**Secrets are readable by any step in the job that receives them.** Once
decoded on the runner, the `.p8` and the `.p12` are files. Any step after that
point can read them, including a step added by a dependency's action.

**The upload is irreversible.** Apple accepts or rejects; there is no recall of
an accepted build. Every gate discussed here exists because of that one
property.

**Nothing here monitors.** No alert fires when the runner goes offline, when a
job fails, or when a build is uploaded. The GitHub Actions tab is the only
record, and it is one nobody reads on a normal day.
