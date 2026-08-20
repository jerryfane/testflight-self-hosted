#!/usr/bin/env python3
"""Assert an app extension's Info.plist declares an extension point Apple accepts.

WHY THIS EXISTS, AND WHY IT IS A LANE CHECK RATHER THAN A BUILD CHECK.

One ship got further than any run before it: the certificate installed, the
guard proved codesign could use the key, both profiles authorised the identity,
the archive built and signed, and the app and the appex agreed on both version
keys in the real .ipa. Then the UPLOAD failed:

    ERROR: [altool] Invalid Info.plist value. The value of the
    NSExtensionPointIdentifier key, com.apple.identitylookup.call-directory,
    in the Info.plist of "Runner.app/PlugIns/MyAppExtension.appex" is
    invalid. (90349)

The correct value for a Call Directory extension is
com.apple.callkit.call-directory. The identitylookup family is REAL -- it holds
message-filter and classification-ui -- which is precisely why the wrong value
survived every review: it is well formed, it is plausible, and it names a
namespace that exists.

NOTHING IN A LOCAL BUILD HAS AN OPINION ABOUT IT. Xcode compiles, links, embeds
and signs an appex with a bogus extension point without a word. The archive
succeeds. The .ipa is well formed. check_bundle_versions.py reads that same
Info.plist and passes, because it asks about versions. Apple's upload validator
is the ONLY reader that checks this, and it runs at the last step of the lane,
after the runner launch, the signing, and the archive.

So run this as early as you can. It needs no Mac, no credentials and no build:
put it in the ordinary Linux gate on every commit if the repo has one, as well
as in this lane.

    EXTENSION_INFO_PLIST=ios/Foo/Info.plist \
    EXTENSION_POINT_ID=com.apple.callkit.call-directory \
    python3 .github/scripts/check_extension_point.py

Or positionally:

    python3 .github/scripts/check_extension_point.py <plist> <extension-point-id>

EXTENSION_PRINCIPAL_CLASS_SUFFIX is optional; when set, NSExtensionPrincipalClass
must end with it.
"""
import os
import plistlib
import sys

# Apple's App Extension Keys reference. Call Directory is NOT in the
# identitylookup family, which is the whole trap. These are the values that have
# actually been written by mistake, kept as named hints rather than a bare
# "wrong" so the next person does not repeat the same forty minutes.
KNOWN_WRONG = {
    "com.apple.identitylookup.call-directory":
        "the identitylookup family holds message-filter and "
        "classification-ui; Call Directory is not in it -- the correct value is "
        "com.apple.callkit.call-directory",
    "com.apple.callkit.calldirectory":
        "the segment is hyphenated: com.apple.callkit.call-directory",
    "com.apple.identitylookup.message-filter":
        "that is the Message Filter extension point, a different extension",
}


def die(message):
    print(f"::error::{message}")
    sys.exit(1)


def main():
    argv = sys.argv[1:]
    plist_path = argv[0] if len(argv) > 0 else os.environ.get("EXTENSION_INFO_PLIST", "")
    expected = argv[1] if len(argv) > 1 else os.environ.get("EXTENSION_POINT_ID", "")
    suffix = os.environ.get("EXTENSION_PRINCIPAL_CLASS_SUFFIX", "").strip()

    if not plist_path:
        die("no extension Info.plist given. Set EXTENSION_INFO_PLIST in "
            ".github/app-config.env or pass the path as the first argument.")
    if not expected:
        die(f"no expected extension point given for {plist_path}. Set "
            "EXTENSION_POINT_ID in .github/app-config.env. This check refuses to "
            "guess: accepting whatever the plist happens to say would make it a "
            "check that passes on the exact defect it exists to catch.")

    try:
        with open(plist_path, "rb") as handle:
            plist = plistlib.load(handle)
    except FileNotFoundError:
        die(f"{plist_path} does not exist. The extension cannot ship without it, "
            "and its absence is not something the archive reports.")
    except Exception as exc:  # noqa: BLE001 - a malformed plist is a real defect
        die(f"{plist_path} could not be parsed: {exc}. An unparseable Info.plist "
            "is not evidence that its contents are right.")

    extension = plist.get("NSExtension")
    if not isinstance(extension, dict):
        die(f"{plist_path} has no NSExtension dictionary. An appex without one is "
            "not an app extension, and Xcode will still build it.")

    point = extension.get("NSExtensionPointIdentifier")
    if point != expected:
        hint = KNOWN_WRONG.get(point)
        die(
            f"NSExtensionPointIdentifier is {point!r}, expected {expected!r}"
            + (f" -- {hint}" if hint else "")
            + ". Apple rejects the wrong value at UPLOAD with error 90349, which "
            "is after the runner launch, the signing and the archive. Nothing in "
            "a local build checks it."
        )

    principal = extension.get("NSExtensionPrincipalClass")
    if not principal or not principal.strip():
        die("NSExtensionPrincipalClass is missing or empty. iOS loads the "
            "extension by that name; empty means the appex installs and never "
            "runs, which looks exactly like the app doing nothing.")
    if suffix and not principal.endswith(suffix):
        die(f"NSExtensionPrincipalClass is {principal!r}, which does not end with "
            f"{suffix!r}. iOS will fail to instantiate the handler at runtime, "
            "silently, on the tester's phone.")

    if plist.get("CFBundlePackageType") != "XPC!":
        die(f"CFBundlePackageType is {plist.get('CFBundlePackageType')!r}, "
            "expected 'XPC!'. An appex with the wrong package type is rejected "
            "at upload.")

    print(f"plist:           {plist_path}")
    print(f"extension point: {point}")
    print(f"principal class: {principal}")
    print("PASS: the extension declares an extension point Apple accepts")


if __name__ == "__main__":
    main()
