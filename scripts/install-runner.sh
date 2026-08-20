#!/usr/bin/env bash
#
# install-runner.sh — install a STANDING GitHub Actions runner on a Mac, in the
# build account's GUI login session, so that codesign can reach the keychain.
#
# The one fact this whole script exists to enforce: the runner service must live
# in the build user's `Aqua` launchd domain. Anywhere else, codesign fails with
# errSecInternalComponent no matter how the keychain is configured, and the
# error names nothing that would lead you back to the session.
#
# Run it AS YOUR ADMIN ACCOUNT (it will sudo where it needs to), or as the build
# user from that user's own Terminal inside a graphical login.
#
# Usage:
#   ./install-runner.sh --user builder --repo OWNER/REPO --labels self-hosted,macOS,ios
#   ./install-runner.sh --user builder --org  YOUR_ORG  --name mac-runner-01
#
set -euo pipefail

# --------------------------------------------------------------- settings ---
# Edit these, or override any of them with the flags below.

# Dedicated NON-ADMIN macOS account that owns the runner and the signing
# keychain. Never an admin account, and never your own login.
BUILD_USER="${BUILD_USER:-builder}"

# Exactly one of these. REPO registers the runner to a single repository; ORG
# registers it to every repository in an organization (see setup-org.md).
REPO="${REPO:-}"          # OWNER/REPO
ORG="${ORG:-}"            # YOUR_ORG

# Runner directory, relative to the build user's home. One registration lives in
# one directory: config.sh writes a single .runner file, so pointing a second
# registration at the same directory overwrites the first without warning.
RUNNER_DIR="${RUNNER_DIR:-actions-runner}"

# Name shown in the GitHub runners list. Defaults to the short hostname.
RUNNER_NAME="${RUNNER_NAME:-}"

# Labels workflows select on. A job requesting a label nobody has does not
# error, it queues forever showing "Waiting for a runner". Prefer capability
# labels (ios, xcode26) over per-app ones.
LABELS="${LABELS:-self-hosted,macOS,ios}"

# Runner release to install. Empty means "resolve the latest at install time".
RUNNER_VERSION="${RUNNER_VERSION:-}"

# ------------------------------------------------------------------------- #

die() { printf 'install-runner: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --user)    BUILD_USER="${2:-}"; shift 2 ;;
    --repo)    REPO="${2:-}"; shift 2 ;;
    --org)     ORG="${2:-}"; shift 2 ;;
    --dir)     RUNNER_DIR="${2:-}"; shift 2 ;;
    --name)    RUNNER_NAME="${2:-}"; shift 2 ;;
    --labels)  LABELS="${2:-}"; shift 2 ;;
    --version) RUNNER_VERSION="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *)         die "unknown argument: $1" ;;
  esac
done

# --- refuse root -------------------------------------------------------------
# svc.sh checks for uid 0 and exits, so running the whole thing as root fails
# at the last and slowest step. It would also leave the runner directory owned
# by root inside another user's home, which then has to be chown'd back before
# anything works. Refuse early and say why.
if [ "$(id -u)" -eq 0 ]; then
  die "do not run this as root. svc.sh rejects uid 0, and a root-owned runner
directory in ${BUILD_USER}'s home has to be undone by hand. Run it as your
admin account (it sudo's where needed) or as ${BUILD_USER} in that user's own
graphical login."
fi

[ -n "$BUILD_USER" ] || die "--user is required"
if [ -n "$REPO" ] && [ -n "$ORG" ]; then
  die "give --repo or --org, not both"
fi
[ -n "$REPO$ORG" ] || die "one of --repo OWNER/REPO or --org YOUR_ORG is required"
command -v gh >/dev/null 2>&1 || die "gh is required, authenticated as an admin
of the target. It is deliberately NOT installed or logged in inside
${BUILD_USER}: a gh token in the build account can act as you across every repo
you own, and that account runs workflow-authored code."
gh auth status >/dev/null 2>&1 || die "gh is not authenticated. Run 'gh auth login' as yourself."

# --- resolve the build user's uid -------------------------------------------
# DERIVED, never hardcoded. A literal 502 is correct on exactly one machine and
# silently targets the wrong session on every other one -- launchctl asuser
# takes any uid without complaint, so a wrong number installs the service into
# somebody else's domain and the failure surfaces later, as a signing error.
BUILD_UID="$(id -u "$BUILD_USER" 2>/dev/null)" \
  || die "no such user: $BUILD_USER (create it first; see setup-runner.md)"
BUILD_HOME="$(dscl . -read "/Users/$BUILD_USER" NFSHomeDirectory 2>/dev/null \
  | awk '{print $2}')"
[ -n "$BUILD_HOME" ] || die "cannot resolve home directory for $BUILD_USER"

[ -n "$RUNNER_NAME" ] || RUNNER_NAME="$(hostname -s)"
RUNNER_PATH="$BUILD_HOME/$RUNNER_DIR"

if [ -n "$ORG" ]; then
  RUNNER_URL="https://github.com/$ORG"
  TOKEN_API="/orgs/$ORG/actions/runners/registration-token"
  LIST_API="/orgs/$ORG/actions/runners"
else
  RUNNER_URL="https://github.com/$REPO"
  TOKEN_API="repos/$REPO/actions/runners/registration-token"
  LIST_API="repos/$REPO/actions/runners"
fi

# --- how we reach the build user's GUI session -------------------------------
# `sudo -u USER` changes the user and leaves the SESSION alone: it lands in the
# Background domain, where the keychain is unreachable. `launchctl asuser UID`
# is the only non-interactive way into the Aqua domain, so every command that
# touches config.sh or svc.sh goes through run_as_build().
#
# Confirm with `launchctl managername`: it prints Aqua in a graphical login and
# Background under ssh or plain sudo.
run_as_build() {
  if [ "$(id -un)" = "$BUILD_USER" ]; then
    bash -lc "$1"
  else
    sudo launchctl asuser "$BUILD_UID" sudo -u "$BUILD_USER" -H bash -lc "$1"
  fi
}

info "build user: $BUILD_USER (uid $BUILD_UID)"
info "runner dir: $RUNNER_PATH"
info "target:     $RUNNER_URL"
info "labels:     $LABELS"

session="$(run_as_build 'launchctl managername' 2>/dev/null || true)"
if [ "$session" != "Aqua" ]; then
  die "the build user's launchd session reports '${session:-unknown}', not Aqua.
$BUILD_USER must be logged in graphically (fast user switching, at the login
window) before the runner is installed. Log in as $BUILD_USER, switch back, and
run this again."
fi
info "session:    Aqua (codesign can reach the keychain)"

# --- download ----------------------------------------------------------------
if [ -z "$RUNNER_VERSION" ]; then
  RUNNER_VERSION="$(gh api repos/actions/runner/releases/latest --jq .tag_name \
    | sed 's/^v//')"
fi
[ -n "$RUNNER_VERSION" ] || die "could not resolve the latest runner version"

case "$(uname -m)" in
  arm64)  RUNNER_ARCH="osx-arm64" ;;
  x86_64) RUNNER_ARCH="osx-x64" ;;
  *)      die "unsupported architecture: $(uname -m)" ;;
esac

TARBALL="actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
DOWNLOAD_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${TARBALL}"

if run_as_build "test -x '$RUNNER_PATH/config.sh'"; then
  info "runner binaries already present in $RUNNER_PATH, skipping download"
else
  info "downloading runner $RUNNER_VERSION ($RUNNER_ARCH)"
  run_as_build "set -euo pipefail
    mkdir -p '$RUNNER_PATH'
    cd '$RUNNER_PATH'
    curl -fsSL -o '$TARBALL' '$DOWNLOAD_URL'
    tar xzf '$TARBALL'
    rm -f '$TARBALL'"
fi

# --- mint the registration token ---------------------------------------------
# Minted here, inline, and used within seconds. Registration tokens expire in
# about an hour and are single-use, so a token pasted from a document or a chat
# log is either dead or already spent -- and a dead token makes config.sh return
# a 404 that reads like a wrong repository name. This script deliberately has no
# flag for supplying one.
#
# Minted as the ADMIN account. The build account holds no GitHub credentials.
info "minting a registration token"
TOKEN="$(gh api -X POST "$TOKEN_API" --jq .token)"
[ -n "$TOKEN" ] || die "failed to mint a registration token (missing admin rights, or admin:org scope for an org)"

# --- register ----------------------------------------------------------------
# THE TOKEN GOES IN ON STDIN, NOT ON A COMMAND LINE. Anything interpolated into
# a command line is visible to `ps` for as long as the process lives, to every
# user on the machine. Passing it through stdin keeps it out of this script's
# argv, out of the sudo and launchctl command lines, and out of shell history.
#
# --unattended: no prompts.
# NO --ephemeral: an ephemeral runner de-registers after exactly ONE job, which
#   looks identical to a crash -- the first job goes green and the next push
#   queues forever against a runner that no longer exists. Omitting the flag is
#   what makes this runner standing.
# --replace: re-registering the same name replaces it instead of failing.
#
# Warm sudo's timestamp first. If sudo has to prompt for a password while stdin
# is a pipe, it reads the token as the password: the registration then fails
# with an authentication error that says nothing about either problem, and the
# token is spent.
if [ "$(id -un)" != "$BUILD_USER" ]; then
  sudo -v
fi

info "registering runner '$RUNNER_NAME' (non-ephemeral)"
printf '%s' "$TOKEN" | run_as_build "set -euo pipefail
  cd '$RUNNER_PATH'
  IFS= read -r tok
  ./config.sh --unattended \
    --url '$RUNNER_URL' \
    --token \"\$tok\" \
    --name '$RUNNER_NAME' \
    --labels '$LABELS' \
    --replace
  unset tok"

unset TOKEN

# --- install and start the service -------------------------------------------
# On macOS svc.sh install writes a LaunchAgent to ~/Library/LaunchAgents and
# launchctl load -w's it into the per-user Aqua domain. That is exactly right.
#
# Do NOT replace this with a LaunchDaemon. A daemon starts at boot with no
# login, which is the property that makes it tempting, and it has no GUI
# session, which is the property that makes codesign fail.
info "installing and starting the launchd service"
run_as_build "set -euo pipefail
  cd '$RUNNER_PATH'
  ./svc.sh install
  ./svc.sh start
  ./svc.sh status || true"

# --- what to check now -------------------------------------------------------
cat <<EOF

Installed. Verify all of the following before trusting it.

1. Registered, online, and STANDING. Both fields matter -- an ephemeral runner
   passes the online check and still disappears after one job:

     gh api $LIST_API \\
       --jq '.runners[] | {name, status, ephemeral, labels: [.labels[].name]}'

   Expect status "online" and ephemeral null or false.

2. launchd holds it, in the right domain. Run these in $BUILD_USER's own
   Terminal, inside the graphical login:

     launchctl list | grep actions.runner
     launchctl managername          # must print Aqua

3. It survives a job. Run one workflow, let it finish, then repeat check 1.
   Still online after a completed job is the whole difference between a
   standing runner and an ephemeral one.

4. Reboot persistence. A LaunchAgent runs only while its user is logged in, so
   enable auto-login for $BUILD_USER (setup-runner.md section 7) and understand
   what /etc/kcpassword costs before you do.

Switch back to your own account with fast user switching. Logging $BUILD_USER
out kills its LaunchAgents and takes the runner offline with no other symptom.
EOF
