#!/usr/bin/env python3
"""Mints (or reuses) EVERY App Store profile this build needs, and installs them.

Generalised from the ensure_extension_profile.py in the lane this came from. The
behaviour that changed, and why:

    That lane passed the APP's profile in as a pair of secrets
    (PROVISIONING_PROFILE_BASE64 / PROVISIONING_PROFILE_NAME) and minted only
    the EXTENSION's here. That split cannot be a template. A per-app secret is
    per-app credentials, which is the one thing this template exists to remove,
    and the exported base64 is also the thing that broke its first real
    ship: two files named MyApp.mobileprovision existed, one listing an Aug-4
    certificate and one listing the Aug-19 certificate the .p12's key belongs
    to, and codesign cannot sign with an identity the profile does not
    authorise.

    So BOTH profiles are minted-or-reused here, against the certificates App
    Store Connect says this account holds. A profile made this way authorises
    the distribution certificate by construction rather than by hoping the
    exported one matched.

WHAT THIS SCRIPT WILL NOT DO: create a bundle id, enable App Groups on one, add
a group to one, or DELETE ANYTHING from the Apple account. Those are one-time
setup on the owner's account. App Groups in particular cannot be configured here
without the App Group resource id, and a profile minted against a bundle id with
no App Groups entitlement builds and signs and installs and then the extension
reads an empty container -- it fails at the far end, on a device, silently. So
this checks and stops with an instruction instead.

PRESENCE IS NOT CONTENT. This file has now made the same mistake twice, one
level apart, and both fixes are load-bearing:

    reuse path  a profile's NAME, STATE and TYPE are metadata; what makes it
                usable is what its ENTITLEMENTS GRANT. A profile minted before
                the group was attached looks perfect forever.

    mint path   a bundle id's CAPABILITY LIST is metadata; what makes it mintable
                is which groups are ATTACHED. "APP_GROUPS is enabled" can be true
                with no group attached, or a different one, and Apple will mint a
                profile that grants nothing useful -- right name, right state,
                right type, passes every metadata check here, then fails at
                CodeSign with a bare unhelpful error.

Both are therefore checked by asking what is GRANTED (require_group_granted),
and the mint path additionally checks the attachment BEFORE minting where Apple
reports it (check_app_group_attached). Where Apple does not report it, that is
ANNOUNCED as unverifiable rather than quietly downgraded to the weaker claim --
see unverifiable_here().

Configuration comes from the environment, which the workflow fills from
.github/app-config.env:

    APP_BUNDLE_ID           required
    APP_PROFILE_NAME        required
    PROFILE_TYPE            default IOS_APP_STORE
    HAS_EXTENSION           "1" to also handle the extension
    EXTENSION_BUNDLE_ID     required when HAS_EXTENSION=1
    EXTENSION_PROFILE_NAME  required when HAS_EXTENSION=1
    APP_GROUP               optional; when set, enforced on BOTH profiles
    EXPORT_OPTIONS_PATH     required; where ExportOptions.plist is written
    APPLE_TEAM_ID           required
    GITHUB_ENV              optional; profile paths are recorded here for cleanup
"""
import base64
import os
import plistlib
import re

import asc

PROFILES_DIR = os.path.expanduser("~/Library/MobileDevice/Provisioning Profiles")


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        asc.die(
            f"{name} is not set. It comes from .github/app-config.env via the "
            "workflow's 'Load the app configuration' step; nothing has been "
            "minted or uploaded."
        )
    return value


PROFILE_TYPE = os.environ.get("PROFILE_TYPE", "").strip() or "IOS_APP_STORE"
# The group the entitlements actually demand. A profile that does not grant THIS
# is unusable no matter how correct its name and state look. Empty disables the
# check; it never means "any group".
APP_GROUP = os.environ.get("APP_GROUP", "").strip()
HAS_EXTENSION = os.environ.get("HAS_EXTENSION", "0").strip() == "1"

# Named by run id as well as uuid so a file this job writes into the OWNER'S
# REAL profile directory can never collide with, overwrite, or on cleanup delete
# one of THEIR profiles. The lane this came from hardcoded `app.mobileprovision`
# once and that is exactly the hazard; a uuid alone still collides with the same
# profile already installed by Xcode.
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")

_certificates_cache = None


def distribution_certificates() -> list:
    """Resource ids of this account's distribution certificates.

    Cached: it is asked once per profile otherwise, and each ask is a chance for
    a transient failure on the mint path.
    """
    global _certificates_cache
    if _certificates_cache is not None:
        return _certificates_cache

    status, body = asc.get("/v1/certificates?filter%5BcertificateType%5D=DISTRIBUTION")
    if status != 200:
        asc.die(f"App Store Connect returned {status} listing certificates")
    identifiers = [item["id"] for item in body.get("data", [])]
    if not identifiers:
        asc.die(
            "No distribution certificate on this account. The one whose .p12 is "
            "in DIST_CERT_P12_BASE64 must also exist here."
        )
    _certificates_cache = identifiers
    return identifiers


def bundle_id_resource(bundle_id: str) -> str:
    """The resource id of an EXISTING bundle id, or a stop with instructions."""
    status, body = asc.get(
        f"/v1/bundleIds?filter%5Bidentifier%5D={bundle_id}"
        "&include=bundleIdCapabilities"
    )
    if status != 200:
        asc.die(f"App Store Connect returned {status} listing bundle ids")

    # filter[identifier] is a CONTAINS-style match on Apple's side, so asking for
    # com.example.app can answer with com.example.app.extension as well. Picking
    # data[0] would then mint a profile against the WRONG bundle id, which signs
    # and archives and is rejected at upload. Match exactly.
    data = [
        item
        for item in body.get("data", [])
        if item.get("attributes", {}).get("identifier") == bundle_id
    ]
    if not data:
        asc.die(
            f"No bundle id {bundle_id} on this account. Create it in "
            "Certificates, Identifiers & Profiles"
            + (
                f", enable the App Groups capability on it, and add it to "
                f"{APP_GROUP}. "
                if APP_GROUP
                else ". "
            )
            + "This script does not create it, because it cannot enable "
            "capabilities on it, and a profile without the capabilities the "
            "entitlements demand fails only later, on a device, silently."
        )

    if APP_GROUP:
        check_app_group_attached(data[0], body, bundle_id)

    return data[0]["id"]


def capabilities_of(entry: dict, body: dict):
    """The bundleIdCapabilities belonging to THIS bundle id, or None if unknowable.

    `included` is a flat list covering EVERY bundle id in `data`, and Apple's
    filter is a contains-match, so for com.example.app the response routinely
    also describes com.example.app.extension. Reading capabilities out of the
    flat list therefore answers a question about SOME bundle id, not about the
    one being minted against -- and the extension having App Groups enabled
    would happily vouch for an app that does not.

    None means "cannot be attributed", never "none". The caller must not read it
    as an answer.
    """
    included = [
        item for item in body.get("included", [])
        if item.get("type") == "bundleIdCapabilities"
    ]
    related = (
        ((entry.get("relationships") or {}).get("bundleIdCapabilities") or {})
        .get("data")
    )
    if related is not None:
        wanted = {item.get("id") for item in related}
        return [item for item in included if item.get("id") in wanted]
    if len(body.get("data", [])) == 1:
        # Only one bundle id came back, so everything included is its own.
        return included
    return None


def _group_identifiers(node) -> set:
    """Every 'group.*' string anywhere in a JSON-ish structure.

    Deliberately a blind walk rather than a path lookup. Apple documents
    bundleIdCapabilities.attributes.settings only loosely, the shape has changed
    before, and for APP_GROUPS it is frequently null. A walk finds the attached
    identifiers if they are reported ANYWHERE in the capability, and finds
    nothing if they are not reported at all -- and the caller treats those two
    outcomes very differently.
    """
    if isinstance(node, str):
        return {node} if node.startswith("group.") else set()
    if isinstance(node, dict):
        found = set()
        for value in node.values():
            found |= _group_identifiers(value)
        return found
    if isinstance(node, (list, tuple)):
        found = set()
        for value in node:
            found |= _group_identifiers(value)
        return found
    return set()


def unverifiable_here(reason: str) -> None:
    """Announce that a check could not be made, and where it IS made instead.

    AN UNVERIFIABLE CHECK MUST ANNOUNCE THAT IT IS UNVERIFIABLE. The failure this
    guards against is not a wrong answer, it is a check that quietly asserts a
    WEAKER property than the one it claims and reads, in a green log, exactly
    like the strong one.
    """
    print(f"::warning::cannot verify before minting that {APP_GROUP} is attached "
          f"to the bundle id: {reason}. This is NOT a pass. The same property is "
          "checked on the minted profile's own entitlements immediately after "
          "the mint, which is the earliest place it can be established -- see "
          "require_group_granted().")


def check_app_group_attached(entry: dict, body: dict, bundle_id: str) -> None:
    """Verify APP_GROUP is ATTACHED to the bundle id, not merely that the
    App Groups capability is switched on.

    THE PRESENCE-VS-CONTENT BUG, ON THE MINT PATH. The reuse path already
    refuses a profile whose ENTITLEMENTS do not grant the group; this is the same
    mistake one level up. "APP_GROUPS is in the capability list" says the
    capability is ENABLED. It says nothing about WHICH groups are attached, and
    the capability can be enabled with none attached at all, or with a different
    group attached. Apple then mints a profile that grants nothing useful: it has
    the right name, the right state, the right type, it passes every metadata
    check in this file, and it fails at CodeSign with a bare unhelpful error.
    That class of defect cost multiple real ship attempts.
    """
    capabilities = capabilities_of(entry, body)
    if capabilities is None:
        unverifiable_here(
            f"Apple's contains-style filter returned {len(body.get('data', []))} "
            "bundle ids and the response does not relate the capabilities to one "
            "of them"
        )
        return

    types = {
        item.get("attributes", {}).get("capabilityType") for item in capabilities
    }
    # Still worth asserting: the capability being OFF is a definite failure, and
    # it is the one the API reports unambiguously.
    if "APP_GROUPS" not in types:
        asc.die(
            f"{bundle_id} exists but does not have the App Groups capability "
            f"enabled. Enable it and add {APP_GROUP}. Without it the app and its "
            "extension cannot share a container, and nothing about the build or "
            "the upload will say so."
        )

    reported = _group_identifiers(capabilities)
    if not reported:
        # The capability is on, and Apple did not say which groups are attached.
        # "No groups reported" and "groups not reported" are DIFFERENT, this
        # response cannot distinguish them, and only one of them is an error --
        # so neither dying nor passing quietly is honest here.
        unverifiable_here(
            f"the App Groups capability is enabled on {bundle_id} but the "
            "bundleIdCapabilities response lists no attached group identifiers, "
            "which this API does not reliably report"
        )
        return

    if APP_GROUP not in reported:
        asc.die(
            f"{bundle_id} has the App Groups capability enabled, but {APP_GROUP} "
            f"is NOT attached to it -- Apple reports {sorted(reported)}. The "
            "capability being ON is not the same as the group being ATTACHED, and "
            "a profile minted now would be granted no useful app group: it would "
            "have the right name, state and type, pass every metadata check, and "
            "fail at CodeSign with a bare unhelpful error. Attach the group in "
            "Certificates, Identifiers & Profiles. NOTHING HAS BEEN MINTED."
        )
    print(f"{bundle_id}: {APP_GROUP} is attached")


def require_group_granted(content, profile_name: str, minted: bool) -> None:
    """Refuse a profile whose ENTITLEMENTS do not grant APP_GROUP.

    What makes a profile usable is what it GRANTS, not what it is named and not
    which capability flags are set on its bundle id. This is the single check
    that establishes that, and it runs on BOTH paths -- reuse and mint -- because
    both can produce a profile that grants nothing.

    On the mint path it necessarily runs AFTER the irreversible write, so it
    reports the profile as existing rather than pretending it prevented it. That
    is the honest limit: the strongest pre-mint statement available is
    check_app_group_attached(), and when Apple does not report the attachment
    this is the earliest place the truth can be known at all.
    """
    granted = entitlements_of(content).get(
        "com.apple.security.application-groups", []
    )
    if APP_GROUP in granted:
        return

    uuid = profile_uuid(content) or "unknown uuid"
    if minted:
        asc.die(
            f"the profile {profile_name!r} ({uuid}) WAS CREATED in the account, "
            f"and it does NOT grant {APP_GROUP} -- it grants "
            f"{list(granted) or 'no app groups'}. The App Groups capability is "
            f"enabled on the bundle id but {APP_GROUP} is evidently not attached "
            "to it, so Apple minted a profile that authorises nothing useful. "
            "Attach the group in Certificates, Identifiers & Profiles, then "
            f"DELETE the {profile_name!r} profile so the next run mints a fresh "
            "one -- this script will not delete anything from the account itself, "
            "and retrying without deleting it will just reuse this broken one. "
            "Nothing has been uploaded."
        )
    asc.die(
        f"the existing {profile_name!r} profile ({uuid}) does NOT grant "
        f"{APP_GROUP}; it grants {list(granted) or 'no app groups'}. It was "
        "almost certainly minted before the group was attached to the bundle id. "
        "DELETE IT in Certificates, Identifiers & Profiles so the next run mints "
        "a fresh one -- this script will not delete anything from the account "
        "itself. Reusing it would fail at CodeSign in exactly the way it did "
        "before."
    )


def entitlements_of(content) -> dict:
    """The Entitlements dict inside a base64 .mobileprovision, or {} if unreadable.

    Unreadable returns {} rather than raising: the caller treats a profile whose
    entitlements cannot be read as one that does not grant what is needed, which
    is the safe direction -- it refuses to reuse rather than reusing blind.
    """
    if not content:
        return {}
    try:
        raw = base64.b64decode(content)
    except Exception:  # noqa: BLE001
        return {}
    match = re.search(rb"<plist.*?</plist>", raw, re.S)
    if not match:
        return {}
    try:
        return plistlib.loads(match.group(0)).get("Entitlements", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def profile_uuid(content) -> str:
    """The UUID out of a base64 .mobileprovision, for naming it in an error."""
    if not content:
        return ""
    try:
        raw = base64.b64decode(content)
    except Exception:  # noqa: BLE001
        return ""
    match = re.search(rb"<key>UUID</key>\s*<string>([-0-9A-Fa-f]+)</string>", raw)
    return match.group(1).decode() if match else ""


def usable_existing_profile(profile_name: str):
    """The ACTIVE profile of this name and type, or None if there is none.

    None means ONE thing: Apple answered, and no usable profile exists. It must
    never mean "the question could not be asked", because the caller reads None
    as permission to MINT -- and minting writes a persistent artifact into the
    owner's developer account. A 401, a 403 on scope, or a transient 500 used to
    return None here, so a network blip would have silently created a second
    profile instead of failing. Failing to read is not the same as reading
    "nothing", and only one of them may lead to a write.
    """
    status, body = asc.get(
        f"/v1/profiles?filter%5Bname%5D={profile_name.replace(' ', '%20')}"
    )
    if status != 200:
        asc.die(
            f"App Store Connect returned {status} listing profiles named "
            f"{profile_name!r}. Refusing to continue: this script cannot tell an "
            "empty account from an unanswered question, and the next step would "
            "create a profile in the owner's account."
        )
    for item in body.get("data", []):
        attributes = item.get("attributes", {})
        # profileType as well as state. A profile of this name could exist as
        # IOS_APP_DEVELOPMENT, and reusing it for an App Store build fails later
        # at signing, where the cause is much harder to see than it is here.
        if not (
            attributes.get("profileState") == "ACTIVE"
            and attributes.get("profileType") == PROFILE_TYPE
        ):
            continue

        content = attributes.get("profileContent")
        # NAME, STATE AND TYPE ARE METADATA. The thing that makes a profile
        # usable is its ENTITLEMENTS, and profiles capture those AT CREATION --
        # so a profile minted before the app group was attached to the bundle id
        # stays broken forever while continuing to look perfect here. Reusing it
        # reproduces the original CodeSign failure exactly, which reads as "the
        # fix did not work" rather than as "you are holding a stale profile".
        if APP_GROUP:
            require_group_granted(content, profile_name, minted=False)
        return content
    return None


def mint_profile(profile_name: str, bundle_resource: str) -> str:
    status, body = asc.post(
        "/v1/profiles",
        {
            "data": {
                "type": "profiles",
                "attributes": {
                    "name": profile_name,
                    "profileType": PROFILE_TYPE,
                },
                "relationships": {
                    "bundleId": {
                        "data": {"type": "bundleIds", "id": bundle_resource}
                    },
                    "certificates": {
                        "data": [
                            {"type": "certificates", "id": identifier}
                            for identifier in distribution_certificates()
                        ]
                    },
                },
            }
        },
    )
    if status not in (200, 201):
        asc.die(f"could not create the profile {profile_name!r}: {status} {body}")

    # Read the response defensively. This is the ONE call in the lane that
    # creates something in the owner's Apple account, so the moment after it
    # succeeds is the moment a surprise is most expensive: the artifact already
    # exists and a KeyError traceback here would report it as a crash rather than
    # as "minted, but the answer was not what we expected".
    attributes = (body.get("data") or {}).get("attributes") or {}
    content = attributes.get("profileContent")
    if not content:
        asc.die(
            "the profile was created but App Store Connect returned no "
            f"profileContent. It may now EXIST in the account -- check for "
            f"{profile_name!r} before retrying, so a second one is not made. "
            f"Response: {body}"
        )
    # Minted under a name and type we did not ask for is not a profile we can
    # use, and silently signing with it would fail later and more obscurely.
    for field, expected in (("name", profile_name), ("profileType", PROFILE_TYPE)):
        actual = attributes.get(field)
        if actual is not None and actual != expected:
            asc.die(
                f"minted profile has {field}={actual!r}, expected {expected!r}. "
                "It exists in the account; do not retry blindly."
            )

    # AND WHAT IT ACTUALLY GRANTS. Everything above this line is metadata about
    # a profile that now exists; this is the only statement about whether it is
    # USABLE. A bundle id with the App Groups capability enabled but no group
    # attached mints a profile that passes every check above and grants nothing.
    if APP_GROUP:
        require_group_granted(content, profile_name, minted=True)
    return content


def install(content: str, profile_name: str, env_var: str, tag: str) -> str:
    """Drop the profile where xcodebuild looks for it, and record the path."""
    raw = base64.b64decode(content)
    match = re.search(rb"<key>UUID</key>\s*<string>([-0-9A-Fa-f]+)</string>", raw)
    if not match:
        asc.die(f"the profile {profile_name!r} has no UUID -- refusing to install it")
    uuid = match.group(1).decode()

    # run id, ROLE, and uuid. The role is in there because two profiles that
    # somehow carried the same uuid would otherwise land on the same filename,
    # and the second would silently overwrite the first -- which reads as "the
    # extension is signed with the app's profile", a failure that surfaces at
    # codesign with nothing pointing back here.
    os.makedirs(PROFILES_DIR, exist_ok=True)
    path = os.path.join(PROFILES_DIR, f"ci-{RUN_ID}-{tag}-{uuid}.mobileprovision")
    with open(path, "wb") as handle:
        handle.write(raw)
    print(f"installed {profile_name} ({uuid}) at {path}")

    # Tell the workflow what was written so its always() cleanup can remove it.
    # PROFILES_DIR is the REAL user's profile directory: on a hosted runner the
    # VM is destroyed and nobody notices, but this lane runs on the org's
    # persistent Mac, where an uncleaned file simply stays. The workflow's audit
    # of what it writes reads its own run: blocks, so a write performed HERE is
    # invisible to it -- which is exactly how the ones in the lane this came from
    # survived three passes.
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as handle:
            handle.write(f"{env_var}={path}\n")
    return path


def ensure(profile_name: str, bundle_id: str, env_var: str, tag: str) -> None:
    existing = usable_existing_profile(profile_name)
    if existing:
        print(f"reusing the active {profile_name}")
        install(existing, profile_name, env_var, tag)
    else:
        print(f"no active {PROFILE_TYPE} profile named {profile_name!r}; minting one")
        content = mint_profile(profile_name, bundle_id_resource(bundle_id))
        install(content, profile_name, env_var, tag)


def write_export_options(app_bundle_id, app_profile, extension_bundle_id,
                         extension_profile) -> None:
    """Render ExportOptions.plist naming the profiles actually in play.

    It is written rather than committed because a plist interpolates nothing:
    the names have to be substituted by something, and this is the only place
    that knows which profiles the run ended up with.
    """
    team = required("APPLE_TEAM_ID")
    path = required("EXPORT_OPTIONS_PATH")

    profiles = {app_bundle_id: app_profile}
    if extension_bundle_id:
        profiles[extension_bundle_id] = extension_profile

    options = {
        "method": "app-store-connect",
        "teamID": team,
        "signingStyle": "manual",
        "uploadSymbols": True,
        "stripSwiftSymbols": True,
        "destination": "export",
        "provisioningProfiles": profiles,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(options, handle)
    print(f"wrote {path} for team {team}:")
    for bundle, name in profiles.items():
        print(f"  {bundle} -> {name}")


def main() -> None:
    app_bundle_id = required("APP_BUNDLE_ID")
    app_profile = required("APP_PROFILE_NAME")

    extension_bundle_id = extension_profile = ""
    if HAS_EXTENSION:
        extension_bundle_id = required("EXTENSION_BUNDLE_ID")
        extension_profile = required("EXTENSION_PROFILE_NAME")
        if extension_profile == app_profile:
            asc.die(
                "EXTENSION_PROFILE_NAME and APP_PROFILE_NAME are the same string. "
                "One profile cannot cover two bundle ids, and reusing the name "
                "would make each run install whichever profile Apple returned "
                "first. Give them distinct names."
            )

    ensure(app_profile, app_bundle_id, "INSTALLED_APP_PROFILE", "app")
    if HAS_EXTENSION:
        ensure(extension_profile, extension_bundle_id,
               "INSTALLED_EXTENSION_PROFILE", "extension")

    write_export_options(
        app_bundle_id, app_profile, extension_bundle_id, extension_profile
    )


if __name__ == "__main__":
    main()
