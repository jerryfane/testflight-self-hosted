#!/usr/bin/env python3
"""Proves the archive CAN sign, BEFORE anything irreversible happens.

The whole reason this file exists: the mint writes a provisioning profile into
the owner's Apple developer account, and the archive after it is where signing
fails. THE MINT CANNOT BE MOVED AFTER THE ARCHIVE -- the archive needs the
profile to sign with. So the only lever is to prove the preconditions BEFORE the
mint, and a check placed after it merely explains why an artifact was wasted.

The three preconditions, all of which have actually failed in the lane this came
from:

  1. A VALID CODESIGNING IDENTITY EXISTS in the keychain.

     THIS SENTENCE USED TO CLAIM find-identity PROVES THE CHAIN VALIDATES. IT
     DOES NOT, and the claim cost two runner launches. find-identity -v reported
     "1 valid identity" while codesign then failed with "unable to build chain to
     self-signed root" -- its notion of valid is NARROWER than codesign's
     requirement to reach a trusted self-signed root. The missing-intermediate
     hypothesis was raised, killed on this false equivalence, and was the cause
     all along. So the chain is checked SEPARATELY, below, rather than inferred.

  2. CODESIGN CAN ACTUALLY USE THE KEY. A real test signature, because existence
     and authorisation are not access -- see can_actually_sign().

  3. THE INSTALLED PROFILE AUTHORISES THAT IDENTITY. This is what broke the
     first real ship: two profiles of the same name existed, one listing an
     older certificate and one listing the certificate the .p12's key belongs
     to. codesign can only sign with an identity the profile authorises, and a
     profile that authorises a DIFFERENT certificate fails with no diagnostic
     beyond "Command CodeSign failed".

TWO MODES, AND THE SPLIT IS NEW HERE. In the lane this came from, the app's
profile arrived as a secret, so it existed before the mint and all three checks
ran before it. In this template BOTH profiles are minted, so there is no profile
to check beforehand -- and dropping the pre-mint guard entirely would give back
exactly the protection it was written for. So:

    check_signing_preconditions.py --keychain NAME
        checks 1 and 2 only. This is the PRE-MINT guard: it is everything that
        can be known before a profile exists, and it is the part that stops a
        profile being created in the owner's account when the machine could not
        have signed with it anyway.

    check_signing_preconditions.py --keychain NAME PROFILE
        all three. Run once per minted profile, AFTER the mint. It names the
        failure rather than preventing the artifact -- that is the honest limit,
        and it is the limit the lane this came from hit too with its extension
        profile.
"""
import argparse
import hashlib
import os
import plistlib
import shutil
import tempfile
import re
import subprocess
import sys
from datetime import datetime, timezone


def die(message):
    raise SystemExit(f"::error::{message}")


def identities(keychain, injected=None):
    """SHA-1 hashes of VALID codesigning identities, from `security find-identity`.

    `injected` exists so the parsing is testable off a Mac; CI passes nothing.
    """
    if injected is None:
        out = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning", keychain],
                             capture_output=True, text=True)
        injected = out.stdout
    # "  1) A1B2C3... "Apple Distribution: Someone (TEAM)"
    return {m.group(1).upper(): m.group(2)
            for m in re.finditer(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"]*)"', injected, re.M)}


def intermediate_present(keychain, injected=None):
    """Is Apple's WWDR intermediate in the keychain?

    codesign must build leaf -> WWDR -> Apple Root. A freshly created keychain
    contains ONLY what the .p12 carried, which is the leaf and its key. Without
    the intermediate the signature fails with "unable to build chain to
    self-signed root" -- a message that appears only under a verbose build, and
    then only in the RAW job log, because verbose output pushes it past the
    truncation boundary of `gh run view --log`.

    Checked rather than assumed: the import step can succeed and still leave the
    keychain without it if the download 404s or the format changes.
    """
    if injected is None:
        out = subprocess.run(
            ["security", "find-certificate", "-c",
             "Apple Worldwide Developer Relations", keychain],
            capture_output=True, text=True)
        injected = out.stdout + out.stderr
    return "keychain:" in injected or "labl" in injected


def can_actually_sign(sha, keychain, injected=None):
    """Sign something throwaway and see whether it works.

    THIS IS THE ONLY CHECK THAT EXERCISES KEY *ACCESS*. Everything else here
    proves the identity EXISTS and is AUTHORISED; none of it proves codesign can
    USE the key. That distinction cost three runner launches: the archive failed
    with `errSecInternalComponent` while find-identity reported a valid identity
    and both profiles authorised it, because the keychain's partition list
    granted access to apple-tool: and apple: and withheld it from codesign: --
    the one binary doing the signing.

    Asserting a property of the partition list would only guard the cause we
    already know. Signing for real exercises the whole path, so it survives a
    sixth cause nobody has met yet.

    A Mach-O is required -- codesign rejects a plain text file -- so a small
    system binary is copied and signed in a temporary directory. Nothing durable
    is touched.
    """
    if injected is not None:
        return injected
    with tempfile.TemporaryDirectory() as tmp:
        probe = os.path.join(tmp, "probe")
        shutil.copy("/bin/echo", probe)
        out = subprocess.run(
            ["codesign", "--force", "--sign", sha, "--keychain", keychain, probe],
            capture_output=True, text=True)
        return out.returncode == 0, (out.stderr or out.stdout).strip()


def profile_certificates(path):
    """(name, uuid, expiry, {sha1: subject}) for the certificates a profile authorises."""
    raw = subprocess.run(["openssl", "smime", "-inform", "der", "-verify", "-noverify", "-in", path],
                         capture_output=True).stdout
    if not raw:
        die(f"could not decode {path} -- is it a .mobileprovision?")
    return parse_profile(raw)


def parse_profile(raw):
    """(name, uuid, expiry, {sha1: der}) from a decoded profile plist.

    Split out from the openssl call so the comparison this guard rests on can be
    fired at synthetic profiles offline, without a Mac and without shipping a
    real credential into the repo to test against.
    """
    d = plistlib.loads(raw)
    certs = {}
    for c in d.get("DeveloperCertificates", []):
        b = bytes(c)
        certs[hashlib.sha1(b).hexdigest().upper()] = b
    return d.get("Name"), d.get("UUID"), d.get("ExpirationDate"), certs


CANNOT_USE_KEY = (
    "errSecInternalComponent means codesign could not USE the key. Causes, in "
    "the order they were eliminated in the lane this came from:\n"
    "  1. partition list missing codesign: -- FIXED, and it was not enough\n"
    "  2. keychain not unlocked or not the default -- both set\n"
    "  3. the key imported with a -T ACL rather than -A\n"
    "  4. THE RUNNER IS NOT IN A LOGIN SESSION. macOS keychain access from a"
    " non-login session (`sudo -u other bash -lc ...`) fails exactly like this"
    " however the keychain is configured. That is a property of how the runner"
    " is launched, NOT of this lane.\n"
)


def main():
    parser = argparse.ArgumentParser(
        description="prove the archive can sign", add_help=True)
    parser.add_argument("profile", nargs="?", default=None,
                        help="a .mobileprovision to check; omit for the pre-mint guard")
    parser.add_argument("--keychain", required=True,
                        help="the throwaway keychain this build created")
    args = parser.parse_args()
    keychain = args.keychain
    profile = args.profile

    found = identities(keychain)
    print(f"valid codesigning identities in {keychain}: {len(found)}")
    for sha, name in found.items():
        print(f"  {sha[:16]}  {name}")
    if not found:
        die(
            f"NO VALID codesigning identity in {keychain}. The certificate imported "
            "but its chain does not validate -- the usual cause is Apple's WWDR "
            "intermediate being absent from a freshly created keychain. Nothing has "
            "been minted or uploaded; fix the chain and re-run."
        )

    if not intermediate_present(keychain):
        die(
            f"Apple's WWDR intermediate is NOT in {keychain}. codesign must build "
            "leaf -> WWDR -> Apple Root, and a freshly created keychain holds only "
            "what the .p12 carried. Without it signing fails with 'unable to build "
            "chain to self-signed root' -- which find-identity does NOT detect, and "
            "which only appears under a verbose build. Nothing has been minted or "
            "uploaded."
        )
    print("Apple WWDR intermediate: present")

    if profile is None:
        # PRE-MINT MODE. There is no profile yet -- both are minted by
        # ensure_profiles.py in the next step -- so the identity/authorisation
        # cross-check cannot run. What CAN run is the key-access probe, and it is
        # the expensive one to discover late: it is what turns "the archive died
        # on errSecInternalComponent after a twenty-minute build, having already
        # created a profile in the account" into a refusal that costs nothing.
        signer, signer_name = sorted(found.items())[0]
        ok, detail = can_actually_sign(signer, keychain)
        if not ok:
            die(
                f"the identity {signer_name!r} is present, but codesign CANNOT USE "
                f"IT: {detail or 'no output'}.\n" + CANNOT_USE_KEY +
                "Nothing has been minted or uploaded."
            )
        print("test signature with that identity: OK")
        print("PASS (pre-mint): a usable signing identity exists. The "
              "profile/identity cross-check runs after the profiles are minted.")
        return

    name, uuid, expires, authorised = profile_certificates(profile)
    print(f"profile {name!r} ({uuid}) authorises {len(authorised)} certificate(s), expires {expires}")

    if expires and expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        die(f"profile {name!r} ({uuid}) EXPIRED on {expires}. Regenerate it.")

    overlap = set(found) & set(authorised)
    if not overlap:
        die(
            f"THE PROFILE DOES NOT AUTHORISE ANY IDENTITY IN THE KEYCHAIN. "
            f"Profile {name!r} ({uuid}) authorises {sorted(s[:16] for s in authorised)}; "
            f"the keychain holds {sorted(s[:16] for s in found)}. codesign can only sign "
            "with an identity the profile authorises, so the archive would fail with a "
            "bare 'Command CodeSign failed'. Since this template MINTS its profiles "
            "against whatever distribution certificates App Store Connect reports, this "
            "means the account holds a distribution certificate that is NOT the one in "
            "DIST_CERT_P12_BASE64, and the profile was reused from before that "
            "certificate existed. Delete the profile in Certificates, Identifiers & "
            "Profiles so the next run mints a fresh one, or replace the org's "
            "DIST_CERT_P12_BASE64 with the matching .p12. Nothing has been uploaded."
        )

    # THE ONE THAT WOULD HAVE CAUGHT errSecInternalComponent. Everything above is
    # about existence and authorisation; this is about whether the tool can
    # actually use the key.
    signer = sorted(overlap)[0]
    ok, detail = can_actually_sign(signer, keychain)
    if not ok:
        die(
            f"the identity is present and authorised, but codesign CANNOT USE IT: "
            f"{detail or 'no output'}.\n" + CANNOT_USE_KEY +
            "Nothing has been uploaded."
        )
    print("test signature with that identity: OK")

    print(f"PASS: {len(overlap)} identity/identities are present, authorised, and "
          f"USABLE -- the archive can sign")


if __name__ == "__main__":
    main()
