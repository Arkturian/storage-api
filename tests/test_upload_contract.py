from upload_contract import is_hls_result_archive


def test_source_video_never_enters_hls_result_path():
    assert not is_hls_result_archive(True, "video/mp4", "recording.mp4")


def test_regular_upload_remains_regular():
    assert not is_hls_result_archive(False, "application/zip", "result.zip")


def test_explicit_zip_is_an_hls_result():
    assert is_hls_result_archive(True, "application/zip", "result.zip")
    assert is_hls_result_archive(True, "application/octet-stream", "result.zip")
