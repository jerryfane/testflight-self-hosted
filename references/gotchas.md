# Gotchas

Failures that cost real time on the way to a first successful TestFlight upload
from a self-hosted Mac. Each is the kind that survives being reasoned about,
because the mechanism is invisible from where the error is printed.

Eight ship attempts produced this list. Six of them failed at the same step
with the same eight words.

---

## `errSecInternalComponent` means the runner is not in a login session

**Cost: four ship attempts, and every keychain fix in this file was tried first.**

The archive fails, and the only thing the log carries is:

```
CodeSign .../MyAppExtension.appex
    Signing Identity: "Apple Distribution: <NAME>"
/usr/bin/codesign --force --sign <SHA1> --entitlements ... 
    .../MyAppExtension.appex: errSecInternalComponent
Command CodeSign failed with a nonzero exit code
```

`errSecInternalComponent` from `codesign` means it could not **use the private
key**. Not that the key is missing, not that the certificate is wrong, not that
the profile is bad — all of those produce different, more specific errors. It
could not reach the Security Server to perform the operation.

That happens when the process is in a launchd domain with no GUI login session.
Check it directly:

```bash
launchctl managername          # Aqua  in a login session
                               # Background  under sudo, ssh, or a LaunchDaemon
```

Measured on a real box: a plain ssh shell reads `Background`. `sudo -u <user>
bash -lc` also reads `Background` — dropping to the user does **not** enter that
user's session. Only `launchctl asuser <uid>` reads `Aqua`.

So the runner started the ordinary way — `sudo -u builder ./run.sh` — is in the
wrong session, and every keychain property can be perfect while signing still
fails. Confirmed perfect and still failing: identity present and valid, WWDR
intermediate present, Apple root present, keychain unlocked and set as default,
partition list correct, profile authorising the exact certificate.

**Run the runner inside the build account's aqua session.** Either
`sudo launchctl asuser $(id -u <BUILD_USER>) sudo -u <BUILD_USER> ... ./run.sh`
for a one-off, or — permanently — `./svc.sh install && ./svc.sh start` run **as
that user, without sudo**, which writes a LaunchAgent into the per-user domain.
Never a LaunchDaemon: it has no login session, so it reintroduces exactly this
failure.

Same mechanism as mac-build-gate's LaunchAgent-not-LaunchDaemon rule, where it
is CoreSimulator that needs the GUI session rather than the keychain.

---

## The key partition list must name `codesign:` explicitly

**Cost: one ship attempt, on a fix that was correct and insufficient.**

The documented incantation after importing a `.p12` into a throwaway keychain is
usually written as:

```bash
security set-key-partition-list -S apple-tool:,apple: -s -k "$PW" build.keychain
```

`apple:` does not cover `codesign`. That list grants access to `apple-tool` and
`apple` and withholds it from the one binary that does the signing. The symptom
is `errSecInternalComponent` — identical to the session problem above, which is
why the two get confused and why fixing one while the other is still wrong reads
as "the fix did not work".

**Use `-S apple-tool:,apple:,codesign:`.** Also import the identity with `-A`
(no ACL restriction) rather than `-T /usr/bin/codesign` (an ACL naming one
binary), and make the throwaway keychain the default for the job, saving and
restoring the previous default.

---

## "unable to build chain to self-signed root" is a warning, not the error

**Cost: two ship attempts spent fixing something that was not broken.**

Immediately above the failure, `codesign` prints:

```
Warning: unable to build chain to self-signed root for signer "Apple Distribution: <NAME>"
.../MyAppExtension.appex: errSecInternalComponent
Command CodeSign failed with a nonzero exit code
```

The first line is a **warning**. It appears in signings that succeed. It is not
the cause of the line under it. Reading it as the cause produced a correct-in-
isolation fix — importing the WWDR intermediate and the Apple root into the
keychain — which changed nothing, and then a second attempt spent on a typo in
one of the certificate URLs introduced by that fix.

The real error was one line further down, at column ~210 of a log being read
with `cut -c1-200`.

**Read to the end of the failing region before theorising, and treat `Warning:`
as a warning.** The importing of WWDR is still worth doing — a freshly created
keychain genuinely lacks the intermediate — but it is a precondition, not this
bug.

---

## `flutter build ipa` hides xcodebuild's error, and `gh run view --log` truncates before it

**Cost: the four attempts above were blind because of this.**

Flutter's failure output is eight words:

```
Failed to build iOS app
Uncategorized (Xcode): Command CodeSign failed with a nonzero exit code
Encountered error while archiving for device.
```

There is no `error:` line anywhere in the step. Adding `-v` to
`flutter build ipa` surfaces xcodebuild's own output — but on a real build that
step becomes ~40,000 lines, and `gh run view <id> --log` **truncates**, ending
mid-build with the failure cut off. Two different readers of the same run both
concluded there was nothing to find.

The untruncated log is only available from the raw endpoint:

```bash
gh api /repos/<OWNER/REPO>/actions/runs/<RUN_ID>/logs > logs.zip
unzip -o -q logs.zip -d logs && grep -ra 'errSec\|CodeSign failed' logs/
```

**When a step's log ends without an error line, assume truncation before
assuming there is nothing there.** Verbose output makes this worse, not better,
so turn on `-v` and switch to the raw endpoint in the same move.

Same family as *Read the artifact, not the label* in mac-build-gate: the
rendered view is not the artifact.

---

## `altool --apiKey` takes a key ID and reads the `.p8` off disk

**Cost: would have wasted a full run — caught by reading the step, not running it.**

The upload step looks complete and is not:

```bash
xcrun altool --upload-app -f build/MyApp.ipa -t ios \
  --apiKey "$APP_STORE_CONNECT_KEY_ID" \
  --apiIssuer "$APP_STORE_CONNECT_ISSUER_ID"
```

`--apiKey` is a key **identifier**, not key material. altool then looks for
`AuthKey_<id>.p8` **on disk**, searching `~/.appstoreconnect/private_keys` among
other places. A lane that only ever passes the key as base64 in an environment
variable never writes that file, and the step fails — at the very end, after
signing, after archiving, and after minting a provisioning profile into the
owner's Apple account.

**Write the key to `~/.appstoreconnect/private_keys/AuthKey_<id>.p8` at
`umask 077` before calling altool, and remove it in an `always()` cleanup.**

The ordering is the real lesson: the irreversible step ran before the check that
would have failed. Anything that writes to the Apple account should come after
every precondition that can be verified locally.

---

## A Call Directory extension point is `com.apple.callkit.call-directory`

**Cost: one full ship — signed, archived, uploaded, rejected.**

```
UPLOAD FAILED with 1 error
ERROR: Invalid Info.plist value. The value of the NSExtensionPointIdentifier
key, com.apple.identitylookup.call-directory, in the Info.plist of
"MyApp.app/PlugIns/MyAppExtension.appex" is invalid. (90349)
```

The `com.apple.identitylookup.*` family is real — `message-filter` and
`classification-ui` are both valid extension points. There is no
`call-directory` in it; that one belongs to `callkit`. The wrong value is
therefore well-formed, plausible, and names a namespace that exists.

Nothing local objects. Xcode compiles, embeds and **signs** an app extension
with a bogus extension point without a word. The archive succeeds, the `.ipa` is
well formed, and a version-agreement check that reads the same `Info.plist`
passes, because it is asking about versions. Apple's upload validator is the
only reader that checks it, and it runs last.

**Validate `NSExtensionPointIdentifier` against a known-good list in ordinary
CI**, where it costs nothing. `check_extension_point.py` in this skill does
that; it needs no Mac, no credentials and no build.

---

## Provisioning profiles capture entitlements at creation

A profile is not a live view of its App ID. Whatever entitlements the App ID had
at the moment the profile was minted are baked into it permanently. Attach an
App Group afterwards and the existing profile does not gain it — it stays broken
while its name, state, type and expiry all still look perfect.

This is what makes a naive reuse check dangerous: matching on name, `ACTIVE`
state and `profileType` is matching on **metadata**, and metadata is exactly
what stays right when the profile has gone wrong.

**Check what a profile GRANTS before reusing it** — decode its entitlements and
require the app group the target actually declares. If it does not, refuse and
name the UUID so a human can delete it. Do not delete it from the account
automatically; a script that silently removes things from someone's developer
account is worse than the bug.

---

## The build number cannot reach a generated `Info.plist` you edited by hand

**Cost: one cycle, on the first project this lane was ever adopted into.**

The lane computes a monotonic build number and passes it to `xcodebuild` as
`CURRENT_PROJECT_VERSION`. That only becomes `CFBundleVersion` if the target's
`Info.plist` asks for it:

```xml
<key>CFBundleVersion</key>
<string>$(CURRENT_PROJECT_VERSION)</string>   <!-- indirection: the number arrives -->
<string>1</string>                            <!-- literal: it never does -->
```

With a literal, the first upload succeeds and **every later one is rejected for
not incrementing** — a failure that arrives at the last step of a fifteen-minute
run and says nothing about `Info.plist`.

The trap underneath it: on an xcodegen or tuist project, `Info.plist` is
**generated**. Editing it works, the file looks right, and the next `generate`
silently puts the literal back. The edit has to go where the generator reads it,
`project.yml`'s `info.properties`, not into the file it emits:

```yaml
    info:
      path: MyApp/Info.plist
      properties:
        CFBundleVersion: $(CURRENT_PROJECT_VERSION)
        CFBundleShortVersionString: $(MARKETING_VERSION)
```

Set a default `CURRENT_PROJECT_VERSION` in the target's build settings too, or a
local Xcode build produces an empty version — CI overrides it on the command
line, but a developer's machine has nothing to override.

**Edit the source of a generated file, never the file.** Same family as
mac-build-gate's *a generated `.xcodeproj` survives `git checkout`*: anything
derived will outlive your change to it, in whichever direction hurts.
`preflight.py` checks this one before a build exists.

---

## A capability being enabled is not a group being attached

**Cost: this survived four separate audits of the same function.**

The pre-mint check that reads well and is wrong:

```python
if "APP_GROUPS" not in capabilities:
    die("App Groups is not enabled on this bundle id")
```

Enabling the App Groups **capability** and attaching a **specific group** to the
bundle id are two separate actions in the developer portal. Doing only the first
leaves a bundle id that passes this check, and Apple will happily mint a valid
profile against it that grants no group at all. The failure then appears at
`CodeSign`, with no mention of app groups.

Worse, the capability list comes back flat in the API's `included` array, and
Apple's identifier filter is a contains-match (see below) — so the list may
describe both `com.example.app` and `com.example.app.extension`, and **the
extension's enabled capability can vouch for the app**.

**Verify content, not presence**: attribute capabilities to the specific bundle
id, then require the specific group. Where Apple does not report attachments at
all, say so explicitly — emit a warning that states it is *not* a pass and names
where the property is actually established (after the mint, against the
profile's own entitlements). A check that cannot be made must announce that it
could not be made; a silent pass is indistinguishable from a real one in a green
log.

---

## Apple's `filter[identifier]` is a contains-match

Querying bundle ids or profiles by identifier does not return one exact record:

```
GET /v1/bundleIds?filter[identifier]=com.example.app
  → com.example.app
  → com.example.app.extension        ← also returned
```

`data[0]` is whichever Apple ordered first. Any code that reads `data[0]` after
filtering is quietly correct until an app grows an extension, and then silently
operates on the wrong record — asking about the app and being answered about the
extension.

**Filter to narrow, then match exactly in code**:
`[x for x in data if x["attributes"]["identifier"] == bundle_id]`.

---

## A failed lookup must never read as "nothing exists"

The most expensive one-line bug available in this domain:

```python
status, body = asc.get("/v1/profiles?filter[name]=" + name)
if status != 200:
    return None          # ← caller reads None as "no profile exists, mint one"
```

A 401, a 403 on key scope, or a transient 500 becomes indistinguishable from
"Apple answered and there is nothing there". The caller then **creates a second
provisioning profile in the owner's Apple developer account** in response to a
network blip — the one artifact in the whole lane that no cleanup can undo.

**Die on any non-200 in a lookup whose `None` grants permission to write.**
Failing to read is not the same as reading nothing, and only one of them may
lead to a write.

Generalises: for every function that returns an "absent" value, ask what the
caller does with it. If the answer is "creates something", absent must be
unambiguous.

---

## A standing runner takes the oldest queued job, not the one you meant

**Cost: a near-miss on an unintended TestFlight upload.**

GitHub assigns a runner to the **oldest queued job matching its labels**. It has
no notion of which job you had in mind when you started it.

This breaks a very natural safety habit. While a runner is ephemeral and started
by hand, "there is no runner online" is an effective gate on shipping. Make that
runner permanent and the gate silently disappears: a ship job queued at any time
— by a push, by a rerun, by an agent — is served within seconds. The control did
not change. The world under it did, and nothing announced that.

Related trap: the obvious ways to ask "what is queued?" do not answer it.
`gh run list --limit N` is a **recency** window, so a job that has been queued a
long time — exactly the one that will be served next — can fall out of it. And
`--status queued` matches only that one state: a run sitting in `pending`,
`waiting` or `requested` is equally non-terminal and equally next in line, and
is not returned at all. (Measured on gh 2.97.0, whose `--status` accepts all of
`queued|pending|waiting|requested|in_progress` — the flag is not the problem;
enumerating one state and calling it "the queue" is.)

```bash
# every non-terminal run, oldest first — the order that decides assignment
gh api "/repos/<OWNER/REPO>/actions/runs?per_page=100" --paginate --jq '
  .workflow_runs[] | select(.status != "completed")
  | "\(.created_at)\t\(.id)\t\(.name)\t\(.status)"' | sort
```

**When you make a runner permanent, replace every safety that depended on it
being absent.** The enforced options are a required-reviewer rule on the
deployment environment, or an `if: vars.SHIP_ARMED == '1'` guard on the upload
job. See `security-posture.md`.

---

## `gh api --paginate` runs `--jq` once per page

```bash
count=$(gh api "/repos/<OWNER/REPO>/actions/runs?per_page=100" --paginate \
          --jq '[.workflow_runs[] | select(...)] | length')
[ "$count" = "0" ] || refuse      # count is "0\n0" once there are two pages
```

The jq filter is applied per page, so an aggregate — a count, a max, a sum —
comes back as one line per page. Displayed, it looks like a harmless repeated
`0`. Compared as a string, it silently stops matching.

`--slurp` aggregates pages but is rejected together with `--jq`.

**Sum across pages explicitly**: pipe to `awk '{s+=$1} END{print s+0}'`.

Ours failed closed — the guard refused to launch when it should have proceeded.
It could as easily have been written to fail open. A guard that misfires for a
reason unrelated to what it guards is not a guard you can reason about.

---

## Self-hosted runners and public repositories do not mix

A workflow file is code, and on a public repository anyone can propose one. A
self-hosted runner executes it **on your machine, as the account holding your
signing identity**, with whatever that account can reach.

GitHub documents this. It is repeated here because the pressure to relax it is
real: a public repo is exactly where a stranger's contribution is most welcome,
and the runner is right there.

**Keep self-hosted runners to private repositories.** If a public repo needs CI,
give it hosted runners. Set organisation secrets to `--visibility private`
rather than `all`, so that a public repo added to the org later cannot reach the
distribution certificate even by accident.

---

## The pattern under most of the above

Six of these are the same shape: **something was checked for presence when what
mattered was content.**

- A capability is enabled — but no group is attached.
- A profile is `ACTIVE` with the right name — but its entitlements are stale.
- `security find-identity -v` reports one valid identity — but `codesign` cannot
  use it.
- The filter returned a record — but not the record asked for.
- The step is green — but the check inside it could not run.

The reliable move is to test the operation you actually depend on rather than a
proxy for it. `check_signing_preconditions.py` in this skill does this literally:
it copies a small Mach-O and **signs it** with the identity before the lane
touches the Apple account. Every metadata check passed on all four runs that
died at `CodeSign`; the test signature is what finally distinguished them.
