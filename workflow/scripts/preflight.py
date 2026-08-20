#!/usr/bin/env python3
"""Check every precondition that can be checked before pushing.

A first ship normally teaches you what is wrong by failing a fifteen-minute
build. Most of what it teaches was knowable in ten seconds. This asks all of it
at once and prints one report.

    python3 .github/scripts/preflight.py --repo OWNER/REPO --user builder
    python3 .github/scripts/preflight.py --org  YOUR_ORG  --user builder

THREE STATES, AND THE THIRD IS THE IMPORTANT ONE.

    PASS  the property holds, and was measured
    FAIL  the property does not hold; a fix command is printed with it
    SKIP  the check could not run here, and why

A check that could not run must never render as a pass. Six of the failures
documented in references/gotchas.md are a weak check reading like a strong one,
so SKIP is deliberately as loud as FAIL and never counts toward success.

REPORT ONLY. Nothing here mutates the machine, the repository or the Apple
account. Every Apple call is a read. Fixes are printed for a human to run.

DEGRADES BY PLATFORM. Off macOS the Mac-only checks report SKIP with a reason
and the GitHub and App Store Connect halves still run, so an agent elsewhere can
clear those before anyone walks to the Mac.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
RESULTS = []

# The seven credentials a lane needs. All are Apple-team-scoped rather than
# app-scoped, which is why they belong at organisation level; see setup-org.md.
REQUIRED_SECRETS = [
    "APPLE_TEAM_ID",
    "APP_STORE_CONNECT_ISSUER_ID",
    "APP_STORE_CONNECT_KEY_ID",
    "APP_STORE_CONNECT_P8_BASE64",
    "DIST_CERT_P12_BASE64",
    "DIST_CERT_PASSWORD",
    "KEYCHAIN_PASSWORD",
]


def record(name, state, message, fix=None):
    RESULTS.append((name, state, message, fix))


def gh(path, jq=None):
    """A read against the GitHub API. Returns (ok, text)."""
    cmd = ["gh", "api", path]
    if jq:
        cmd += ["--jq", jq]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip()
    return True, out.stdout.strip()


def run_as_build(command, build_user, build_uid):
    """Run a command inside the build user's Aqua session.

    Lifted from scripts/install-runner.sh. `sudo -u USER` changes the user and
    leaves the SESSION alone, landing in Background where the keychain is
    unreachable; `launchctl asuser UID` is the only non-interactive way in.
    Measuring the operator's own session instead of the build user's would
    report a property of the wrong account, which is worse than not checking.
    """
    if os.environ.get("USER") == build_user:
        argv = ["bash", "-lc", command]
    else:
        argv = ["sudo", "-n", "launchctl", "asuser", str(build_uid),
                "sudo", "-n", "-u", build_user, "-H", "bash", "-lc", command]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return out.returncode == 0, (out.stdout or out.stderr).strip()


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path):
    """Parse app-config.env the way the workflow does, including the CRLF guard.

    A third parser would eventually disagree with the lane's, and the preflight
    would bless a config the job then rejects. Mirrors the "Load the app
    configuration" step in testflight.yml.
    """
    if not os.path.isfile(path):
        record("CONFIG", SKIP,
               f"{path} not found; app-level checks need it",
               f"cp .github/app-config.env.example {path} and fill it in")
        return {}

    raw = open(path, "rb").read()
    if b"\r" in raw:
        record("CONFIG", FAIL,
               f"{path} has CRLF line endings",
               f"convert it to LF: tr -d '\\r' < {path} > tmp && mv tmp {path}")
        return {}

    cfg = {}
    for line in raw.decode().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cfg[key.strip()] = value.strip().strip('"').strip("'")

    missing = [k for k in ("APP_BUNDLE_ID", "IOS_DIR", "FLUTTER", "HAS_EXTENSION")
               if not cfg.get(k)]
    if missing:
        record("CONFIG", FAIL,
               f"{path} is missing required keys: {', '.join(missing)}",
               f"fill them in; see .github/app-config.env.example")
    else:
        record("CONFIG", PASS,
               f"{cfg['APP_BUNDLE_ID']}, ios dir {cfg['IOS_DIR']}, "
               f"flutter={cfg['FLUTTER']}, extension={cfg['HAS_EXTENSION']}")
    return cfg


# --------------------------------------------------------------------------
# GitHub side - runs anywhere
# --------------------------------------------------------------------------

def check_gh_cli():
    if not shutil.which("gh"):
        record("GH", FAIL, "the gh CLI is not installed",
               "brew install gh && gh auth login")
        return False
    ok, out = gh("user", ".login")
    if not ok:
        record("GH", FAIL, "gh is installed but not authenticated",
               "gh auth login")
        return False
    record("GH", PASS, f"authenticated as {out}")
    return True


def check_repo_private(repo):
    """A public repo plus a self-hosted runner means a stranger's pull request
    runs on your Mac as the account holding your signing identity."""
    if not repo:
        record("REPO", SKIP, "no --repo given", "pass --repo OWNER/REPO")
        return
    ok, out = gh(f"repos/{repo}", ".visibility")
    if not ok:
        record("REPO", FAIL, f"cannot read {repo}: {out.splitlines()[0][:90]}",
               "check the name and that gh has access")
        return
    if out != "private":
        record("REPO", FAIL,
               f"{repo} is {out}. Self-hosted runners must not serve public repos",
               "make it private, or give it GitHub-hosted runners instead")
    else:
        record("REPO", PASS, f"{repo} is private")


def check_runner(repo, org, wanted_labels, labels_source):
    """Online, standing rather than ephemeral, and labelled to match runs-on."""
    if org:
        api, scope = f"/orgs/{org}/actions/runners", f"org {org}"
    elif repo:
        api, scope = f"repos/{repo}/actions/runners", repo
    else:
        record("RUNNER", SKIP, "no --org or --repo given", "pass one of them")
        return

    ok, out = gh(api, ".runners")
    if not ok:
        record("RUNNER", FAIL, f"cannot list runners for {scope}: "
               f"{out.splitlines()[0][:90]}",
               "org runners need the admin:org scope: "
               "gh auth refresh -h github.com -s admin:org")
        return

    runners = json.loads(out or "[]")
    if not runners:
        record("RUNNER", FAIL, f"no runner registered for {scope}",
               "scripts/install-runner.sh --user <BUILD_USER> "
               + (f"--org {org}" if org else f"--repo {repo}"))
        return

    online = [r for r in runners if r.get("status") == "online"]
    if not online:
        names = ", ".join(r["name"] for r in runners)
        record("RUNNER", FAIL, f"registered but offline: {names}",
               "the build user must be logged in graphically, then: "
               "sudo launchctl asuser $(id -u <BUILD_USER>) sudo -u <BUILD_USER> "
               "-H bash -lc 'cd ~/actions-runner && ./svc.sh start'")
        return

    r = online[0]
    labels = {l["name"] for l in r.get("labels", [])}

    # ephemeral null or false means standing. An ephemeral runner serves one job
    # and de-registers, so the next push waits forever with nothing to serve it.
    if r.get("ephemeral"):
        record("RUNNER", FAIL, f"{r['name']} is online but EPHEMERAL",
               "re-register without --ephemeral so it serves repeated jobs")
        return

    shown = ", ".join(sorted(labels))

    if not labels_source:
        # No workflow in this repository to compare against. Report what the
        # runner is, and say plainly that the labels were not checked.
        record("RUNNER", PASS, f"{r['name']} online, standing, labels [{shown}]")
        record("LABELS", SKIP,
               "no .github/workflows/testflight.yml here, so the runner's "
               "labels were not compared against any runs-on",
               "run this from the app repository to check them")
        return

    missing = set(wanted_labels) - labels
    if missing:
        record("RUNNER", PASS, f"{r['name']} online, standing, labels [{shown}]")
        record("LABELS", FAIL,
               f"{labels_source} asks for {', '.join(sorted(missing))}, which "
               f"{r['name']} does not carry - the job would queue forever",
               f"re-register with --labels {','.join(sorted(labels | missing))}, "
               "or change runs-on to match the runner")
        return

    record("RUNNER", PASS, f"{r['name']} online, standing, labels [{shown}]")
    record("LABELS", PASS,
           f"runner carries everything {labels_source} asks for")


def check_secrets(repo, org):
    """Presence by name only. Values are write-only by design, so this is the
    ceiling: it catches the partially-populated set, which looks configured and
    fails halfway through signing."""
    found, sources = set(), []

    if org:
        ok, out = gh(f"/orgs/{org}/actions/secrets", ".secrets")
        if ok:
            secrets = json.loads(out or "[]")
            found |= {s["name"] for s in secrets}
            sources.append(f"org {org}")
            # `all` exposes the distribution certificate to any public repo
            # added to the org later. `private` covers every private repo and
            # cannot leak that way.
            wide = [s["name"] for s in secrets if s.get("visibility") == "all"]
            if wide:
                record("SECRETVIS", FAIL,
                       f"{len(wide)} org secret(s) are visible to ALL repos, "
                       "including any public one added later",
                       f"gh secret set <NAME> --org {org} --visibility private")
            else:
                record("SECRETVIS", PASS, "org secrets are not visible to public repos")
        else:
            record("SECRETVIS", SKIP, f"cannot read org secrets for {org}",
                   "gh auth refresh -h github.com -s admin:org")

    if repo:
        ok, out = gh(f"repos/{repo}/actions/secrets", ".secrets")
        if ok:
            found |= {s["name"] for s in json.loads(out or "[]")}
            sources.append(repo)
        ok, out = gh(f"repos/{repo}/environments", ".environments[]?.name")
        if ok and out:
            for env in out.splitlines():
                ok2, out2 = gh(
                    f"repos/{repo}/environments/{env}/secrets", ".secrets")
                if ok2:
                    found |= {s["name"] for s in json.loads(out2 or "[]")}
                    sources.append(f"{repo} env:{env}")

    if not sources:
        record("SECRETS", SKIP, "no --org or --repo given", "pass one of them")
        return

    missing = [s for s in REQUIRED_SECRETS if s not in found]
    where = " + ".join(sources)
    if missing:
        record("SECRETS", FAIL,
               f"{len(missing)} of {len(REQUIRED_SECRETS)} missing from {where}: "
               f"{', '.join(missing)}",
               "see references/setup-org.md section 4; a partial set looks "
               "configured and fails at signing")
    else:
        record("SECRETS", PASS, f"all {len(REQUIRED_SECRETS)} present ({where})")


# --------------------------------------------------------------------------
# Mac side
# --------------------------------------------------------------------------

def check_mac_session(build_user):
    """The single check that would have saved four of the eight attempts this
    lane was extracted from."""
    if platform.system() != "Darwin":
        record("SESSION", SKIP, "not macOS", "run this on the build Mac")
        return
    if not build_user:
        record("SESSION", SKIP, "no --user given", "pass --user <BUILD_USER>")
        return

    try:
        uid = subprocess.run(["id", "-u", build_user], capture_output=True,
                             text=True, timeout=10)
        if uid.returncode != 0:
            record("SESSION", FAIL, f"no such user: {build_user}",
                   "check --user, or create the account (setup-runner.md step 1)")
            return
        build_uid = uid.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        record("SESSION", SKIP, f"could not resolve the user: {exc}")
        return

    ok, out = run_as_build("launchctl managername", build_user, build_uid)
    if not ok:
        record("SESSION", SKIP,
               f"could not enter {build_user}'s session ({out.splitlines()[0][:70] if out else 'no output'})",
               "this needs sudo without a password prompt; run it from a "
               "terminal, or as the build user")
        return
    if out != "Aqua":
        record("SESSION", FAIL,
               f"{build_user}'s session reports '{out}', not Aqua - codesign "
               "cannot reach the keychain from there",
               f"log in graphically as {build_user} (fast user switching), "
               "switch back, and re-run. Never Log Out.")
    else:
        record("SESSION", PASS, f"{build_user} session is Aqua")


def check_xcode():
    if platform.system() != "Darwin":
        record("XCODE", SKIP, "not macOS")
        return
    try:
        out = subprocess.run(["xcodebuild", "-version"], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record("XCODE", FAIL, f"xcodebuild not usable: {exc}",
               "install Xcode from the App Store")
        return
    if out.returncode != 0:
        record("XCODE", FAIL,
               f"xcodebuild failed: {(out.stderr or '').strip()[:80]}",
               "sudo xcodebuild -license accept && sudo xcodebuild -runFirstLaunch")
        return
    record("XCODE", PASS, out.stdout.splitlines()[0].strip())


def check_signing_probe(build_user):
    """Deliberately a SKIP, not a PASS.

    The identity lives in a throwaway keychain the lane creates and deletes per
    job, so between runs there is nothing on the machine to sign with -- measured
    on a working box, `security find-identity -v -p codesigning` returns zero
    identities for both the operator and the build account. The real key-access
    probe therefore stays in the lane, where check_signing_preconditions.py runs
    it before anything is minted.

    Reporting this as PASS because no failure was observed would be exactly the
    coincidentally-green check this repo documents six times over.
    """
    if platform.system() != "Darwin":
        record("SIGNING", SKIP, "not macOS")
        return
    record("SIGNING", SKIP,
           "no signing identity exists between runs by design; the key-access "
           "probe runs in-lane, pre-mint",
           "nothing to do - SESSION above is the precondition that decides it")


# --------------------------------------------------------------------------
# Apple side - needs credentials
# --------------------------------------------------------------------------

def asc_ready():
    """asc.py raises a bare KeyError for these, so name them before it does."""
    if not (os.environ.get("APP_STORE_CONNECT_P8_BASE64")
            or os.environ.get("APP_STORE_CONNECT_P8_PATH")):
        return False, "no key: set APP_STORE_CONNECT_P8_PATH=/path/AuthKey_XXX.p8"
    for var in ("APP_STORE_CONNECT_ISSUER_ID", "APP_STORE_CONNECT_KEY_ID"):
        if not os.environ.get(var):
            return False, f"{var} is not set"
    try:
        import jwt  # noqa: F401
    except ImportError:
        return False, "PyJWT is not installed (pip3 install --user pyjwt cryptography)"
    return True, ""


def check_apple(cfg):
    """Bundle id, App Group attachment, and a distribution certificate.

    Every one of these is an existing hardened function; calling them rather
    than reimplementing keeps the exact-match fix, the capability-attribution
    rule and the attached-versus-enabled distinction that each cost a real
    failure to learn.
    """
    ready, why = asc_ready()
    if not ready:
        for name in ("BUNDLEID", "APPGROUP", "DISTCERT", "BUILDNUM"):
            record(name, SKIP, why,
                   "export the three APP_STORE_CONNECT_* values to enable these")
        return

    # ensure_profiles freezes configuration into module globals at import time,
    # so the config has to be in the environment BEFORE the import or APP_GROUP
    # is empty and the app-group check silently passes on nothing.
    for key, value in cfg.items():
        os.environ.setdefault(key, value)

    sys.path.insert(0, HERE)
    try:
        import ensure_profiles
    except Exception as exc:  # noqa: BLE001
        for name in ("BUNDLEID", "APPGROUP", "DISTCERT"):
            record(name, SKIP, f"could not load ensure_profiles: {exc}")
        return

    # asc.die raises SystemExit, so each check is wrapped and the report
    # continues instead of stopping at the first failure.
    bundle_id = cfg.get("APP_BUNDLE_ID", "")
    if bundle_id:
        try:
            ensure_profiles.bundle_id_resource(bundle_id)
            note = " (and its App Group is attached)" if cfg.get("APP_GROUP") else ""
            record("BUNDLEID", PASS, f"{bundle_id} exists in the portal{note}")
            if cfg.get("APP_GROUP"):
                record("APPGROUP", PASS,
                       f"{cfg['APP_GROUP']} attached to {bundle_id}")
        except SystemExit as exc:
            msg = str(exc).replace("::error::", "").strip()
            first = msg.splitlines()[0] if msg else "refused"
            target = "APPGROUP" if "group" in msg.lower() else "BUNDLEID"
            record(target, FAIL, first[:160],
                   "Certificates, Identifiers & Profiles - note that enabling a "
                   "capability and attaching a group are two separate actions")
    else:
        record("BUNDLEID", SKIP, "APP_BUNDLE_ID not in config")

    try:
        certs = ensure_profiles.distribution_certificates()
        record("DISTCERT", PASS,
               f"{len(certs)} distribution certificate(s) on the team")
    except SystemExit as exc:
        record("DISTCERT", FAIL, str(exc).replace("::error::", "").strip()[:160],
               "create a distribution certificate in the developer portal")


def check_build_number(cfg, repo):
    """Apple rejects an upload whose build number does not exceed the last one.

    The cheapest possible way to waste a full run, and invisible until the very
    last step.
    """
    ready, why = asc_ready()
    if not ready or not repo or not cfg.get("APP_BUNDLE_ID"):
        return  # already reported by check_apple, or nothing to compare against

    sys.path.insert(0, HERE)
    try:
        import asc
        import verify_beta_ready
    except Exception as exc:  # noqa: BLE001
        record("BUILDNUM", SKIP, f"could not load the client: {exc}")
        return

    try:
        app = verify_beta_ready.app_id(cfg["APP_BUNDLE_ID"])
    except SystemExit as exc:
        record("BUILDNUM", SKIP,
               "no App Store Connect app record yet: "
               + str(exc).replace("::error::", "").strip().splitlines()[0][:110],
               "create the app in App Store Connect before the first upload")
        return

    status, body = asc.get(
        f"/v1/builds?filter[app]={app}&sort=-uploadedDate&limit=1")
    if status != 200:
        record("BUILDNUM", SKIP, f"App Store Connect returned {status} listing builds")
        return
    data = body.get("data", [])
    last = data[0].get("attributes", {}).get("version") if data else None

    # Mirrors the workflow: BUILD_OFFSET + github.run_number.
    ok, out = gh(f"repos/{repo}/actions/variables/BUILD_OFFSET", ".value")
    offset = int(out) if ok and out.strip().isdigit() else 0
    ok, out = gh(
        f"repos/{repo}/actions/workflows/testflight.yml/runs?per_page=1",
        ".workflow_runs[0].run_number")
    if not ok or not out.strip().isdigit():
        record("BUILDNUM", SKIP,
               "could not read the last run_number for testflight.yml",
               "harmless before the first run")
        return
    nxt = offset + int(out) + 1

    if last is None:
        record("BUILDNUM", PASS, f"no builds uploaded yet; next would be {nxt}")
    elif str(last).isdigit() and nxt <= int(last):
        record("BUILDNUM", FAIL,
               f"next build number would be {nxt}, but {last} is already "
               "uploaded - Apple will reject it",
               f"set the repository variable BUILD_OFFSET to at least "
               f"{int(last) - int(out) + 1}: gh variable set BUILD_OFFSET "
               f"--repo {repo} --body {int(last) - int(out) + 1}")
    else:
        record("BUILDNUM", PASS, f"last uploaded {last}, next would be {nxt}")


def check_project_exists(cfg):
    """The thing xcodebuild will be pointed at has to be on disk.

    Found by adopting the lane on a real project: an xcodegen repository has no
    .xcodeproj until something generates it, and the lane's Dependencies step
    runs pod install and flutter pub get but nothing else. The archive then
    fails on a container that was never there. Cheap to ask here; twelve minutes
    into a build otherwise.
    """
    if cfg.get("FLUTTER") == "1":
        record("PROJECT", SKIP, "Flutter build; xcodebuild container not used")
        return
    ios_dir = cfg.get("IOS_DIR")
    if not ios_dir:
        record("PROJECT", SKIP, "IOS_DIR not in config")
        return
    if not os.path.isdir(ios_dir):
        record("PROJECT", FAIL, f"IOS_DIR '{ios_dir}' does not exist here",
               "check IOS_DIR, or run this from the repository root")
        return

    # A PREBUILD command generates the container at build time (xcodegen,
    # tuist, a script). If it is set and the container is absent, the honest
    # report is "will be generated, not verified here" -- a flat FAIL would
    # contradict the lane, which generates before it archives.
    has_prebuild = bool(cfg.get("PREBUILD"))

    named = cfg.get("XCODE_WORKSPACE") or cfg.get("XCODE_PROJECT")
    if named:
        if os.path.exists(os.path.join(ios_dir, named)):
            record("PROJECT", PASS, f"{ios_dir}/{named} is present")
        elif has_prebuild:
            record("PROJECT", SKIP,
                   f"{ios_dir}/{named} is absent, but PREBUILD is set to "
                   "generate it - not run here",
                   "verify PREBUILD produces exactly that container")
        else:
            record("PROJECT", FAIL,
                   f"{ios_dir}/{named} does not exist - xcodebuild has nothing "
                   "to archive",
                   "if the project is generated (xcodegen, tuist, a script), set "
                   "PREBUILD in .github/app-config.env so the lane generates it")
        return

    found = [n for n in os.listdir(ios_dir)
             if n.endswith((".xcworkspace", ".xcodeproj"))]
    if len(found) == 1:
        record("PROJECT", PASS, f"{ios_dir}/{found[0]} is present")
    elif not found and has_prebuild:
        record("PROJECT", SKIP,
               f"no container in {ios_dir} yet, but PREBUILD is set to generate "
               "one - not run here",
               "set XCODE_PROJECT or XCODE_WORKSPACE so the lane knows which "
               "container PREBUILD produces")
    elif not found:
        record("PROJECT", FAIL,
               f"no .xcodeproj or .xcworkspace in {ios_dir}",
               "if it is generated, set PREBUILD in .github/app-config.env; "
               "otherwise set XCODE_PROJECT or XCODE_WORKSPACE")
    else:
        record("PROJECT", FAIL,
               f"{len(found)} containers in {ios_dir}: {', '.join(found)} - "
               "xcodebuild cannot pick",
               "set XCODE_WORKSPACE or XCODE_PROJECT in .github/app-config.env")


def check_bundle_version_is_variable(cfg):
    """CFBundleVersion must come from the build setting, not a literal.

    The lane computes a monotonic build number and passes it as
    CURRENT_PROJECT_VERSION. If Info.plist hardcodes CFBundleVersion instead of
    referencing $(CURRENT_PROJECT_VERSION), that number never reaches the
    binary: the first upload succeeds and every later one is rejected for not
    incrementing. Documented as a known limit; found in the wild on the first
    project the lane was adopted into.
    """
    if cfg.get("FLUTTER") == "1":
        record("BUNDLEVER", SKIP, "Flutter manages the version itself")
        return
    ios_dir = cfg.get("IOS_DIR")
    if not ios_dir or not os.path.isdir(ios_dir):
        record("BUNDLEVER", SKIP, "IOS_DIR not available")
        return

    plists = []
    for root, dirs, files in os.walk(ios_dir):
        dirs[:] = [d for d in dirs
                   if d not in ("Pods", "build", "DerivedData")
                   and not d.endswith((".xcodeproj", ".xcworkspace"))]
        plists += [os.path.join(root, f) for f in files if f == "Info.plist"]

    if not plists:
        record("BUNDLEVER", SKIP, f"no Info.plist found under {ios_dir}")
        return

    literal = []
    for path in plists:
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "CFBundleVersion" not in text:
            continue
        after = text.split("CFBundleVersion", 1)[1]
        value = after.split("<string>", 1)[1].split("</string>", 1)[0] \
            if "<string>" in after else ""
        if value and "$(" not in value:
            literal.append((path, value.strip()))

    if literal:
        shown = "; ".join(f"{p} = {v}" for p, v in literal[:2])
        # For a generated project, editing Info.plist is undone by the next
        # generate (gotcha: 'The build number cannot reach a generated
        # Info.plist you edited by hand'), so point at the source instead.
        if cfg.get("PREBUILD") or os.path.isfile(os.path.join(ios_dir, "project.yml")):
            fix = ("this Info.plist is generated - set CFBundleVersion to "
                   "$(CURRENT_PROJECT_VERSION) in project.yml's info.properties, "
                   "not in the plist, or the next generate overwrites it")
        else:
            fix = "set it to $(CURRENT_PROJECT_VERSION) in the Info.plist"
        record("BUNDLEVER", FAIL,
               f"CFBundleVersion is a literal ({shown}) - the computed build "
               "number will never reach the binary and Apple will reject the "
               "second upload",
               fix)
    else:
        record("BUNDLEVER", PASS,
               "CFBundleVersion comes from $(CURRENT_PROJECT_VERSION)")


def check_extension_point(cfg):
    """Offline, and it catches an upload rejection that otherwise arrives after
    signing, minting and archiving have all succeeded."""
    if cfg.get("HAS_EXTENSION") != "1":
        record("EXTPOINT", SKIP, "HAS_EXTENSION is not 1")
        return
    plist = cfg.get("EXTENSION_INFO_PLIST", "")
    point = cfg.get("EXTENSION_POINT_ID", "")
    if not plist or not point:
        record("EXTPOINT", FAIL,
               "HAS_EXTENSION=1 but EXTENSION_INFO_PLIST or EXTENSION_POINT_ID "
               "is not set",
               "fill both in .github/app-config.env")
        return

    # This one prints to stdout and sys.exit(1)s rather than raising a message,
    # so it is run as a subprocess and its output captured.
    env = dict(os.environ, EXTENSION_INFO_PLIST=plist, EXTENSION_POINT_ID=point)
    out = subprocess.run(
        [sys.executable, os.path.join(HERE, "check_extension_point.py")],
        capture_output=True, text=True, env=env, timeout=30)
    if out.returncode == 0:
        record("EXTPOINT", PASS, f"{point} is an identifier Apple accepts")
    else:
        text = (out.stdout + out.stderr).replace("::error::", "").strip()
        record("EXTPOINT", FAIL, text.splitlines()[0][:160] if text else "refused",
               "see references/gotchas.md, 'A Call Directory extension point is "
               "com.apple.callkit.call-directory'")


# --------------------------------------------------------------------------

def runs_on_labels():
    """The labels this repository's job asks for, and where they came from.

    Only the adopting repository's own workflow counts. The template's runs-on
    is a default, and comparing a default against somebody's real runner
    produces a confident failure about nothing -- which is the shape of error
    this whole skill exists to stop. When the repository's workflow is not
    present, say the labels were not compared rather than compare the wrong two
    things.
    """
    path = ".github/workflows/testflight.yml"
    if not os.path.isfile(path):
        return [], None
    for line in open(path):
        if line.strip().startswith("runs-on:"):
            inside = line.split("runs-on:", 1)[1].strip()
            if inside.startswith("["):
                return ([x.strip().strip("'\"")
                         for x in inside.strip("[]").split(",") if x.strip()],
                        path)
    return [], None


def main():
    ap = argparse.ArgumentParser(
        description="check every precondition that can be checked before pushing")
    ap.add_argument("--repo", help="OWNER/REPO")
    ap.add_argument("--org", help="organisation login, for an org-level runner")
    ap.add_argument("--user", help="the build account the runner runs as")
    ap.add_argument("--config", default=".github/app-config.env")
    args = ap.parse_args()

    if args.repo and args.org:
        ap.error("give --repo or --org, not both")

    cfg = load_config(args.config)

    if check_gh_cli():
        check_repo_private(args.repo)
        wanted, source = runs_on_labels()
        check_runner(args.repo, args.org, wanted, source)
        check_secrets(args.repo, args.org)
    else:
        for name in ("REPO", "RUNNER", "LABELS", "SECRETS"):
            record(name, SKIP, "gh is unavailable")

    check_mac_session(args.user)
    check_xcode()
    check_signing_probe(args.user)
    check_project_exists(cfg)
    check_bundle_version_is_variable(cfg)
    check_extension_point(cfg)
    check_apple(cfg)
    check_build_number(cfg, args.repo)

    width = max(len(n) for n, _, _, _ in RESULTS)
    print()
    for name, state, message, fix in RESULTS:
        print(f"{name.ljust(width)}  {state}  {message}")
        if fix and state != PASS:
            print(f"{' ' * width}        fix: {fix}")
    print()

    failed = [r for r in RESULTS if r[1] == FAIL]
    skipped = [r for r in RESULTS if r[1] == SKIP]
    passed = [r for r in RESULTS if r[1] == PASS]

    print(f"{len(passed)} passed, {len(failed)} failed, {len(skipped)} not checked")
    if skipped:
        # Stated rather than implied: an unrun check is not a satisfied one.
        print("Checks marked SKIP were not run. They are not passes.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
