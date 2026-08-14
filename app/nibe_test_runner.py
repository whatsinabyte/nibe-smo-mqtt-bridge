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
import subprocess
import sys
import time
from collections.abc import Callable
from threading import Event

from nibe_mqtt_publisher import MgmtTopic
from nibe_utils import fmt_ts as _fmt_ts

log_commands = logging.getLogger('nibe.commands')


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

        python_exe = sys.executable or 'python3'
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
            proc = subprocess.run(
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
                capture_output=True,
                text=True,
                cwd=run_dir,
                env=env,
                timeout=14400,  # 4 hour hard limit
            )
            elapsed = time.monotonic() - t_start
            exit_code = proc.returncode
            output = (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t_start
            exit_code = -1
            output = (
                'Test suite process killed after 4-hour hard limit.\n'
                'The nightly profile (500 examples, stateful_step_count=50) exceeded\n'
                'the subprocess timeout. Consider reducing max_examples in conftest.py.'
            )
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            exit_code = -2
            output = f'Failed to run test suite: {exc}'
            # Log the full exception here — output only ever reaches the HA
            # notification body, which HA renders as Markdown. A bracketed
            # OSError string like "[Errno 13] Permission denied: '/tests'"
            # looks like a Markdown link reference and can be silently
            # stripped by the renderer, losing the one detail (the path)
            # needed to actually diagnose a launch failure.
            log_commands.exception('Failed to launch test suite subprocess')

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
        except Exception as _e:
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
        if passed:
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
            'Generated html report',
            '=== ',
            '--- ',
        )

        if exit_code == 0:
            meaningful = [
                ln
                for ln in lines
                if ln.strip()
                and not set(ln.strip()).issubset(set('.FEx[] |\t0123456789%u'))
                and not ln.strip().lower().startswith(_NOISE_PREFIXES)
            ]
            if counts_line and counts_line not in meaningful:
                meaningful.append(counts_line)
            summary = '\n'.join(meaningful) if meaningful else counts_line
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
        if passed:
            dismiss_fn(mqtt_client, 'nibe_test_suite_result')
        else:
            _MAX_NOTIF = 2048
            timed_out = exit_code == -1
            launch_error = exit_code == -2
            if timed_out:
                title = 'Nibe Test Suite — ⏱ TIMED OUT'
                body = (
                    'The test process was killed before it finished. '
                    'Reduce `max_examples` or `stateful_step_count` in '
                    '`tests/conftest.py` and rebuild the add-on.'
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

            message = (
                f'{timestamp} — {counts_line} — {elapsed_str}\n\n'
                f'{body}\n\n'
                f'[View full report]({get_base_url_fn()}/local/nibe_test_report.html)'
            )
            if len(message) > _MAX_NOTIF:
                message = (
                    message[: _MAX_NOTIF - 60] + '\n…\n\n'
                    f'[View full report]({get_base_url_fn()}/local/nibe_test_report.html)'
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
