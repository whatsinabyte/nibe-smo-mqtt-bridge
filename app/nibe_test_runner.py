"""
nibe_test_runner.py
====================
run_test_suite() — runs the full pytest suite as a subprocess and reports
the result via MQTT sensor + HA persistent notification. Extracted from
nibe_ha_integration.py's ManagementCommandHandler._handle_run_tests, whose
debug-only "Run Test Suite from HA" feature this implements — a distinct,
self-contained concern (subprocess orchestration, HTML report
post-processing, output parsing) that happened to be defined as a giant
nested closure inside one MQTT command handler.

Dependency injection (notify_fn, dismiss_fn, get_base_url_fn), not direct
imports of nibe_ha_integration's notify_ha/dismiss_ha/_get_ha_base_url,
avoids a circular import between this module and nibe_ha_integration.py —
the same pattern EntityManager already uses for notify_fn/dismiss_fn.

Responsibilities
-----------------
- Running pytest in a subprocess with the nightly Hypothesis profile.
- Post-processing the HTML report for mobile readability.
- Parsing pytest output into a compact summary and failure list.
- Publishing progress/result to the RUN_TESTS_STATE/RUN_TESTS_ATTRS topics.
- Sending an HA persistent notification on failure.

What this module does NOT do
------------------------------
- No MQTT command routing (that's ManagementCommandHandler's job — it
  decides *when* to call run_test_suite and handles the duplicate-trigger
  guard via the same threading.Event passed in here).
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from threading import Event

from nibe_mqtt_publisher import MgmtTopic
from nibe_utils import fmt_ts as _fmt_ts

log_commands = logging.getLogger('nibe.commands')

# The in-flight pytest subprocess, if any — set right before it's launched
# and cleared once it exits. Only one test run can be in flight at a time
# (guarded by the caller's done_event), so a plain module-level reference is
# sufficient. Lets abort_test_suite() (called from the add-on's shutdown
# sequence) actually kill the process instead of leaving it to run for up to
# its full 4-hour hard timeout while the container is trying to stop —
# subprocess.run()'s own `timeout=` only bounds *our* wait, it has no way to
# be cancelled from outside once it's blocked in communicate().
_current_proc: subprocess.Popen | None = None

# Set by abort_test_suite() right before it kills the process, and read by
# run_test_suite() afterward to tell "killed on purpose because the add-on
# is shutting down" apart from a genuine crash/negative-returncode failure —
# without this, an aborted run would be reported as a real test FAILURE
# (misleading — no tests actually finished running) rather than what it
# actually is. Reset at the start of every run.
_abort_reason: str | None = None


def abort_test_suite(reason: str = 'add-on shutting down') -> None:
    """Kill the in-flight pytest subprocess (and its whole process group),
    if any, so it can't hold up a clean container stop. Safe to call when
    no run is in flight (no-op).

    Kills the *process group*, not just proc.pid, via os.killpg(). pytest
    runs with -n auto (xdist), which forks a worker subprocess per CPU core
    under the main pytest process — proc.kill() alone only kills that top
    -level process, leaving the xdist workers running as orphans. Those
    orphans inherited the same stdout/stderr pipe file descriptors, so they
    can keep the write end of those pipes open even after the top-level
    process is gone — and run_test_suite's post-kill communicate() call
    reads until pipe EOF, so it would hang waiting for those orphaned
    workers to exit on their own instead of returning promptly. The
    subprocess is launched with start_new_session=True (see run_test_suite)
    specifically so the whole group can be targeted here.
    """
    global _abort_reason
    proc = _current_proc
    if proc is None or proc.poll() is not None:
        return
    log_commands.warning('Aborting in-flight test suite run: %s', reason)
    _abort_reason = reason
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        # Process (and thus its group) already exited between the poll()
        # check above and this call — nothing left to kill.
        pass


def _extract_failure_lines(text: str) -> list[str]:
    """Pull the "short test summary info" block — one line per failure:
    "FAILED tests/test_x.py::Class::test - ErrorType: message"
    Falls back to E-prefixed assertion lines from the FAILURES section.
    """
    result: list[str] = []
    in_short = False
    for ln in text.splitlines():
        if 'short test summary info' in ln:
            in_short = True
            continue
        if in_short:
            if ln.startswith('FAILED '):
                result.append(ln[len('FAILED ') :].strip())
            elif ln.startswith('='):
                break
    if result:
        return result
    # Fallback: E-prefixed assertion lines from the FAILURES section
    in_failures = False
    block: list[str] = []
    for ln in text.splitlines():
        if re.search(r'={3,} FAILURES ={3,}', ln):
            in_failures = True
            continue
        if in_failures:
            if re.search(r'={3,}', ln):
                break
            block.append(ln)
    e_lines = [ln2.lstrip() for ln2 in block if ln2.strip().startswith('E ')]
    return e_lines[:5] if e_lines else block[:10]


def run_test_suite(
    mqtt_client,
    notify_fn: Callable,
    dismiss_fn: Callable,
    get_base_url_fn: Callable[[], str],
    done_event: Event,
) -> None:
    """Run the full pytest suite and report progress/result via MQTT + HA.

    Publishes progress and final result to MgmtTopic.RUN_TESTS_STATE /
    RUN_TESTS_ATTRS, then sends a HA persistent notification with a
    copy-pasteable summary.  Runs with HYPOTHESIS_PROFILE=nightly so
    nightly runs exercise maximum Hypothesis coverage.

    Parameters
    ----------
    mqtt_client :
        A connected paho MQTT client — publishes progress/result here.
    notify_fn, dismiss_fn :
        notify_ha / dismiss_ha, injected to avoid a circular import with
        nibe_ha_integration.py (which defines them).
    get_base_url_fn :
        _get_ha_base_url, injected for the same reason — builds the
        clickable report link in the failure notification.
    done_event :
        Cleared when the run finishes (success, failure, or exception) —
        the caller sets it before submitting this function to an executor,
        as a duplicate-trigger guard.
    """
    global _abort_reason, _current_proc

    # Defensive cleanup: under normal operation _handle_run_tests' duplicate-
    # trigger guard (_test_running) already prevents this function from being
    # re-entered while a run is in flight, so _current_proc should always be
    # None here. But if that guard and _current_proc's tracked state ever
    # desync — e.g. an abnormal container restart that doesn't fully tear
    # down a detached process group (start_new_session=True), or any other
    # bug that clears _test_running without actually killing the subprocess
    # — a stale process could still be alive and competing for CPU/resources
    # with the new run. Kill it first rather than let two runs overlap.
    if _current_proc is not None and _current_proc.poll() is None:
        stale_proc = _current_proc
        log_commands.warning(
            'Stale test subprocess (pid %d) found before starting a new '
            'run — killing it first', stale_proc.pid,
        )
        abort_test_suite('superseded by a new test run')
        try:
            stale_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log_commands.error(
                'Stale test subprocess (pid %d) did not exit within 10s '
                'after SIGKILL', stale_proc.pid,
            )

    _abort_reason = None  # clear any stale reason (incl. from the cleanup above)
    try:
        addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        test_path = '/tests'
        if not os.path.isdir(test_path):
            # Fallback for development layout (tests/ alongside app/)
            test_path = os.path.join(addon_dir, 'tests')

        # Determine working directory — pytest.ini lives at addon root
        # and configures testpaths/pythonpath relative to it.
        pytest_ini = os.path.join(addon_dir, 'pytest.ini')
        run_dir = addon_dir if os.path.exists(pytest_ini) else '/tests'

        # sys.executable is normally the running interpreter's absolute path,
        # but on some Alpine/musl container setups it comes back empty (the
        # interpreter can't determine its own path in that environment). A
        # bare 'python3' fallback then depends on the *subprocess's* PATH
        # resolution succeeding, which isn't guaranteed to match what this
        # process's own PATH would resolve — shutil.which() does a real
        # PATH-aware lookup right here instead of hoping subprocess.run's
        # own resolution works out.
        python_exe = sys.executable or shutil.which('python3') or shutil.which('python') or 'python3'
        env = {
            **os.environ,
            'HYPOTHESIS_PROFILE': 'nightly',
            'PYTHONPATH': os.path.join(addon_dir, 'app'),
        }

        # Publish 'running' state immediately
        mqtt_client.publish(MgmtTopic.RUN_TESTS_STATE, 'running', retain=True)
        mqtt_client.publish(
            MgmtTopic.RUN_TESTS_ATTRS,
            json.dumps(
                {
                    'status': 'running',
                    'started': _fmt_ts(),
                }
            ),
            retain=True,
        )

        t_start = time.monotonic()
        report_path = '/homeassistant/www/nibe_test_report.html'
        try:
            # subprocess.Popen (rather than the simpler subprocess.run)
            # so the Popen handle can be stashed in _current_proc —
            # abort_test_suite() needs a live handle to kill if the add-on
            # is asked to shut down mid-run; subprocess.run() blocks inside
            # its own call and doesn't expose one.
            #
            # start_new_session=True makes pytest the leader of its own new
            # process group, separate from this add-on's own group — so a
            # kill can target that whole group (pytest -n auto plus all its
            # xdist worker subprocesses) via os.killpg(), rather than only
            # the top-level pytest process. Without this, killing just the
            # top-level PID leaves orphaned xdist workers running, still
            # holding the stdout/stderr pipes open, and communicate() would
            # hang waiting for pipe EOF that never comes.
            _current_proc = subprocess.Popen(
                [
                    python_exe,
                    '-m',
                    'pytest',
                    test_path,
                    f'--html={report_path}',
                    '--tb=short',  # full traceback on failures
                    '--no-header',  # skip pytest version header
                    '-q',  # compact: N passed in Xs
                    '--timeout=600',  # per-test cap; nightly stateful tests exceed pytest.ini default of 300s
                    '-n',
                    'auto',  # xdist: one worker per CPU core (~4 on ODROID-M1)
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=run_dir,
                env=env,
                start_new_session=True,
            )
            try:
                stdout, stderr = _current_proc.communicate(timeout=14400)  # 4 hour hard limit
                elapsed = time.monotonic() - t_start
                exit_code = _current_proc.returncode
                output = (stdout + stderr).strip()
            except subprocess.TimeoutExpired as timeout_exc:
                try:
                    os.killpg(os.getpgid(_current_proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # The second communicate() call (post-kill) drains whatever
                # output the process/its xdist workers had buffered and waits
                # for the pipes to actually close — its return value is the
                # most complete capture available, so it takes priority over
                # timeout_exc.stdout/stderr (captured earlier, mid-wait, by
                # the raise inside the first communicate() call). Previously
                # this return value was discarded entirely, so a subprocess
                # that died almost immediately (for any reason — this 4-hour
                # timeout is a hard ceiling, not the only way to land here)
                # left no trace of what it actually printed before dying.
                drained_stdout, drained_stderr = _current_proc.communicate()
                partial_stdout = drained_stdout or timeout_exc.stdout or ''
                partial_stderr = drained_stderr or timeout_exc.stderr or ''
                partial_output = (partial_stdout + partial_stderr).strip()
                elapsed = time.monotonic() - t_start
                exit_code = -1
                log_commands.error(
                    'Test suite subprocess did not finish within the 14400s hard '
                    'limit — killed process group (pid %d) after %.1fs elapsed. '
                    'Captured output (%d bytes):\n%s',
                    _current_proc.pid, elapsed, len(partial_output),
                    partial_output[-4000:] if partial_output else '(nothing captured)',
                )
                output = (
                    f'Test suite process killed after {elapsed:.1f}s '
                    '(14400s hard limit).\n'
                    + (
                        f'Captured output before kill:\n{partial_output[-2000:]}'
                        if partial_output
                        else 'No output was captured before the process was killed — '
                             'check the add-on log for the exact kill time.'
                    )
                )
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            exit_code = -2
            output = f'Failed to run test suite (python_exe={python_exe!r}): {exc}'
            # Log the full exception here — output only ever reaches the HA
            # notification body, which HA renders as Markdown. A bracketed
            # OSError string like "[Errno 13] Permission denied: '/tests'"
            # looks like a Markdown link reference and can be silently
            # stripped by the renderer, losing the one detail (the path)
            # needed to actually diagnose a launch failure.
            log_commands.exception('Failed to launch test suite subprocess')
        finally:
            _current_proc = None

        # Post-process the HTML report: inject a mobile viewport meta tag
        # and relax the min-width so the report is readable on phones.
        try:
            with open(report_path, encoding='utf-8') as _f:
                _html = _f.read()
            _html = _html.replace(
                '<meta charset="utf-8"/>',
                '<meta charset="utf-8"/>\n'
                '    <meta name="viewport" '
                'content="width=device-width, initial-scale=1"/>',
            )
            _html = _html.replace('min-width: 800px', 'min-width: 320px')

            with open(report_path, 'w', encoding='utf-8') as _f:
                _f.write(_html)
        except FileNotFoundError:
            log_commands.warning(
                'Test suite HTML report not found at %s — '
                'pytest-html may not be installed in the Docker image. '
                'Check requirements-test.txt and rebuild the add-on.',
                report_path,
            )
        except (OSError, UnicodeDecodeError) as _e:
            # UnicodeDecodeError (not an OSError subclass) can happen when a
            # kill (abort_test_suite's SIGKILL, or the hard-timeout path)
            # truncates the report mid-write, mid-multibyte-UTF-8-sequence.
            # Must not be allowed to propagate to the outer except below —
            # that would replace the carefully-computed aborted/timed_out/
            # failed status with a generic 'error' state, losing exactly the
            # abort/timeout distinction this module exists to preserve.
            log_commands.warning(
                'Could not post-process HTML report at %s: %s',
                report_path,
                _e,
            )

        # Explicit, unconditional existence check — independent of whether
        # the post-processing block above raised. This is the ground truth
        # for "did the report actually land where a user can reach it",
        # verified from inside this same process/container right after
        # pytest-html's own write, rather than inferred from pytest-html's
        # stdout message (which claims success unconditionally) or from an
        # external check (SSH/SMB) that can't tell this container's view of
        # the path apart from another container's.
        report_exists = os.path.isfile(report_path)
        report_size = os.path.getsize(report_path) if report_exists else 0
        log_commands.info(
            'Post-run report check: exists=%s size=%d at %s',
            report_exists,
            report_size,
            report_path,
        )

        passed = exit_code == 0
        aborted = _abort_reason is not None
        if aborted:
            # Killed on purpose (abort_test_suite(), e.g. the add-on is
            # shutting down) — no tests actually finished running, so this
            # is not a real pass/fail/timeout result and must not be
            # reported as one. Checked first: an abort can race with the
            # 4-hour timeout path (both use os.killpg internally) and must
            # take priority over whatever exit_code the kill produced.
            status = 'aborted'
        elif passed:
            status = 'passed'
        elif exit_code == -1:
            status = 'timed_out'
        elif exit_code == -2:
            status = 'error'
        else:
            status = 'failed'

        # ── Extract the pytest counts line ────────────────────────────
        # Always the last non-empty line, e.g. "1 failed, 2251 passed in 1:10:22"
        lines = output.splitlines()
        counts_line = next((ln.strip() for ln in reversed(lines) if ln.strip()), '')

        # ── Build the sensor summary (stored in attributes tab) ────────
        # Pass: strip progress-dot lines, keep warnings + counts line.
        # Fail: short summary block + counts line.
        # xdist/pytest infrastructure lines to suppress from the summary
        _NOISE_PREFIXES = (
            'bringing up nodes',
            'generated html report',
            '=== ',
            '--- ',
        )

        def _is_noise_line(ln: str) -> bool:
            # Strip whatever dash/equals padding pytest-html/xdist wraps a
            # line in (the exact dash count isn't guaranteed, e.g.
            # "---- Generated html report: ... ----") before comparing, so
            # the prefix check doesn't depend on matching that padding
            # verbatim.
            core = ln.strip().strip('-= ').lower()
            return core.startswith(_NOISE_PREFIXES) or ln.strip().lower().startswith(_NOISE_PREFIXES)

        if aborted:
            # The captured output is just whatever partial stdout/stderr
            # happened to be flushed before the kill — not a meaningful
            # pass/fail summary, so don't run it through that parsing at
            # all; state plainly what happened instead.
            summary = f'Test run aborted before completion: {_abort_reason}'
        elif exit_code == 0:
            meaningful = [
                ln
                for ln in lines
                if ln.strip()
                and not set(ln.strip()).issubset(set('.FEsx[] |\t0123456789%u'))
                and not _is_noise_line(ln)
            ]
            if counts_line and counts_line not in meaningful:
                meaningful.append(counts_line)
            summary = '\n'.join(meaningful) if meaningful else counts_line
            if report_exists:
                summary += (
                    f'\n\n📄 [View full report]({get_base_url_fn()}/local/'
                    f'nibe_test_report.html?v={int(time.time())})\n'
                    '(large file — may take a moment to load. Left-click opens the '
                    'HA dashboard instead of the report — right-click and choose '
                    '"Open link in new tab" to view it.)'
                )
        else:
            fail_lines = _extract_failure_lines(output)
            parts = fail_lines + ([counts_line] if counts_line else [])
            summary = '\n'.join(parts) if parts else output[-2000:]

        timestamp = _fmt_ts()

        # Format elapsed time readably
        if elapsed < 60:
            elapsed_str = f'{elapsed:.1f}s'
        else:
            elapsed_str = f'{int(elapsed // 60)}m {elapsed % 60:.0f}s'

        if exit_code == 0:
            log_commands.info(
                'Test suite %s in %s',
                status,
                elapsed_str,
            )
        else:
            log_commands.error(
                'Test suite %s in %s (exit code %d)',
                status,
                elapsed_str,
                exit_code,
            )

        # Publish result sensor
        mqtt_client.publish(MgmtTopic.RUN_TESTS_STATE, status, retain=True)
        mqtt_client.publish(
            MgmtTopic.RUN_TESTS_ATTRS,
            json.dumps(
                {
                    'status': status,
                    'exit_code': exit_code,
                    'elapsed_s': round(elapsed, 1),
                    'elapsed': elapsed_str,
                    'timestamp': timestamp,
                    'summary': summary,
                    'report_path': report_path,
                    'report_exists': report_exists,
                    'report_size': report_size,
                }
            ),
            retain=True,
        )

        # HA persistent notification — only on failure.
        # On success the result is visible on the test suite sensor
        # (sensor attributes tab). On pass: dismiss any previous failure
        # notification. On fail: send a focused notification showing the
        # failing test name and assertion, with a clickable report link.
        # On an intentional abort: no notification at all — nothing failed,
        # the run just never got to finish, and sending a FAILED-looking
        # notification for a deliberate shutdown would be actively
        # misleading. Any previous failure notification is left alone
        # (not dismissed) since this run's outcome is genuinely unknown.
        if aborted:
            pass
        elif passed:
            dismiss_fn(mqtt_client, 'nibe_test_suite_result')
        else:
            _MAX_NOTIF = 2048
            timed_out = exit_code == -1
            launch_error = exit_code == -2
            if timed_out:
                title = 'Nibe Test Suite — ⏱ TIMED OUT'
                if elapsed >= 14000:
                    # Actually ran close to the full 4-hour hard limit — the
                    # "reduce test volume" advice is the right diagnosis.
                    body = (
                        'The test process was killed after running for '
                        f'{elapsed_str}, close to the 4-hour hard limit. '
                        'Reduce `max_examples` or `stateful_step_count` in '
                        '`tests/conftest.py` and rebuild the add-on.'
                    )
                else:
                    # Killed by the same code path, but nowhere near the
                    # 4-hour limit — this is NOT a "tests are too slow"
                    # situation. Something else killed or crashed the
                    # subprocess almost immediately; point at the captured
                    # output (see `output` above) rather than the wrong fix.
                    body = (
                        f'The test process was killed after only {elapsed_str} — '
                        'nowhere near the 4-hour limit, so this is not a '
                        '"tests are too slow" situation. Check the captured '
                        'output above and the add-on log for what actually '
                        'happened to the subprocess.'
                    )
            elif launch_error:
                title = 'Nibe Test Suite — ⚠ LAUNCH ERROR'
                body = output
            else:
                title = 'Nibe Test Suite — ❌ FAILED'
                fail_lines = _extract_failure_lines(output)
                if fail_lines:
                    # Format each as bold test path + assertion on next line
                    formatted: list[str] = []
                    for fl in fail_lines:
                        if ' - ' in fl:
                            test_path, _, err_msg = fl.partition(' - ')
                            formatted.append(f'**{test_path}**\n`{err_msg}`')
                        else:
                            formatted.append(f'**{fl}**')
                    body = '\n\n'.join(formatted)
                else:
                    body = f'```\n{summary}\n```'

            report_link = (
                f'[View full report]({get_base_url_fn()}/local/'
                f'nibe_test_report.html?v={int(time.time())}) '
                '(right-click → "Open link in new tab" — left-click opens the HA '
                'dashboard instead)'
            )
            message = (
                f'{timestamp} — {counts_line} — {elapsed_str}\n\n'
                f'{body}\n\n'
                f'{report_link}'
            )
            if len(message) > _MAX_NOTIF:
                message = (
                    message[: _MAX_NOTIF - len(report_link) - 10] + '\n…\n\n'
                    f'{report_link}'
                )
            notify_fn(
                mqtt_client,
                title=title,
                message=message,
                notification_id='nibe_test_suite_result',
            )
    except Exception:
        # Anything unprotected above (an MQTT publish failing mid-run, a
        # broker disconnect, an unexpected exception in notify_fn, etc.)
        # would otherwise propagate out of this function silently — this
        # runs in a background executor, so nothing would surface the
        # error — leaving RUN_TESTS_STATE retained as "running" forever.
        # Force a terminal state so a stuck run is at least visible.
        log_commands.exception('run_test_suite crashed unexpectedly')
        try:
            mqtt_client.publish(MgmtTopic.RUN_TESTS_STATE, 'error', retain=True)
            mqtt_client.publish(
                MgmtTopic.RUN_TESTS_ATTRS,
                json.dumps({'status': 'error', 'timestamp': _fmt_ts()}),
                retain=True,
            )
        except Exception:
            log_commands.exception('Failed to publish crash state for run_test_suite')
    finally:
        done_event.clear()
