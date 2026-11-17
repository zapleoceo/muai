# Android Office widget (work in progress)

The owner's 2026-11-14 request is a native home-screen task widget for Samsung
S24 Ultra, with a task list and responses from the phone. This is **not released**.
Branch: feat/office-android-widget. Coordination: office-android-widget-20261114,
fencing token 1 at implementation.

## Scope

Native Java Android app (API 31+, target/compile 35), no third-party runtime
dependencies, tracking, notification service, agent credentials or new account.
Home widget displays up to five scoped Office cards, adapting to its size, with a last-update timestamp and
a link to the full native list. Resize on the launcher as needed.
Tapping a task opens native detail. An open question has an explicit text-answer
dialog. No background mutation, automatic yes/no, cancel or recovery actions.
This first version supports question responses, **not arbitrary emoji reactions
or all dashboard task controls**.

GET /office/hourly/mobile uses the existing owner session and the same report
projection/scope as the kanban. Titles/questions/action text use existing
redaction. No new authentication mechanism or permissions. Native answer sends
the existing /tasks/{room}/{task_id}/answer form with the current question ID and
same-origin header and require_open=true. This optional guard is checked under the
existing question row lock; an already answered question is rejected atomically.
Existing web clients retain the prior behavior. Owner audit remains in place.

Login is the existing Telegram owner login in an app-private WebView; no JS
bridge, file/content access or TLS bypass. Only Vera and Telegram OAuth main
navigation is allowed. Cookies are never copied into source, extras, backups or
logs. No session is minted by the app. This flow still needs a real-device test,
including Telegram popup and expired login.

Cache is app-private with backup disabled; 401 clears the cached list.
The launcher necessarily displays private task titles: add the widget only to
a trusted home screen. Logout clears the local cookie store and task cache; it
does not revoke sessions on other devices. Network failures show a stale warning
and disable native answering until a successful refresh. Ambiguous POST failures
are not retried automatically. User must inspect the refreshed Office before retry.

## Build and install (not yet executed)

Requires an existing JDK 17+, Android SDK platform 35/build-tools and Gradle 8.11.1.
No SDK license was accepted, toolchain installed, or CI run started for this task.
From vera3/android-office, with ANDROID_HOME set:
`gradle :app:assembleDebug :app:lintDebug`.
Artifact: app/build/outputs/apk/debug/app-debug.apk.
A Gradle wrapper and reproducible release signing remain to be added after
toolchain verification. Do not distribute an unverified APK.

After a verified APK exists: install it on the owner's phone, open Vera Office,
complete the existing Telegram owner login, return to the native list, then long
press home screen > Widgets > Vera Office. Do not disable Samsung protections
automatically. Device installation/permission prompts are the owner's actions.

## Required acceptance before done

- Build, Android lint, emulator and physical S24 Ultra launcher checks.
- Login popup, session expiry, logout, airplane mode and stale timestamp.
- Multiple widgets, resize, reboot, battery saver and background update limits.
- Read-only refresh creates no Room messages or mutations.
- User-authorized test question: exact answer, one send, duplicate-tap guard,
  expired/closed question rejection; do not answer real questions as a test.
- No private cache after 401/logout; no credentials in logs/backup.
- Independent review and backend tests before server publication/deploy.

The backend route function is office_mobile; it is read-only and returns schema 1.

## Validation record (2026-11-14)

XML parse (manifest and all resources) and git diff --check passed. Six backend
unit tests added but NOT RUN: no installed Python runtime on Asus-Z. Android build,
lint and all device checks NOT RUN: no SDK/Gradle. Independent source review found
logout, stale-answer, API31, resize and broadcast-lifetime issues; fixes are present,
but post-fix independent review and execution remain pending. No deployment or APK.
Background refresh uses JobScheduler; network requests have a 9-second disconnect
deadline. Rotation preserves the native Activity, but process-death send receipt
handling needs device acceptance; never automatically retry a lost response.

## Second security review follow-up

SnapshotGate serializes the entire launcher publication (snapshot read through
updateAppWidget) with logout/401 invalidation. POST 401 clears the snapshot before
any follow-up refresh; older in-flight GET results cannot restore it. Deep links
carry both room and task ID. All task/reply/login-popup windows use FLAG_SECURE.

Standalone Java gate tests compiled with --release 17 and ran successfully:
stale GET after logout, pending GET after POST-401 invalidation, and a latch-driven
old-render versus logout ordering test. This is not an Android UI/device test.
A real PostgreSQL concurrent-answer test was added to test_room_pg.py and remains
NOT RUN until Python/PostgreSQL execution is available.

## Network permission regression (2026-11-16, Room fence 3)

ACCESS_NETWORK_STATE is required by Android 14+ for RefreshJob's network
constraint; INTERNET alone is insufficient. The manifest now declares both.
Reference: https://developer.android.com/about/versions/14/behavior-changes-14

SDK-free check (existing JDK, no Gradle or SDK installation): compile
`tests/my/veranda/office/ManifestContractTest.java` with `javac --release 17`
and run `my.veranda.office.ManifestContractTest app/src/main/AndroidManifest.xml`
using the compiled classes on the classpath. It passes with the fix and rejects
the original manifest. This checks source XML, not the merged APK manifest.

Source review: SnapshotGate still serializes widget publication with logout/401;
backup remains disabled, cleartext forbidden, and job service unexported and
protected by BIND_JOB_SERVICE. RefreshJob.onStopJob does not interrupt its worker;
a stopped job can finish its bounded network request. Validate stop/restart,
logout during refresh, login popup and stale native dialogs on Android before
acceptance. No Android build/lint/device or Python/PostgreSQL tests ran here.
