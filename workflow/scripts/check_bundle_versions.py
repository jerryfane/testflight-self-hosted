#!/usr/bin/env python3
"""Reads CFBundleVersion out of the BUILT artifact and requires app == appex.

An intermediate check verifies a STEP. This verifies the OUTCOME, and only the
second survives someone rearranging the steps.

The distinction is not theoretical. In the lane this came from, a ruby check
asserted the extension's version in the PROJECT FILE and was correct every time
it ran -- while the project file it read was one the generator itself had just
rewritten with a 9999 default. The compiled appex carried 9999 and the app
carried the real number, which Apple rejects, and no assertion on the project
file could see it. It was found by reading the version out of the compiled
binary. This is that method, kept.

It also catches causes nobody has thought of, because it does not care WHY they
differ.

    python3 .github/scripts/check_bundle_versions.py build/ios/ipa/MyApp.ipa
    python3 .github/scripts/check_bundle_versions.py path/to/MyApp.app

--expect-extension makes an artifact with NO .appex a FAILURE. Pass it whenever
HAS_EXTENSION=1 in .github/app-config.env: for those apps an app with no
extension is not the product, and silently passing would mean the appex could
vanish from the build without anyone noticing. Without the flag a missing appex
is reported and accepted, which is the right answer for an app that has none.
Version agreement is still enforced on any appex that IS present either way.
"""
import plistlib
import sys
import zipfile
from pathlib import Path

APP_EXT = ".appex"


def die(message: str) -> None:
    raise SystemExit(f"::error::{message}")


def load_plist(raw: bytes, where: str):
    """Parse a plist, or fail with what could not be read and where.

    plistlib raises InvalidFileException, which reaches the log as a traceback.
    This runs at the LAST GATE BEFORE THE UPLOAD, so the difference matters: a
    traceback reads as "the checker is broken" and invites a re-run of the whole
    lane, which is how a second profile gets minted. It has to read as "the
    artifact is not what we expected".
    """
    try:
        return plistlib.loads(raw)
    except Exception as error:  # noqa: BLE001 - any parse failure, same meaning
        die(f"could not read the Info.plist at {where}: {error}. "
            "The artifact is malformed; do not re-run the lane until you know why.")


def versions_from_ipa(path: Path):
    """(app_plist, {appex_name: plist}) read straight out of the zip."""
    app_plist = None
    appexes = {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith("Info.plist"):
                continue
            parts = name.split("/")
            # Payload/MyApp.app/Info.plist
            if len(parts) == 3 and parts[0] == "Payload" and parts[1].endswith(".app"):
                app_plist = load_plist(archive.read(name), name)
            # Payload/MyApp.app/PlugIns/Foo.appex/Info.plist
            elif len(parts) == 5 and parts[2] == "PlugIns" and parts[3].endswith(APP_EXT):
                appexes[parts[3]] = load_plist(archive.read(name), name)
    return app_plist, appexes


def versions_from_app(path: Path):
    app_info = path / "Info.plist"
    if not app_info.is_file():
        die(f"{path} has no Info.plist -- is it a built .app?")
    app_plist = load_plist(app_info.read_bytes(), str(app_info))
    appexes = {}
    for appex in sorted((path / "PlugIns").glob(f"*{APP_EXT}")):
        info = appex / "Info.plist"
        if info.is_file():
            appexes[appex.name] = load_plist(info.read_bytes(), str(info))
    return app_plist, appexes


def main() -> None:
    argv = [a for a in sys.argv[1:] if a != "--expect-extension"]
    expect_extension = "--expect-extension" in sys.argv[1:]
    if len(argv) != 1:
        die("usage: check_bundle_versions.py [--expect-extension] <path to .ipa or .app>")
    target = Path(argv[0])
    if not target.exists():
        die(f"{target} does not exist")

    if target.suffix == ".ipa" or zipfile.is_zipfile(target):
        app, appexes = versions_from_ipa(target)
    else:
        app, appexes = versions_from_app(target)

    if app is None:
        die(f"no app Info.plist found in {target}")

    if not appexes and expect_extension:
        # Fail closed: HAS_EXTENSION=1 says an app with no extension is not this
        # product, and silently passing would mean the appex could vanish
        # without anyone noticing.
        die(f"no .appex found inside {target}, but HAS_EXTENSION=1 -- the "
            "extension is not embedded, so the feature it provides is absent "
            "from the build that is about to be shipped to testers.")

    app_version = app.get("CFBundleVersion")
    app_short = app.get("CFBundleShortVersionString")
    print(f"app   {app.get('CFBundleIdentifier')}  "
          f"CFBundleVersion={app_version}  short={app_short}")

    if not app_version:
        die("the app has an EMPTY CFBundleVersion. Apple rejects the upload, and "
            "the usual cause is a build-number setting the project does not "
            "actually read.")

    failures = []
    for name, plist in sorted(appexes.items()):
        version = plist.get("CFBundleVersion")
        short = plist.get("CFBundleShortVersionString")
        print(f"appex {name}  CFBundleVersion={version}  short={short}")
        if not version:
            failures.append(f"{name} has an EMPTY CFBundleVersion -- it installs nowhere")
        elif version != app_version:
            failures.append(
                f"{name} CFBundleVersion {version!r} != app {app_version!r}; "
                "Apple rejects an upload whose extension version differs from its app's"
            )
        if short != app_short:
            failures.append(
                f"{name} CFBundleShortVersionString {short!r} != app {app_short!r}"
            )

    print()
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        die(f"{len(failures)} version mismatch(es) in the built artifact")
    if not appexes:
        print("PASS: app version present; no extensions in this artifact")
    else:
        print(f"PASS: app and {len(appexes)} extension(s) agree on both version keys")


if __name__ == "__main__":
    main()
