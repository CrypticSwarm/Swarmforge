#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
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


@dataclass
class RunMetrics:
    tool_names: list[str]
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None


class Colors:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)


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


def _extract_metrics(event: Any) -> RunMetrics:
    tool_names = _extract_tool_names(event)

    # Best-effort heuristics. OpenCode JSON event schema may vary.
    cost_candidates: list[float] = []
    in_token_candidates: list[int] = []
    out_token_candidates: list[int] = []

    for key, val in _walk_json(event):
        key_l = str(key).lower()

        if isinstance(val, (int, float)):
            if key_l in {"cost", "totalcost", "costusd", "total_usd", "usd"}:
                cost_candidates.append(float(val))
            if key_l in {"prompttokens", "inputtokens", "input_tokens"}:
                in_token_candidates.append(int(val))
            if key_l in {"completiontokens", "outputtokens", "output_tokens"}:
                out_token_candidates.append(int(val))

    # Prefer the max as a crude "total" when both per-step and total appear.
    cost_usd = max(cost_candidates) if cost_candidates else None
    input_tokens = max(in_token_candidates) if in_token_candidates else None
    output_tokens = max(out_token_candidates) if out_token_candidates else None

    return RunMetrics(
        tool_names=tool_names,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_json_events(raw: str) -> RunMetrics:
    merged_tool_names: list[str] = []
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def merge(metrics: RunMetrics):
        nonlocal cost_usd, input_tokens, output_tokens

        for name in metrics.tool_names:
            if name not in merged_tool_names:
                merged_tool_names.append(name)

        if metrics.cost_usd is not None:
            cost_usd = max(cost_usd or 0.0, metrics.cost_usd)
        if metrics.input_tokens is not None:
            input_tokens = max(input_tokens or 0, metrics.input_tokens)
        if metrics.output_tokens is not None:
            output_tokens = max(output_tokens or 0, metrics.output_tokens)

    # Prefer NDJSON; fall back to parsing a single JSON blob.
    saw_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        merge(_extract_metrics(event))
        saw_any = True

    if not saw_any:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            event = None
        if event is not None:
            merge(_extract_metrics(event))

    return RunMetrics(
        tool_names=merged_tool_names,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


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


def _fmt_cost(cost_usd: float | None) -> str:
    if cost_usd is None:
        return "n/a"
    return f"${cost_usd:.4f}"


def _fmt_tokens(input_tokens: int | None, output_tokens: int | None) -> str:
    if input_tokens is None and output_tokens is None:
        return ""
    return f"{input_tokens or 0} in, {output_tokens or 0} out"


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
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color output: auto, always, never (default: auto)",
    )
    parser.add_argument(
        "--report-cost",
        action="store_true",
        help="Attempt to extract per-test cost from json events",
    )

    args = parser.parse_args()

    color_enabled = False
    if args.color == "always":
        color_enabled = True
    elif args.color == "never":
        color_enabled = False
    else:
        color_enabled = sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"

    c = Colors(enabled=color_enabled)

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
    extra_env: dict[str, str] = {}

    failures: list[str] = []
    per_test_cost: dict[str, float] = {}
    total_cost: float = 0.0

    started = time.time()
    print(c.bold(f"Running {len(tests)} test(s)"))
    print(c.dim(f"student: {args.model} | judge: {eval_model} | agent: {args.agent}"))

    for idx, test in enumerate(tests, start=1):
        label = f"{test.skill}/{test.path.name}"
        rel_path = str(test.path.relative_to(repo_root))

        print(c.dim(f"[{idx}/{len(tests)}] {rel_path}"))

        t0 = time.time()
        output = ""
        run_metrics = RunMetrics(tool_names=[], cost_usd=None, input_tokens=None, output_tokens=None)

        needs_default = bool(test.expect_must_match or test.expect_must_not_match or (args.enable_judge and test.use_judge))
        needs_json = bool(
            args.report_cost
            or test.expect_must_tool
            or test.expect_must_not_tool
        )

        try:
            if needs_default:
                output = _run_opencode(
                    model=args.model,
                    agent=args.agent,
                    prompt=test.prompt,
                    config_dir=config_dir,
                    extra_env=extra_env,
                    timeout_s=args.timeout_s,
                    output_format="default",
                )

            if needs_json:
                raw_events = _run_opencode(
                    model=args.model,
                    agent=args.agent,
                    prompt=test.prompt,
                    config_dir=config_dir,
                    extra_env=extra_env,
                    timeout_s=args.timeout_s,
                    output_format="json",
                )
                run_metrics = _parse_json_events(raw_events)

        except Exception as exc:
            failures.append(f"{label}: run failed: {exc}")
            dt = time.time() - t0
            print(c.red(f"FAIL {label}"), c.dim(f"({dt:.1f}s)"))
            continue

        # Assertions (formatted output)
        for pattern in test.expect_must_match:
            if not _regex_search(pattern, output):
                failures.append(f"{label}: missing pattern: {pattern}")

        for pattern in test.expect_must_not_match:
            if _regex_search(pattern, output):
                failures.append(f"{label}: forbidden pattern matched: {pattern}")

        # Assertions (tool calls)
        if test.expect_must_tool or test.expect_must_not_tool:
            tool_names = run_metrics.tool_names

            for tool in test.expect_must_tool:
                if tool not in tool_names:
                    failures.append(f"{label}: missing tool call: {tool} (saw: {tool_names})")

            for tool in test.expect_must_not_tool:
                if tool in tool_names:
                    failures.append(f"{label}: forbidden tool call: {tool} (saw: {tool_names})")

        # Optional LLM-as-judge check
        if args.enable_judge and test.use_judge:
            skill_path = repo_root / "opencode/config/skills" / test.skill / "SKILL.md"
            try:
                skill_text = skill_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                failures.append(f"{label}: missing skill file: {skill_path}")
            else:
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
                else:
                    if not isinstance(verdict, dict) or verdict.get("pass") is not True:
                        violations = verdict.get("violations") if isinstance(verdict, dict) else None
                        failures.append(f"{label}: judge failed: {violations}")

        dt = time.time() - t0

        # Cost reporting (best-effort)
        if args.report_cost and run_metrics.cost_usd is not None:
            per_test_cost[label] = run_metrics.cost_usd
            total_cost += run_metrics.cost_usd

        # Determine pass/fail for this test by checking if any new failures were added.
        # This is slightly blunt, but avoids coupling to internal assertion structure.
        test_failed = any(f.startswith(f"{label}:") for f in failures)

        cost_str = _fmt_cost(run_metrics.cost_usd if args.report_cost else None)
        tok_str = _fmt_tokens(run_metrics.input_tokens, run_metrics.output_tokens)
        meta_bits = [f"{dt:.1f}s"]
        if args.report_cost:
            meta_bits.append(cost_str)
        if tok_str:
            meta_bits.append(tok_str)

        meta = c.dim(f"({' | '.join(meta_bits)})")

        if test_failed:
            print(c.red(f"FAIL {label}"), meta)
        else:
            print(c.green(f"PASS {label}"), meta)

    elapsed = time.time() - started

    passed = len(tests) - len({f.split(":", 1)[0] for f in failures})
    failed = len(tests) - passed

    print("")
    if failures:
        print(c.red("FAIL"), c.dim(f"({failed} failed, {passed} passed, {elapsed:.1f}s)"))
        for failure in failures:
            print("-", failure)
    else:
        print(c.green("PASS"), c.dim(f"({passed} passed, {elapsed:.1f}s)"))

    if args.report_cost:
        print("")
        print(c.bold("Cost summary (best-effort)"))
        if per_test_cost:
            for label in sorted(per_test_cost.keys()):
                print(f"- {label}: ${per_test_cost[label]:.4f}")
            print(f"- total: ${total_cost:.4f}")
        else:
            print("- n/a (no cost fields found in json events)")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
