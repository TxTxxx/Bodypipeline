import json
from pathlib import Path
from types import SimpleNamespace

from pipeline.server.providers import provider_ids
from pipeline.server.providers.tianji import TianjiProvider

PROFILE = json.loads(Path("configs/tianji-thor-v1.json").read_text())


def configured_provider():
    provider = TianjiProvider()
    provider.configure(
        SimpleNamespace(
            tianji_provider_bin="",
            letools_bin="",
            command_timeout=30,
        )
    )
    return provider


def test_tianji_provider_is_registered_and_profile_is_approved():
    provider = configured_provider()
    assert "tianji" in provider_ids()
    provider.validate_profile(PROFILE)
    assert provider.profile_blockers(PROFILE) == []
    assert provider.conversion_capabilities(PROFILE) == {}


def test_tianji_stages_client_recording_layout_and_uses_inspect_report(
    tmp_path, monkeypatch
):
    source = tmp_path / "raw" / "episode0"
    (source / "data").mkdir(parents=True)
    (source / "video").mkdir()
    (source / "data/metadata.yaml").write_text("data metadata")
    (source / "video/metadata.yaml").write_text("video metadata")
    (source / "data/bag_0.mcap").write_bytes(b"data")
    (source / "video/bag_0.mcap").write_bytes(b"video")

    provider = configured_provider()
    monkeypatch.setattr(
        provider,
        "_inspect",
        lambda staged, profile: {
            "fps": 50,
            "wrench_fps": 200,
            "frames": 60,
            "reports": [],
        },
    )
    result = provider.prepare(source.parent, tmp_path / "work", PROFILE)

    staged = Path(result["aligned_path"])
    assert staged == tmp_path / "work/episode0"
    assert (staged / "data/bag_0.mcap").read_bytes() == b"data"
    assert (staged / "video/bag_0.mcap").read_bytes() == b"video"
    assert result["frames"] == 60
    assert result["streams"]["state"]["dimensions"] == [16]


def test_tianji_conversion_uses_official_source_format_and_client_task():
    provider = configured_provider()
    args = provider.conversion_args(
        executable="/opt/letools/bin/letools",
        source=Path("/work/aligned"),
        destination=Path("/work/dataset"),
        profile=PROFILE,
        job={
            "instruction": "Pick and place",
            "target_format": "v2.1",
            "convert_workers": 2,
            "video_workers": 4,
            "video_codec_threads": 3,
        },
    )
    assert args[:6] == [
        "/opt/letools/bin/letools",
        "convert",
        "/work/aligned",
        "/work/dataset",
        "--source-format",
        "tianji",
    ]
    assert args[args.index("--task") + 1] == "Pick and place"
    assert args[args.index("--to") + 1] == "v2.1"
    assert args[args.index("--wrench-fps") + 1] == "200"
