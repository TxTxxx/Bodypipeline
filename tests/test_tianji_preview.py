from scripts.tianji_preview import has_h264_idr, preview_interval


def test_annex_b_idr_detection():
    assert has_h264_idr(b"\x00\x00\x01\x65payload")
    assert has_h264_idr(b"\x00\x00\x01\x67sps\x00\x00\x00\x01\x65idr")
    assert not has_h264_idr(b"\x00\x00\x01\x41delta")
    assert not has_h264_idr(b"not annex-b")


def test_preview_interval_is_bounded():
    assert preview_interval(2) == 0.5
    assert preview_interval(0) == 2.0
    assert preview_interval(120) == 1 / 30
