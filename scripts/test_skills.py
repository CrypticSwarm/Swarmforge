#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TestCase:
    path: Path
    skill: str
    prompt: str
    expect_must_match: list[str]
    expect_must_not_match: list[str]
    expect_must_tool: list[str]
    expect_must_not_tool: list[str]
    use_judge: bool


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _iter_test_files(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "opencode/config/skills").glob("*/tests/*.json"))


def _load_test(path: Path) -> TestCase:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    skill = str(raw.get("skill", "")).strip()
    prompt = str(raw.get("prompt", "")).strip()
    expect = raw.get("expect", {})

    if not skill:
        raise ValueError("Missing required field: skill")
    if not prompt:
        raise ValueError("Missing required field: prompt")
    if not isinstance(expect, dict):
        raise ValueError("Field expect must be an object")

    must_match = expect.get("must_match", [])
    must_not_match = expect.get("must_not_match", [])
    must_tool = expect.get("must_tool", [])
    must_not_tool = expect.get("must_not_tool", [])

    if not isinstance(must_match, list) or not all(isinstance(x, str) for x in must_match):
        raise ValueError("expect.must_match must be a list of strings")
    if not isinstance(must_not_match, list) or not all(
        isinstance(x, str) for x in must_not_match
    ):
        raise ValueError("expect.must_not_match must be a list of strings")
    if not isinstance(must_tool, list) or not all(isinstance(x, str) for x in must_tool):
        raise ValueError("expect.must_tool must be a list of strings")
    if not isinstance(must_not_tool, list) or not all(isinstance(x, str) for x in must_not_tool):
        raise ValueError("expect.must_not_tool must be a list of strings")

    judge = raw.get("judge", {})
    use_judge = bool(judge.get("enabled", False)) if isinstance(judge, dict) else False

    return TestCase(
        path=path,
        skill=skill,
        prompt=prompt,
        expect_must_match=must_match,
        expect_must_not_match=must_not_match,
        expect_must_tool=must_tool,
        expect_must_not_tool=must_not_tool,
        use_judge=use_judge,
    )


def _run_opencode(
    *,
    model: str,
    agent: str,
    prompt: str,
    config_dir: str,
    extra_env: dict[str, str],
    timeout_s: int,
    output_format: str,
) -> str:
    env = os.environ.copy()
    env.update(extra_env)
    env["OPENCODE_CONFIG_DIR"] = config_dir

    cmd = [
        "opencode",
        "run",
        "--format",
        output_format,
        "--model",
        model,
        "--agent",
        agent,
        prompt,
    ]

    result = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        timeout=timeout_s,
    )

    if result.returncode != 0:
        raise RuntimeError(f"opencode failed (exit {result.returncode})\n{result.stdout}")

    return result.stdout


def _regex_search(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.MULTILINE | re.DOTALL) is not None


def _walk_json(value: Any):
    if isinstance(value, dict):
        for key, val in value.items():
            yield key, val
            yield from _walk_json(val)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _extract_tool_names(event: Any) -> list[str]:
    tool_names: list[str] = []

    if isinstance(event, dict):
        event_type = event.get("type")
        if isinstance(event_type, str) and "tool" in event_type.lower():
            name = event.get("name")
            if isinstance(name, str):
                tool_names.append(name)

        tool = event.get("tool")
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                tool_names.append(name)
        elif isinstance(tool, str):
            tool_names.append(tool)

        function = event.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str):
                tool_names.append(name)

    for key, val in _walk_json(event):
        if key in {"toolName", "tool_name", "tool"} and isinstance(val, str):
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", val):
                tool_names.append(val)
        if key == "function" and isinstance(val, dict):
            name = val.get("name")
            if isinstance(name, str):
                tool_names.append(name)

    seen: set[str] = set()
    ordered: list[str] = []
    for name in tool_names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _parse_tool_calls_from_ndjson(raw: str) -> list[str]:
    tool_names: list[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        tool_names.extend(_extract_tool_names(event))

    # Some versions may emit a single JSON blob (not NDJSON).
    if not tool_names:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            event = None
        if event is not None:
            tool_names.extend(_extract_tool_names(event))

    seen: set[str] = set()
    ordered: list[str] = []
    for name in tool_names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _judge(
    *,
    eval_model: str,
    agent: str,
    skill_name: str,
    skill_text: str,
    test_prompt: str,
    student_output: str,
    config_dir: str,
    extra_env: dict[str, str],
    timeout_s: int,
) -> dict[str, Any]:
    judge_prompt = "\n".join(
        [
            "You are grading whether an assistant followed a skill.",
            "Return strict JSON only.",
            "",
            f"Skill name: {skill_name}",
            "Skill text:",
            skill_text,
            "",
            "Task prompt:",
            test_prompt,
            "",
            "Assistant output:",
            student_output,
            "",
            "Return JSON with this shape:",
            '{"pass": true|false, "violations": ["..."], "notes": "..."}',
        ]
    )

    raw = _run_opencode(
        model=eval_model,
        agent=agent,
        prompt=judge_prompt,
        config_dir=config_dir,
        extra_env=extra_env,
        timeout_s=timeout_s,
        output_format="default",
    ).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Judge did not return valid JSON: {exc}\n{raw}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run skill behavior tests via opencode")
    parser.add_argument("--model", required=True, help="Student model (provider/model)")
    parser.add_argument(
        "--eval-model",
        default=None,
        help="Optional judge model (provider/model). Defaults to --model.",
    )
    parser.add_argument(
        "--agent",
        default="build",
        help="Agent name to use for runs (default: build)",
    )
    parser.add_argument(
        "--skill",
        default=None,
        help="Only run tests for this skill name",
    )
    parser.add_argument(
        "--enable-judge",
        action="store_true",
        help="Enable LLM-as-judge checks for tests that have judge.enabled=true",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=600,
        help="Per-test timeout seconds (default: 600)",
    )

    args = parser.parse_args()

    repo_root = _repo_root()
    config_dir = str(repo_root / "opencode/config")

    test_files = _iter_test_files(repo_root)
    if not test_files:
        print("No skill tests found.")
        return 0

    tests: list[TestCase] = []
    for test_path in test_files:
        test = _load_test(test_path)
        if args.skill and test.skill != args.skill:
            continue
        tests.append(test)

    if not tests:
        print("No matching skill tests found.")
        return 0

    eval_model = args.eval_model or args.model

    # Keep OpenCode state isolated inside the container or caller-controlled HOME.
    # The Makefile `test` target sets HOME and mounts a dedicated data directory.
    extra_env: dict[str, str] = {}

    failures: list[str] = []
    for test in tests:
        label = f"{test.skill}:{test.path.name}"
        print(f"==> {label}")

        try:
            output = _run_opencode(
                model=args.model,
                agent=args.agent,
                prompt=test.prompt,
                config_dir=config_dir,
                extra_env=extra_env,
                timeout_s=args.timeout_s,
                output_format="default",
            )
        except Exception as exc:
            failures.append(f"{label}: run failed: {exc}")
            continue

        for pattern in test.expect_must_match:
            if not _regex_search(pattern, output):
                failures.append(f"{label}: missing pattern: {pattern}")

        for pattern in test.expect_must_not_match:
            if _regex_search(pattern, output):
                failures.append(f"{label}: forbidden pattern matched: {pattern}")

        if test.expect_must_tool or test.expect_must_not_tool:
            try:
                raw_events = _run_opencode(
                    model=args.model,
                    agent=args.agent,
                    prompt=test.prompt,
                    config_dir=config_dir,
                    extra_env=extra_env,
                    timeout_s=args.timeout_s,
                    output_format="json",
                )
            except Exception as exc:
                failures.append(f"{label}: tool event capture failed: {exc}")
                raw_events = ""

            tool_names = _parse_tool_calls_from_ndjson(raw_events)

            for tool in test.expect_must_tool:
                if tool not in tool_names:
                    failures.append(f"{label}: missing tool call: {tool} (saw: {tool_names})")

            for tool in test.expect_must_not_tool:
                if tool in tool_names:
                    failures.append(f"{label}: forbidden tool call: {tool} (saw: {tool_names})")

        if args.enable_judge and test.use_judge:
            skill_path = repo_root / "opencode/config/skills" / test.skill / "SKILL.md"
            try:
                skill_text = skill_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                failures.append(f"{label}: missing skill file: {skill_path}")
                continue

            try:
                verdict = _judge(
                    eval_model=eval_model,
                    agent=args.agent,
                    skill_name=test.skill,
                    skill_text=skill_text,
                    test_prompt=test.prompt,
                    student_output=output,
                    config_dir=config_dir,
                    extra_env=extra_env,
                    timeout_s=args.timeout_s,
                )
            except Exception as exc:
                failures.append(f"{label}: judge failed: {exc}")
                continue

            if not isinstance(verdict, dict) or verdict.get("pass") is not True:
                violations = verdict.get("violations") if isinstance(verdict, dict) else None
                failures.append(f"{label}: judge failed: {violations}")

    if failures:
        print("\nFAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
