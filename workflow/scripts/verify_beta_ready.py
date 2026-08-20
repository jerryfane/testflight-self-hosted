#!/usr/bin/env python3
"""Fails unless the uploaded build actually reaches TestFlight.

`altool` reporting "upload succeeded" does not mean a build reached a tester.
The hardest-won lesson of the lane this came from, relearned twice: a green
upload step and a build nobody can install look identical from CI.

Deliberately TWO-SIDED. It fails only on real failures and on the timeout, and
treats "still processing" as keep-waiting. A guard that turns a slow-but-healthy
ingest red is the mirror failure, and that has already shipped once -- a
measurement guard that reported a fully successful run as inconclusive because
it looked for evidence in the wrong channel.

    APP_BUNDLE_ID=com.example.myapp BUILD_NUMBER=42 \
    python3 .github/scripts/verify_beta_ready.py
"""
import os
import time
from datetime import datetime, timedelta, timezone

import asc

POLL_SECONDS = 30
POLL_ATTEMPTS = 30  # ~15 minutes

READY = {"READY_FOR_BETA_TESTING", "IN_BETA_TESTING"}
DEAD = {"FAILED", "INVALID"}


def app_id(bundle_id: str) -> str:
    status, body = asc.get(f"/v1/apps?filter%5BbundleId%5D={bundle_id}")
    if status != 200:
        asc.die(f"App Store Connect returned {status} looking up {bundle_id}")
    # filter[bundleId] is not an exact match on Apple's side, so an app whose id
    # merely contains this one can come back too. Match exactly rather than
    # taking data[0] and polling the wrong app forever.
    data = [
        item for item in body.get("data", [])
        if item.get("attributes", {}).get("bundleId") == bundle_id
    ] or body.get("data", [])
    if not data:
        asc.die(
            f"No App Store Connect app record for {bundle_id} yet -- create it "
            "first, in App Store Connect. It cannot be created through the API: "
            "POST /v1/apps returns 403, the resource does not allow CREATE. The "
            "build may well have uploaded; this step cannot see it without the "
            "app record."
        )
    return data[0]["id"]


def distribution(build_id: str) -> list:
    """Which beta groups this build is actually in.

    READY_FOR_BETA_TESTING means Apple finished PROCESSING it. It does not mean
    anybody can install it: a build reaches a phone only through a beta group
    that has testers. Nothing in this lane assigns one, so the honest answer to
    "can anyone test this" needs a second question, and this asks it.

    An empty list is a real answer; a failed lookup is not, and must not be
    reported as "no groups".
    """
    status, body = asc.get(f"/v1/builds/{build_id}/betaGroups")
    if status != 200:
        asc.die(
            f"App Store Connect returned {status} listing the beta groups for "
            f"build {build_id}. The build uploaded; this step cannot tell "
            "whether anyone can install it, and will not guess."
        )
    return [
        item.get("attributes", {}).get("name", item.get("id", "?"))
        for item in body.get("data", [])
    ]


def main() -> None:
    bundle_id = os.environ.get("APP_BUNDLE_ID", "").strip()
    if not bundle_id:
        asc.die("APP_BUNDLE_ID is not set; it comes from .github/app-config.env.")
    build_number = os.environ["BUILD_NUMBER"]
    # Two minutes of slack: a build uploaded moments before this run started must
    # not be mistaken for ours, and clocks are not identical.
    floor = datetime.now(timezone.utc) - timedelta(minutes=2)

    identifier = app_id(bundle_id)
    print(f"app {identifier} ({bundle_id}), waiting for build {build_number}")

    for attempt in range(1, POLL_ATTEMPTS + 1):
        status, body = asc.get(
            f"/v1/builds?filter%5Bapp%5D={identifier}"
            f"&filter%5Bversion%5D={build_number}"
            "&include=buildBetaDetail"
        )
        if status != 200:
            asc.die(f"App Store Connect returned {status} listing builds")

        included = {
            item["id"]: item.get("attributes", {})
            for item in body.get("included", [])
            if item.get("type") == "buildBetaDetails"
        }

        for build in body.get("data", []):
            attributes = build.get("attributes", {})
            uploaded = attributes.get("uploadedDate")
            if uploaded:
                when = datetime.fromisoformat(uploaded.replace("Z", "+00:00"))
                if when < floor:
                    # An older build reusing this number. Binding to the upload
                    # time is what stops a stale build turning this green.
                    continue

            processing = attributes.get("processingState")
            if processing in DEAD:
                asc.die(
                    f"build {build_number} came back {processing} -- it will not "
                    "reach testers. Check the ASC build page for the reason."
                )

            detail_id = (
                build.get("relationships", {})
                .get("buildBetaDetail", {})
                .get("data", {})
                or {}
            ).get("id")
            beta_state = included.get(detail_id, {}).get("internalBuildState")

            if beta_state == "MISSING_EXPORT_COMPLIANCE":
                asc.die(
                    "build is waiting on export compliance. Set "
                    "ITSAppUsesNonExemptEncryption in Info.plist, or answer the "
                    "compliance question in App Store Connect."
                )

            if beta_state in READY:
                print(f"build {build_number} is {beta_state}")

                # THE PRODUCT QUESTION, not the API one. An earlier version
                # printed "testers can install it" on the strength of the
                # processing state alone, which is a claim about Apple's pipeline
                # dressed as a claim about a person's phone.
                groups = distribution(build.get("id", ""))
                if not groups:
                    asc.die(
                        f"build {build_number} processed correctly and is in NO "
                        "BETA GROUP, so nobody can install it yet. The upload "
                        "worked and nothing needs rebuilding -- add the build to "
                        "an internal group in App Store Connect > TestFlight, and "
                        "make sure the person who should test it is a tester in "
                        "that group. Being the account holder is not enough."
                    )
                print(
                    f"build {build_number} is distributed to: {', '.join(groups)} "
                    "-- testers in those groups can install it"
                )
                return

            print(
                f"  attempt {attempt}/{POLL_ATTEMPTS}: "
                f"processing={processing} beta={beta_state} -- still working"
            )

        time.sleep(POLL_SECONDS)

    asc.die(
        f"build {build_number} did not become beta-ready within "
        f"{POLL_ATTEMPTS * POLL_SECONDS // 60} minutes. It may still arrive; this "
        "step refuses to report success it cannot see."
    )


if __name__ == "__main__":
    main()
