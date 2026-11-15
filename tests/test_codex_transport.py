"""Real subprocess tests using a controlled CLI, no account or network."""

import base64
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pipeline.server import codex_transport as c

SOURCE = Path(__file__).parent / "fixtures/fake_codex_cli.py"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    binary = tmp_path / "codex fixture"
    binary.write_text(
        f"#!{sys.executable}\n"
        + SOURCE.read_text().replace("from __future__ import annotations", "")
    )
    binary.chmod(0o755)
    control = tmp_path / "control.json"
    control.write_text('{"mode":"valid"}')
    config = {
        "version": 1,
        "executable": str(binary),
        "model": "test-model",
        "reasoning": "high",
        "max_images": 2,
        "max_image_bytes": 10000,
        "max_prompt_chars": 5000,
        "max_output_bytes": 100000,
        "timeout": 10,
    }
    monkeypatch.setenv("PIPELINE_ANNOTATION_BACKEND", "codex")
    monkeypatch.setenv("PIPELINE_CODEX_CONFIG", json.dumps(config))
    monkeypatch.setenv("PIPELINE_CODEX_RECEIPTS", str(tmp_path / "receipts"))
    monkeypatch.setenv("PIPELINE_ARGUS_API_KEY", "test-placeholder-never-forwarded")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-never-forwarded")
    return tmp_path, config, control


def content(text="Inspect this fixture"):
    return [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,"
                + base64.b64encode(b"test image bytes").decode()
            },
        },
    ]


def call(parts=None):
    return c.call_model(parts or content(), "test-model", "high", 10)


def starts(root):
    path = root / "invocations.jsonl"
    return (
        [
            r
            for line in path.read_text().splitlines()
            if (r := json.loads(line))["phase"] == "start"
        ]
        if path.exists()
        else []
    )


def test_argv_isolated_output_and_success_reuse_under_concurrent_callers(cli):
    root, config, control = cli
    control.write_text('{"delay":0.05}')
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: call(), range(6)))
    assert len(starts(root)) == 1
    assert len({r["id"] for r in results}) == 1
    c.acknowledge(results[0], True)
    assert call()["id"] == results[0]["id"]
    assert results[0]["usage"]["prompt_tokens"] == 100
    assert results[0]["usage"]["cost_source"] == "unknown_codex_cli"
    args = starts(root)[0]["argv"]
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--image" in args and "--output-schema" in args
    assert "features.shell_tool=false" in args and "features.apps=false" in args
    assert "model_providers.bodypipeline_cli.request_max_retries=0" in args
    assert "model_providers.bodypipeline_cli.stream_max_retries=0" in args
    assert "OPENAI_API_KEY" not in starts(root)[0]["env_names"]
    assert "PIPELINE_ARGUS_API_KEY" not in starts(root)[0]["env_names"]
    assert "--ignore-user-config" in args and "--ignore-rules" in args
    assert not Path(args[args.index("--cd") + 1]).exists()
    assert config["executable"].endswith("codex fixture")  # space stays one argv


@pytest.mark.parametrize("mode", ["authentication", "quota", "configuration"])
def test_rejected_auth_and_config_do_not_auto_retry_but_explicit_resume_can(
    cli, monkeypatch, mode
):
    root, _, control = cli
    control.write_text(json.dumps({"mode": mode}))
    with pytest.raises(c.CLIError):
        call()
    control.write_text('{"mode":"valid"}')
    with pytest.raises(c.CLIError):
        call()
    assert len(starts(root)) == 1
    monkeypatch.setenv("PIPELINE_CODEX_RESUME_INDEX", "2")
    result = call()
    c.acknowledge(result, True)
    assert len(starts(root)) == 2 and call()["id"] == result["id"]
    assert len(c.receipts(root / "receipts")) == 2
    assert all(row["cost_usd"] is None for row in c.receipts(root / "receipts"))


@pytest.mark.parametrize("mode", ["invalid", "rate_limit"])
def test_shared_four_send_limit_survives_explicit_resumes(cli, monkeypatch, mode):
    root, _, control = cli
    control.write_text(json.dumps({"mode": mode}))
    monkeypatch.setattr(c.time, "sleep", lambda _: None)
    for resume in range(1, 7):
        monkeypatch.setenv("PIPELINE_CODEX_RESUME_INDEX", str(resume))
        with pytest.raises(c.CLIError):
            call()
    assert len(starts(root)) == 4
    assert len(c.receipts(root / "receipts")) == 4


def test_unknown_outcome_is_not_resent_on_restart_or_resume(cli, monkeypatch):
    root, _, control = cli
    control.write_text('{"mode":"unknown"}')
    with pytest.raises(c.CLIError):
        call()
    monkeypatch.setenv("PIPELINE_CODEX_RESUME_INDEX", "2")
    with pytest.raises(c.CLIError):
        call()
    assert len(starts(root)) == 1


def test_contract_invalid_reply_uses_same_persistent_retry_counter(cli):
    root, _, _ = cli
    for _ in range(4):
        response = call()
        c.acknowledge(response, False)
    with pytest.raises(c.CLIError, match="三次重试上限"):
        call()
    assert len(starts(root)) == 4


@pytest.mark.parametrize(
    "parts",
    [
        content() * 3,
        [{"type": "text", "text": "x" * 5001}],
        [
            {
                "type": "image_url",
                "image_url": {"url": "https://untrusted.invalid/image"},
            }
        ],
    ],
)
def test_input_bounds_fail_before_launch(cli, parts):
    root, _, _ = cli
    with pytest.raises(c.CLIError):
        call(parts)
    assert not starts(root)


def test_tool_event_rejected(cli):
    _, _, control = cli
    control.write_text('{"mode":"tool"}')
    with pytest.raises(c.CLIError, match="禁用工具"):
        call()


def test_system_proxies_are_forwarded_without_gateway_keys(monkeypatch):
    env = c.cli_environment(
        {
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "https_proxy": "http://127.0.0.1:7897",
            "PIPELINE_ARGUS_BASE_URL": "https://broken.invalid",
            "OPENAI_API_KEY": "test-only",
        }
    )
    assert set(env) == {"HTTP_PROXY", "https_proxy"}
