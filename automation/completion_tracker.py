#!/usr/bin/env python3
"""
Completion Status Tracker — Intelligent task completion detection and verification.

Problem: Work Orchestrator status showed tasks as 'in progress' when the Claude Code
session had already completed. This system provides intelligent detection of real
task completion.

Architecture:
  1. Session Monitor   — tracks Claude Code sessions via OpenClaw Gateway API
  2. Output Parser     — extracts completion/failure signals from session output
  3. Verification Engine — tests that artifacts exist and function correctly
  4. Status Updater    — auto-updates WORK_STATUS.md on task completion
  5. Integration Hooks — connects to System Integration Layer

Usage:
    python completion_tracker.py check                 # Check all tracked tasks
    python completion_tracker.py check --task-id <id>  # Check specific task
    python completion_tracker.py status                # Show tracker state
    python completion_tracker.py monitor               # Continuous monitoring
    python completion_tracker.py verify <task-name>    # Run verification for task
    python completion_tracker.py --json                # JSON output for integration
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable

# Ensure automation/ is on sys.path
sys.path.insert(0, str(Path(__file__).parent))

from flywheel_config import (
    WORKSPACE, AUTOMATION_DIR, WORK_STATUS_FILE,
)

# ── Configuration ──────────────────────────────────────────────────

TRACKER_STATE_FILE = AUTOMATION_DIR / "completion_tracker_state.json"
TRACKER_LOG_FILE = AUTOMATION_DIR / "completion_tracker.log"

# Session considered stale if no output for this many seconds
SESSION_STALE_TIMEOUT = 600  # 10 minutes
# Session considered dead if no process found and last seen > this
SESSION_DEAD_TIMEOUT = 120   # 2 minutes after process disappears
# How often to poll in monitor mode
MONITOR_INTERVAL = 60        # 1 minute

# Completion signal patterns (in session output)
COMPLETION_PATTERNS = [
    r"(?i)(?:task|implementation|system|feature|fix)\s+(?:is\s+)?(?:complete|completed|done|finished|deployed)",
    r"(?i)(?:successfully|all)\s+(?:completed|deployed|created|built|implemented)",
    r"(?i)COMPLETE[!.]",
    r"(?i)✅\s*(?:complete|done|finished|all\s+tests\s+pass)",
    r"(?i)all\s+(?:\d+\s+)?tests?\s+pass(?:ed|ing)?",
    r"(?i)committed?\s+(?:to|and\s+pushed)",
    r"(?i)pushed?\s+to\s+(?:remote|origin|github)",
    r"(?i)production[- ]ready",
    r"(?i)verified?\s+(?:and\s+)?(?:working|functional|operational)",
]

FAILURE_PATTERNS = [
    r"(?i)(?:task|implementation|build)\s+failed",
    r"(?i)error[:\s]+(?:fatal|critical|unrecoverable)",
    r"(?i)(?:cannot|could\s+not|unable\s+to)\s+(?:complete|finish|deploy)",
    r"(?i)(?:tests?\s+)?fail(?:ed|ing|ure)",
    r"(?i)traceback\s*\(",
    r"(?i)panic[:\s]",
    r"(?i)abort(?:ed|ing)",
]

PARTIAL_COMPLETION_PATTERNS = [
    r"(?i)partial(?:ly)?\s+(?:complete|done|implemented)",
    r"(?i)(?:some|most)\s+(?:tests?\s+)?(?:pass|work)",
    r"(?i)TODO[:\s]",
    r"(?i)(?:remaining|still\s+need|left\s+to\s+do)",
    r"(?i)work\s+in\s+progress|WIP",
]


# ── Logging Setup ──────────────────────────────────────────────────

def setup_logging(verbose: bool = False):
    AUTOMATION_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
        handlers=[
            logging.FileHandler(TRACKER_LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )

logger = logging.getLogger("completion_tracker")


# ── Data Structures ────────────────────────────────────────────────

class CompletionState(Enum):
    UNKNOWN = "unknown"
    IN_PROGRESS = "in_progress"
    LIKELY_COMPLETE = "likely_complete"
    VERIFIED_COMPLETE = "verified_complete"
    PARTIAL = "partial"
    FAILED = "failed"
    STALE = "stale"        # No output for SESSION_STALE_TIMEOUT
    SESSION_ENDED = "session_ended"  # Process gone, need verification
    TIMEOUT = "timeout"


@dataclass
class SessionInfo:
    """Information about a Claude Code session."""
    session_key: str
    session_name: str
    started_at: Optional[str] = None
    last_output_at: Optional[str] = None
    is_alive: bool = True
    output_snippet: str = ""
    process_pid: Optional[int] = None
    category: str = "unknown"  # cron, subagent, main, discord


@dataclass
class TrackedTask:
    """A task being tracked for completion."""
    task_id: str
    task_name: str
    description: str
    session_key: Optional[str] = None  # OpenClaw session key
    session_name: Optional[str] = None
    started_at: str = ""
    last_checked_at: str = ""
    state: str = CompletionState.UNKNOWN.value
    completion_confidence: float = 0.0  # 0-1
    completion_signals: List[str] = field(default_factory=list)
    failure_signals: List[str] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    verification_results: List[Dict[str, Any]] = field(default_factory=list)
    session_alive: bool = True
    session_last_seen: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TrackerState:
    """Overall tracker state."""
    tracked_tasks: Dict[str, TrackedTask] = field(default_factory=dict)
    last_check: str = ""
    total_checks: int = 0
    total_completions_detected: int = 0
    total_failures_detected: int = 0
    session_cache: Dict[str, Dict] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tracked_tasks": {
                k: v.to_dict() for k, v in self.tracked_tasks.items()
            },
            "last_check": self.last_check,
            "total_checks": self.total_checks,
            "total_completions_detected": self.total_completions_detected,
            "total_failures_detected": self.total_failures_detected,
        }


# ── State Persistence ──────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_str() -> str:
    return _utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def load_tracker_state() -> TrackerState:
    """Load tracker state from disk."""
    if TRACKER_STATE_FILE.exists():
        try:
            with open(TRACKER_STATE_FILE) as f:
                data = json.load(f)
            state = TrackerState(
                last_check=data.get("last_check", ""),
                total_checks=data.get("total_checks", 0),
                total_completions_detected=data.get("total_completions_detected", 0),
                total_failures_detected=data.get("total_failures_detected", 0),
            )
            for task_id, task_data in data.get("tracked_tasks", {}).items():
                state.tracked_tasks[task_id] = TrackedTask(**task_data)
            return state
        except (json.JSONDecodeError, IOError, TypeError) as e:
            logger.warning(f"Failed to load tracker state: {e}")
    return TrackerState()


def save_tracker_state(state: TrackerState):
    """Save tracker state to disk."""
    try:
        TRACKER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRACKER_STATE_FILE, "w") as f:
            json.dump(state.to_dict(), f, indent=2)
    except IOError as e:
        logger.error(f"Failed to save tracker state: {e}")


# ── Session Monitor ────────────────────────────────────────────────

def _load_gateway_config() -> tuple:
    """Load OpenClaw gateway port and auth token."""
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
        port = config.get("gateway", {}).get("port", 18789)
        token = config.get("gateway", {}).get("auth", {}).get("token", "")
        return port, token
    except Exception:
        return 18789, ""


def _gateway_invoke(tool: str, action: str = "json", args: dict = None) -> Optional[Dict]:
    """Invoke an OpenClaw Gateway tool and return the result."""
    port, token = _load_gateway_config()
    url = f"http://127.0.0.1:{port}/tools/invoke"
    payload = json.dumps({
        "tool": tool,
        "action": action,
        "args": args or {},
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            return data.get("result", {})
        else:
            logger.warning(f"Gateway tool {tool} returned error: {data}")
    except Exception as e:
        logger.debug(f"Gateway invoke {tool} failed: {e}")
    return None


def get_active_sessions() -> List[SessionInfo]:
    """Get all active Claude Code sessions from OpenClaw Gateway."""
    result = _gateway_invoke("sessions_list")
    if not result:
        return _get_sessions_fallback()

    sessions = []
    details = result.get("details", {})
    for s in details.get("sessions", []):
        key = s.get("key", "")
        # Determine category
        if ":cron:" in key:
            cat = "cron"
        elif ":subagent:" in key:
            cat = "subagent"
        elif ":discord:" in key:
            cat = "discord"
        elif key.endswith(":main"):
            cat = "main"
        else:
            cat = "other"

        sessions.append(SessionInfo(
            session_key=key,
            session_name=s.get("name", key.split(":")[-1] if ":" in key else key),
            started_at=s.get("started_at"),
            last_output_at=s.get("last_activity"),
            is_alive=True,
            category=cat,
            process_pid=s.get("pid"),
        ))

    return sessions


def _get_sessions_fallback() -> List[SessionInfo]:
    """Fallback session detection via OS process table."""
    sessions = []
    try:
        result = subprocess.run(
            ["pgrep", "-af", "claude"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    parts = line.split(maxsplit=1)
                    pid = int(parts[0]) if parts else 0
                    cmd = parts[1] if len(parts) > 1 else ""
                    sessions.append(SessionInfo(
                        session_key=f"pid:{pid}",
                        session_name=f"claude-{pid}",
                        is_alive=True,
                        process_pid=pid,
                        category="unknown",
                    ))
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return sessions


def is_session_alive(session_key: str) -> bool:
    """Check if a specific session is still alive."""
    active = get_active_sessions()
    return any(s.session_key == session_key for s in active)


def get_session_output(session_key: str) -> Optional[str]:
    """Get recent output from a session via Gateway."""
    result = _gateway_invoke("session_output", args={"session_key": session_key})
    if result:
        return result.get("details", {}).get("output", result.get("output", ""))
    return None


# ── Output Parser ──────────────────────────────────────────────────

class OutputParser:
    """Parse session output for completion/failure signals."""

    @staticmethod
    def parse(output: str) -> Dict[str, Any]:
        """Parse output text and return signal analysis.

        Returns:
            {
                "completion_signals": [matched patterns],
                "failure_signals": [matched patterns],
                "partial_signals": [matched patterns],
                "confidence": float 0-1,
                "suggested_state": CompletionState,
            }
        """
        if not output:
            return {
                "completion_signals": [],
                "failure_signals": [],
                "partial_signals": [],
                "confidence": 0.0,
                "suggested_state": CompletionState.UNKNOWN,
            }

        completion_signals = []
        failure_signals = []
        partial_signals = []

        # Check last 5000 chars (most relevant for completion detection)
        tail = output[-5000:] if len(output) > 5000 else output

        for pattern in COMPLETION_PATTERNS:
            matches = re.findall(pattern, tail)
            completion_signals.extend(matches)

        for pattern in FAILURE_PATTERNS:
            matches = re.findall(pattern, tail)
            failure_signals.extend(matches)

        for pattern in PARTIAL_COMPLETION_PATTERNS:
            matches = re.findall(pattern, tail)
            partial_signals.extend(matches)

        # Calculate confidence
        confidence = 0.0
        suggested_state = CompletionState.UNKNOWN

        if failure_signals and not completion_signals:
            confidence = min(0.3 + 0.15 * len(failure_signals), 0.95)
            suggested_state = CompletionState.FAILED
        elif completion_signals and not failure_signals:
            confidence = min(0.4 + 0.12 * len(completion_signals), 0.95)
            suggested_state = CompletionState.LIKELY_COMPLETE
        elif completion_signals and failure_signals:
            # Mixed signals — could be partial or recovered failure
            if len(completion_signals) > len(failure_signals):
                confidence = 0.5
                suggested_state = CompletionState.LIKELY_COMPLETE
            else:
                confidence = 0.4
                suggested_state = CompletionState.PARTIAL
        elif partial_signals:
            confidence = 0.3
            suggested_state = CompletionState.PARTIAL

        return {
            "completion_signals": completion_signals[:10],  # Cap for readability
            "failure_signals": failure_signals[:10],
            "partial_signals": partial_signals[:10],
            "confidence": round(confidence, 2),
            "suggested_state": suggested_state,
        }


# ── Verification Engine ────────────────────────────────────────────

class VerificationEngine:
    """Verify task completion by checking artifacts and running tests."""

    @staticmethod
    def verify_file_exists(path: str, min_size: int = 10) -> Dict[str, Any]:
        """Verify a file artifact exists and has minimum content."""
        p = Path(path)
        result = {
            "check": "file_exists",
            "path": path,
            "passed": False,
            "details": "",
        }

        if not p.exists():
            result["details"] = f"File not found: {path}"
            return result

        size = p.stat().st_size
        if size < min_size:
            result["details"] = f"File too small: {size} bytes (min {min_size})"
            return result

        result["passed"] = True
        result["details"] = f"File exists: {size} bytes"
        return result

    @staticmethod
    def verify_file_contains(path: str, patterns: List[str]) -> Dict[str, Any]:
        """Verify file contains expected content patterns."""
        p = Path(path)
        result = {
            "check": "file_contains",
            "path": path,
            "passed": False,
            "details": "",
            "matched": [],
            "missing": [],
        }

        if not p.exists():
            result["details"] = f"File not found: {path}"
            return result

        try:
            content = p.read_text()
        except Exception as e:
            result["details"] = f"Cannot read file: {e}"
            return result

        matched = []
        missing = []
        for pattern in patterns:
            if re.search(pattern, content):
                matched.append(pattern)
            else:
                missing.append(pattern)

        result["matched"] = matched
        result["missing"] = missing
        result["passed"] = len(missing) == 0
        result["details"] = (
            f"Matched {len(matched)}/{len(patterns)} patterns"
            + (f", missing: {missing}" if missing else "")
        )
        return result

    @staticmethod
    def verify_command(command: str, expected_returncode: int = 0,
                       expected_output: Optional[str] = None,
                       timeout: int = 30) -> Dict[str, Any]:
        """Verify by running a command and checking output."""
        result = {
            "check": "command",
            "command": command,
            "passed": False,
            "details": "",
            "returncode": None,
            "output": "",
        }

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(WORKSPACE),
            )

            result["returncode"] = proc.returncode
            result["output"] = (proc.stdout + proc.stderr)[:2000]

            if proc.returncode != expected_returncode:
                result["details"] = (
                    f"Return code {proc.returncode} != expected {expected_returncode}"
                )
                return result

            if expected_output and expected_output not in proc.stdout:
                result["details"] = (
                    f"Expected output not found: '{expected_output}'"
                )
                return result

            result["passed"] = True
            result["details"] = "Command executed successfully"
            return result

        except subprocess.TimeoutExpired:
            result["details"] = f"Command timed out after {timeout}s"
            return result
        except Exception as e:
            result["details"] = f"Command execution error: {e}"
            return result

    @staticmethod
    def verify_python_import(module_path: str, function_name: Optional[str] = None) -> Dict[str, Any]:
        """Verify a Python module can be imported and optionally has a function."""
        result = {
            "check": "python_import",
            "module": module_path,
            "passed": False,
            "details": "",
        }

        cmd = f"python3 -c \"import sys; sys.path.insert(0, '{AUTOMATION_DIR}'); "
        if function_name:
            cmd += f"from {module_path} import {function_name}; print('OK')\""
        else:
            cmd += f"import {module_path}; print('OK')\""

        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0 and "OK" in proc.stdout:
                result["passed"] = True
                result["details"] = f"Module {module_path} imports successfully"
            else:
                result["details"] = f"Import failed: {proc.stderr[:500]}"
        except Exception as e:
            result["details"] = f"Import check error: {e}"

        return result

    @staticmethod
    def run_verification_suite(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run a suite of verifications based on artifact definitions.

        Each artifact dict should have:
            - type: "file", "command", "python_import", "file_contains"
            - Plus type-specific fields (path, command, module, patterns, etc.)
        """
        results = []
        engine = VerificationEngine()

        for artifact in artifacts:
            atype = artifact.get("type", "")
            try:
                if atype == "file":
                    r = engine.verify_file_exists(
                        artifact["path"],
                        artifact.get("min_size", 10),
                    )
                elif atype == "file_contains":
                    r = engine.verify_file_contains(
                        artifact["path"],
                        artifact.get("patterns", []),
                    )
                elif atype == "command":
                    r = engine.verify_command(
                        artifact["command"],
                        artifact.get("expected_returncode", 0),
                        artifact.get("expected_output"),
                        artifact.get("timeout", 30),
                    )
                elif atype == "python_import":
                    r = engine.verify_python_import(
                        artifact["module"],
                        artifact.get("function"),
                    )
                else:
                    r = {
                        "check": f"unknown:{atype}",
                        "passed": False,
                        "details": f"Unknown artifact type: {atype}",
                    }
            except Exception as e:
                r = {
                    "check": atype,
                    "passed": False,
                    "details": f"Verification error: {e}",
                }
            results.append(r)

        return results


# ── Task Tracking ──────────────────────────────────────────────────

class CompletionTracker:
    """Main tracker that ties session monitoring, output parsing, and verification."""

    def __init__(self):
        self.state = load_tracker_state()
        self.output_parser = OutputParser()
        self.verification_engine = VerificationEngine()

    def track_task(self, task_id: str, task_name: str, description: str = "",
                   session_key: Optional[str] = None,
                   session_name: Optional[str] = None,
                   artifacts: Optional[List[Dict]] = None) -> TrackedTask:
        """Start tracking a task for completion."""
        now = _utcnow_str()
        task = TrackedTask(
            task_id=task_id,
            task_name=task_name,
            description=description,
            session_key=session_key,
            session_name=session_name,
            started_at=now,
            last_checked_at=now,
            state=CompletionState.IN_PROGRESS.value,
            artifacts=[a for a in (artifacts or [])],
            session_alive=True,
            session_last_seen=now,
        )
        self.state.tracked_tasks[task_id] = task
        save_tracker_state(self.state)
        logger.info(f"Tracking task '{task_name}' (id={task_id}, session={session_key})")
        return task

    def untrack_task(self, task_id: str):
        """Stop tracking a task."""
        if task_id in self.state.tracked_tasks:
            del self.state.tracked_tasks[task_id]
            save_tracker_state(self.state)
            logger.info(f"Untracked task {task_id}")

    def check_task(self, task_id: str) -> TrackedTask:
        """Check completion status of a specific task.

        This is the core intelligence of the tracker:
        1. Check if session is still alive
        2. Parse any available output for signals
        3. If session ended, run artifact verification
        4. Determine completion state with confidence
        """
        task = self.state.tracked_tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not being tracked")

        now = _utcnow_str()
        task.last_checked_at = now

        # Step 1: Session liveness check
        session_alive = False
        if task.session_key:
            session_alive = is_session_alive(task.session_key)
        elif task.session_name:
            # Try to match by name in active sessions
            active = get_active_sessions()
            for s in active:
                if task.session_name in s.session_name or task.session_name in s.session_key:
                    session_alive = True
                    task.session_key = s.session_key
                    break

        task.session_alive = session_alive
        if session_alive:
            task.session_last_seen = now

        # Step 2: Parse output if available
        output = None
        if task.session_key:
            output = get_session_output(task.session_key)
        if output:
            task.output_snippet = output[-500:]
            parse_result = self.output_parser.parse(output)
            task.completion_signals = parse_result["completion_signals"]
            task.failure_signals = parse_result["failure_signals"]
            task.completion_confidence = parse_result["confidence"]

            suggested = parse_result["suggested_state"]
            if suggested != CompletionState.UNKNOWN:
                # Output signals suggest a state
                if suggested == CompletionState.LIKELY_COMPLETE and not session_alive:
                    task.state = CompletionState.LIKELY_COMPLETE.value
                elif suggested == CompletionState.FAILED:
                    task.state = CompletionState.FAILED.value
                elif suggested == CompletionState.PARTIAL:
                    task.state = CompletionState.PARTIAL.value

        # Step 3: Session lifecycle analysis
        if not session_alive:
            if task.state == CompletionState.IN_PROGRESS.value:
                # Session ended while we still thought it was in progress
                task.state = CompletionState.SESSION_ENDED.value
                logger.info(
                    f"Session ended for task '{task.task_name}' — "
                    f"running verification"
                )

        # Step 4: Run artifact verification if session ended or likely complete
        if task.state in (
            CompletionState.SESSION_ENDED.value,
            CompletionState.LIKELY_COMPLETE.value,
        ) and task.artifacts:
            verification_results = self.verification_engine.run_verification_suite(
                task.artifacts
            )
            task.verification_results = verification_results

            passed = sum(1 for r in verification_results if r.get("passed"))
            total = len(verification_results)

            if total > 0:
                pass_rate = passed / total
                if pass_rate == 1.0:
                    task.state = CompletionState.VERIFIED_COMPLETE.value
                    task.completion_confidence = max(task.completion_confidence, 0.95)
                    logger.info(
                        f"Task '{task.task_name}' VERIFIED COMPLETE "
                        f"({passed}/{total} checks passed)"
                    )
                elif pass_rate >= 0.5:
                    task.state = CompletionState.PARTIAL.value
                    task.completion_confidence = max(task.completion_confidence, pass_rate * 0.7)
                    logger.warning(
                        f"Task '{task.task_name}' partially complete "
                        f"({passed}/{total} checks passed)"
                    )
                else:
                    task.state = CompletionState.FAILED.value
                    task.completion_confidence = 0.2
                    logger.warning(
                        f"Task '{task.task_name}' failed verification "
                        f"({passed}/{total} checks passed)"
                    )
        elif task.state == CompletionState.SESSION_ENDED.value and not task.artifacts:
            # Session ended but no artifacts to verify — use output signals
            if task.completion_signals:
                task.state = CompletionState.LIKELY_COMPLETE.value
                task.completion_confidence = max(task.completion_confidence, 0.6)
            elif task.failure_signals:
                task.state = CompletionState.FAILED.value
            else:
                # No signals either way — ambiguous
                task.state = CompletionState.STALE.value
                task.notes = "Session ended with no completion or failure signals"

        # Step 5: Stale detection for still-alive sessions
        if session_alive and task.state == CompletionState.IN_PROGRESS.value:
            # Check if session has been quiet too long
            if task.session_last_seen:
                try:
                    last_seen = datetime.strptime(
                        task.session_last_seen, "%Y-%m-%d %H:%M:%S UTC"
                    ).replace(tzinfo=timezone.utc)
                    elapsed = (_utcnow() - last_seen).total_seconds()
                    if elapsed > SESSION_STALE_TIMEOUT:
                        task.state = CompletionState.STALE.value
                        task.notes = f"No output for {elapsed:.0f}s"
                except (ValueError, TypeError):
                    pass

        save_tracker_state(self.state)
        return task

    def check_all(self) -> Dict[str, TrackedTask]:
        """Check all tracked tasks and return results."""
        now = _utcnow_str()
        self.state.last_check = now
        self.state.total_checks += 1

        results = {}
        for task_id in list(self.state.tracked_tasks.keys()):
            try:
                results[task_id] = self.check_task(task_id)
            except Exception as e:
                logger.error(f"Error checking task {task_id}: {e}")

        # Count state changes
        for task in results.values():
            if task.state == CompletionState.VERIFIED_COMPLETE.value:
                self.state.total_completions_detected += 1
            elif task.state == CompletionState.FAILED.value:
                self.state.total_failures_detected += 1

        save_tracker_state(self.state)
        return results

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of all tracked tasks."""
        state_counts = {}
        tasks_summary = []

        for task_id, task in self.state.tracked_tasks.items():
            state_counts[task.state] = state_counts.get(task.state, 0) + 1
            tasks_summary.append({
                "id": task.task_id,
                "name": task.task_name,
                "state": task.state,
                "confidence": task.completion_confidence,
                "session_alive": task.session_alive,
                "last_checked": task.last_checked_at,
            })

        return {
            "total_tracked": len(self.state.tracked_tasks),
            "state_counts": state_counts,
            "tasks": tasks_summary,
            "total_checks": self.state.total_checks,
            "total_completions": self.state.total_completions_detected,
            "total_failures": self.state.total_failures_detected,
            "last_check": self.state.last_check,
        }


# ── Status Updater ─────────────────────────────────────────────────

class StatusUpdater:
    """Auto-update WORK_STATUS.md when task states change."""

    @staticmethod
    def update_work_status(task: TrackedTask, old_state: str):
        """Append status update to WORK_STATUS.md when state changes."""
        if task.state == old_state:
            return  # No change

        now = _utcnow().strftime("%Y-%m-%d %H:%M UTC")

        state_emoji = {
            CompletionState.VERIFIED_COMPLETE.value: "✅",
            CompletionState.LIKELY_COMPLETE.value: "🏁",
            CompletionState.PARTIAL.value: "⚠️",
            CompletionState.FAILED.value: "❌",
            CompletionState.STALE.value: "⏳",
            CompletionState.SESSION_ENDED.value: "🔍",
            CompletionState.IN_PROGRESS.value: "🔄",
            CompletionState.TIMEOUT.value: "⏰",
        }.get(task.state, "❓")

        confidence_str = f" ({task.completion_confidence:.0%} confidence)" if task.completion_confidence > 0 else ""

        verification_str = ""
        if task.verification_results:
            passed = sum(1 for r in task.verification_results if r.get("passed"))
            total = len(task.verification_results)
            verification_str = f" Verification: {passed}/{total} checks passed."

        entry = (
            f"- {now} — {state_emoji} Completion Tracker: "
            f"'{task.task_name}' → {task.state}{confidence_str}.{verification_str}"
        )

        if task.notes:
            entry += f" Note: {task.notes}"

        # Append to WORK_STATUS.md
        try:
            from checkin_engine import append_checkin_to_status
            append_checkin_to_status(
                f"{state_emoji} Completion Tracker: '{task.task_name}' → "
                f"{task.state}{confidence_str}.{verification_str}"
            )
            logger.info(f"Updated WORK_STATUS.md: {task.task_name} → {task.state}")
        except Exception as e:
            logger.error(f"Failed to update WORK_STATUS.md: {e}")

    @staticmethod
    def update_current_task_section(task: TrackedTask):
        """Update the 'Current Task' section of WORK_STATUS.md."""
        if not WORK_STATUS_FILE.exists():
            return

        try:
            content = WORK_STATUS_FILE.read_text()

            # If task is verified complete, update the status line
            if task.state in (
                CompletionState.VERIFIED_COMPLETE.value,
                CompletionState.LIKELY_COMPLETE.value,
            ):
                # Update Status line to show completion
                content = re.sub(
                    r"(- \*\*Status:\*\* )(?:Building|In progress|Working|WORKING).*",
                    f"\\1COMPLETED — verified by Completion Tracker at {_utcnow().strftime('%H:%M UTC')}",
                    content,
                    count=1,
                )
                WORK_STATUS_FILE.write_text(content)
        except Exception as e:
            logger.error(f"Failed to update Current Task section: {e}")


# ── Integration with System Integration Layer ──────────────────────

def notify_integration_layer(task: TrackedTask, event: str):
    """Send notifications to the System Integration Layer."""
    try:
        from system_integration import add_notification
        add_notification(
            "completion_tracker",
            f"Task '{task.task_name}': {event} (state={task.state}, "
            f"confidence={task.completion_confidence:.0%})",
            priority="info" if "complete" in task.state else "warning",
        )
    except ImportError:
        logger.debug("System integration layer not available")
    except Exception as e:
        logger.error(f"Failed to notify integration layer: {e}")


# ── Monitor Mode ───────────────────────────────────────────────────

def run_monitor(interval: int = MONITOR_INTERVAL):
    """Continuous monitoring loop."""
    tracker = CompletionTracker()
    updater = StatusUpdater()

    logger.info(f"Starting completion monitor (interval={interval}s)")

    import signal as sig
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        logger.info(f"Monitor received signal {signum}, shutting down")

    sig.signal(sig.SIGTERM, handle_signal)
    sig.signal(sig.SIGINT, handle_signal)

    while not shutdown:
        try:
            # Save old states to detect changes
            old_states = {
                tid: t.state
                for tid, t in tracker.state.tracked_tasks.items()
            }

            results = tracker.check_all()

            # Handle state changes
            for task_id, task in results.items():
                old_state = old_states.get(task_id, CompletionState.UNKNOWN.value)
                if task.state != old_state:
                    logger.info(
                        f"State change: '{task.task_name}' "
                        f"{old_state} → {task.state}"
                    )
                    updater.update_work_status(task, old_state)
                    notify_integration_layer(task, f"state changed to {task.state}")

                    # Update current task section for completions
                    if task.state in (
                        CompletionState.VERIFIED_COMPLETE.value,
                        CompletionState.LIKELY_COMPLETE.value,
                    ):
                        updater.update_current_task_section(task)

            # Auto-cleanup: remove verified-complete tasks older than 1 hour
            cutoff = (_utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S UTC")
            for task_id in list(tracker.state.tracked_tasks.keys()):
                task = tracker.state.tracked_tasks[task_id]
                if (task.state == CompletionState.VERIFIED_COMPLETE.value
                        and task.last_checked_at < cutoff):
                    logger.info(f"Auto-cleaning completed task: {task.task_name}")
                    tracker.untrack_task(task_id)

        except Exception as e:
            logger.error(f"Monitor cycle error: {e}", exc_info=True)

        # Interruptible sleep
        end = time.time() + interval
        while time.time() < end and not shutdown:
            time.sleep(min(5, end - time.time()))

    logger.info("Completion monitor stopped")


# ── CLI Interface ──────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Completion Status Tracker — verify task completion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # check
    check_parser = subparsers.add_parser("check", help="Check tracked tasks")
    check_parser.add_argument("--task-id", help="Check specific task ID")

    # status
    subparsers.add_parser("status", help="Show tracker status summary")

    # monitor
    mon_parser = subparsers.add_parser("monitor", help="Continuous monitoring")
    mon_parser.add_argument(
        "--interval", type=int, default=MONITOR_INTERVAL,
        help=f"Check interval in seconds (default: {MONITOR_INTERVAL})",
    )

    # track
    track_parser = subparsers.add_parser("track", help="Start tracking a task")
    track_parser.add_argument("task_id", help="Unique task identifier")
    track_parser.add_argument("task_name", help="Human-readable task name")
    track_parser.add_argument("--session-key", help="OpenClaw session key")
    track_parser.add_argument("--session-name", help="Session name to match")
    track_parser.add_argument("--description", default="", help="Task description")
    track_parser.add_argument(
        "--artifact", action="append", default=[],
        help="Artifact to verify (JSON string, e.g. '{\"type\":\"file\",\"path\":\"/path\"}')",
    )

    # untrack
    untrack_parser = subparsers.add_parser("untrack", help="Stop tracking a task")
    untrack_parser.add_argument("task_id", help="Task ID to untrack")

    # verify
    verify_parser = subparsers.add_parser("verify", help="Run verification suite")
    verify_parser.add_argument("task_id", help="Task ID to verify")

    # sessions
    subparsers.add_parser("sessions", help="List active sessions")

    # Global flags
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    setup_logging(verbose=args.verbose if hasattr(args, "verbose") else False)

    json_output = args.json if hasattr(args, "json") else False

    if not args.command:
        parser.print_help()
        return

    tracker = CompletionTracker()

    if args.command == "check":
        if args.task_id:
            task = tracker.check_task(args.task_id)
            if json_output:
                print(json.dumps(task.to_dict(), indent=2))
            else:
                _print_task(task)
        else:
            results = tracker.check_all()
            if json_output:
                print(json.dumps(
                    {tid: t.to_dict() for tid, t in results.items()},
                    indent=2,
                ))
            else:
                if not results:
                    print("No tasks being tracked.")
                for tid, task in results.items():
                    _print_task(task)
                    print()

    elif args.command == "status":
        summary = tracker.get_summary()
        if json_output:
            print(json.dumps(summary, indent=2))
        else:
            _print_summary(summary)

    elif args.command == "monitor":
        run_monitor(interval=args.interval)

    elif args.command == "track":
        artifacts = []
        for a in args.artifact:
            try:
                artifacts.append(json.loads(a))
            except json.JSONDecodeError:
                logger.error(f"Invalid artifact JSON: {a}")

        task = tracker.track_task(
            task_id=args.task_id,
            task_name=args.task_name,
            description=args.description,
            session_key=args.session_key,
            session_name=args.session_name,
            artifacts=artifacts,
        )
        if json_output:
            print(json.dumps(task.to_dict(), indent=2))
        else:
            print(f"Now tracking: {task.task_name} (id={task.task_id})")

    elif args.command == "untrack":
        tracker.untrack_task(args.task_id)
        print(f"Stopped tracking: {args.task_id}")

    elif args.command == "verify":
        task = tracker.state.tracked_tasks.get(args.task_id)
        if not task:
            print(f"Task {args.task_id} not found")
            return 1

        if not task.artifacts:
            print(f"Task {args.task_id} has no artifacts defined")
            return 1

        results = VerificationEngine.run_verification_suite(task.artifacts)
        if json_output:
            print(json.dumps(results, indent=2))
        else:
            print(f"Verification results for '{task.task_name}':")
            for r in results:
                status = "PASS" if r.get("passed") else "FAIL"
                print(f"  [{status}] {r.get('check', '?')}: {r.get('details', '')}")

    elif args.command == "sessions":
        sessions = get_active_sessions()
        if json_output:
            print(json.dumps([asdict(s) for s in sessions], indent=2))
        else:
            if not sessions:
                print("No active sessions found.")
            else:
                print(f"Active sessions ({len(sessions)}):")
                for s in sessions:
                    alive = "alive" if s.is_alive else "dead"
                    print(f"  [{s.category}] {s.session_name} ({alive}) key={s.session_key}")

    return 0


def _print_task(task: TrackedTask):
    """Print a task in human-readable format."""
    state_emoji = {
        CompletionState.VERIFIED_COMPLETE.value: "✅",
        CompletionState.LIKELY_COMPLETE.value: "🏁",
        CompletionState.PARTIAL.value: "⚠️",
        CompletionState.FAILED.value: "❌",
        CompletionState.STALE.value: "⏳",
        CompletionState.SESSION_ENDED.value: "🔍",
        CompletionState.IN_PROGRESS.value: "🔄",
        CompletionState.TIMEOUT.value: "⏰",
        CompletionState.UNKNOWN.value: "❓",
    }.get(task.state, "❓")

    print(f"{state_emoji} {task.task_name} [{task.task_id}]")
    print(f"   State: {task.state} (confidence: {task.completion_confidence:.0%})")
    print(f"   Session: {'alive' if task.session_alive else 'ended'} "
          f"(key={task.session_key or 'none'})")
    print(f"   Started: {task.started_at}")
    print(f"   Last checked: {task.last_checked_at}")

    if task.completion_signals:
        print(f"   Completion signals: {task.completion_signals[:3]}")
    if task.failure_signals:
        print(f"   Failure signals: {task.failure_signals[:3]}")
    if task.verification_results:
        passed = sum(1 for r in task.verification_results if r.get("passed"))
        total = len(task.verification_results)
        print(f"   Verification: {passed}/{total} passed")
    if task.notes:
        print(f"   Notes: {task.notes}")


def _print_summary(summary: Dict[str, Any]):
    """Print tracker summary in human-readable format."""
    print(f"Completion Status Tracker")
    print(f"  Tracked tasks: {summary['total_tracked']}")
    print(f"  Total checks: {summary['total_checks']}")
    print(f"  Completions detected: {summary['total_completions']}")
    print(f"  Failures detected: {summary['total_failures']}")
    print(f"  Last check: {summary['last_check'] or 'never'}")

    if summary["state_counts"]:
        print(f"\n  State distribution:")
        for state, count in sorted(summary["state_counts"].items()):
            print(f"    {state}: {count}")

    if summary["tasks"]:
        print(f"\n  Tasks:")
        for t in summary["tasks"]:
            emoji = "✅" if "complete" in t["state"] else "🔄" if t["state"] == "in_progress" else "❓"
            alive = "alive" if t["session_alive"] else "ended"
            print(f"    {emoji} {t['name']} — {t['state']} ({t['confidence']:.0%}) [{alive}]")


if __name__ == "__main__":
    sys.exit(main() or 0)
