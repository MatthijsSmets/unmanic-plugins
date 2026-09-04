import json
import urllib.error

import plugin


def stream(index, language="eng", codec="ac3", channels=6, title="", bitrate=640000, profile=""):
    tags = {}
    if language is not None:
        tags["language"] = language
    if title:
        tags["title"] = title
    item = {
        "index": index,
        "codec_type": "audio",
        "codec_name": codec,
        "channels": channels,
        "bit_rate": str(bitrate),
        "tags": tags,
    }
    if profile:
        item["profile"] = profile
    return item


def probe(*audio_streams):
    return {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            *audio_streams,
            {"index": 10, "codec_type": "subtitle", "tags": {"language": "eng"}},
            {"index": 11, "codec_type": "attachment"},
        ]
    }


def settings(**overrides):
    base = dict(plugin.Settings.settings)
    base.update(overrides)
    return base


class FakeClient:
    radarr = []
    sonarr = []
    episode_files = {}
    configured = True

    def __init__(self, base_url, api_key, timeout=10):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.configured = bool(base_url and api_key)

    def radarr_movies(self):
        return self.radarr

    def sonarr_series(self):
        return self.sonarr

    def sonarr_episode_files(self, series_id):
        return self.episode_files.get(series_id, [])


def test_language_normalization_codes_names_and_variants():
    assert plugin.normalize_language("en") == "eng"
    assert plugin.normalize_language("eng") == "eng"
    assert plugin.normalize_language("English") == "eng"
    assert plugin.normalize_language("fra") == "fra"
    assert plugin.normalize_language("fre") == "fra"
    assert plugin.normalize_language("fr") == "fra"
    assert plugin.normalize_language("nld") == "nld"
    assert plugin.normalize_language("dut") == "nld"
    assert plugin.normalize_language("Dutch") == "nld"
    assert plugin.normalize_language("ru") == "rus"
    assert plugin.normalize_language("rus") == "rus"
    assert plugin.normalize_language("Russian") == "rus"
    assert plugin.normalize_language("uk") == "ukr"
    assert plugin.normalize_language("ukr") == "ukr"
    assert plugin.normalize_language("Ukrainian") == "ukr"
    assert plugin.normalize_language("und") is None
    assert plugin.normalize_language(None) is None


def test_language_normalization_uses_iso_database_for_broad_coverage():
    assert plugin.normalize_language("sv") == "swe"
    assert plugin.normalize_language("Swedish") == "swe"
    assert plugin.normalize_language("ar") == "ara"
    assert plugin.normalize_language("Arabic") == "ara"


def test_radarr_response_parsing_and_movie_matching():
    FakeClient.radarr = [
        {
            "path": "/movies/Movie",
            "originalLanguage": {"name": "French"},
            "movieFile": {"path": "/movies/Movie/Movie.mkv"},
        }
    ]
    FakeClient.sonarr = []
    found = plugin.detect_original_language(
        "/movies/Movie/Movie.mkv",
        settings(**{"Radarr URL": "http://radarr", "Radarr API key": "key"}),
        FakeClient,
    )
    assert found == "fra"


def test_sonarr_response_parsing_and_series_matching():
    FakeClient.radarr = []
    FakeClient.sonarr = [{"id": 7, "path": "/tv/Show", "originalLanguage": {"name": "English"}}]
    FakeClient.episode_files = {7: [{"path": "/tv/Show/Season 01/S01E01.mkv"}]}
    found = plugin.detect_original_language(
        "/tv/Show/Season 01/S01E01.mkv",
        settings(**{"Sonarr URL": "http://sonarr", "Sonarr API key": "key"}),
        FakeClient,
    )
    assert found == "eng"


def test_configured_path_mapping_matches_arr_path():
    FakeClient.radarr = [
        {
            "path": "/data/movies/Movie",
            "originalLanguage": {"name": "English"},
            "movieFile": {"path": "/data/movies/Movie/Movie.mkv"},
        }
    ]
    FakeClient.sonarr = []
    found = plugin.detect_original_language(
        "/library/movies/Movie/Movie.mkv",
        settings(
            **{
                "Radarr URL": "http://radarr",
                "Radarr API key": "key",
                "Path mappings": "/library/movies=/data/movies",
            }
        ),
        FakeClient,
    )
    assert found == "eng"


def test_missing_api_configuration_is_safe_noop():
    FakeClient.radarr = []
    FakeClient.sonarr = []
    assert plugin.detect_original_language("/movie.mkv", settings(), FakeClient) is None


def test_api_timeout_or_unavailable_behavior(monkeypatch):
    def failing_urlopen(*args, **kwargs):
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(plugin.urllib.request, "urlopen", failing_urlopen)
    client = plugin.ArrClient("http://radarr", "key", 1)
    assert client.radarr_movies() == []


def test_missing_original_language_is_noop():
    decision = plugin.select_audio_streams(probe(stream(1, "eng")), None, settings())
    assert decision["changed"] is False
    assert decision["reason"] == "missing original language"


def test_missing_or_und_stream_language_tags_do_not_match():
    decision = plugin.select_audio_streams(probe(stream(1, None), stream(2, "und")), "eng", settings())
    assert decision["changed"] is False
    assert decision["reason"] == "no matching original-language audio"


def test_no_matching_original_language_stream_is_noop():
    decision = plugin.select_audio_streams(probe(stream(1, "spa")), "eng", settings())
    assert decision["changed"] is False
    assert decision["reason"] == "no matching original-language audio"


def test_commentary_audio_description_and_false_positive_detection():
    assert plugin.is_commentary_or_audio_description(stream(1, title="Director Commentary"))
    assert plugin.is_commentary_or_audio_description(stream(1, title="Audio Description"))
    assert plugin.is_commentary_or_audio_description(stream(1, title="Descriptive Audio"))
    assert plugin.is_commentary_or_audio_description(stream(1, title="AD"))
    assert plugin.is_commentary_or_audio_description(stream(1, title="Narration"))
    assert not plugin.is_commentary_or_audio_description(stream(1, title="Community comments discussion"))
    assert not plugin.is_commentary_or_audio_description(stream(1, title="Adventure Track"))


def test_codec_ranking_prefers_lossless_over_high_bitrate_lossy():
    decision = plugin.select_audio_streams(
        probe(
            stream(1, "eng", "ac3", 6, bitrate=1536000),
            stream(2, "eng", "truehd", 8, bitrate=1000000),
        ),
        "eng",
        settings(**{"Selection mode": plugin.MODE_SINGLE}),
    )
    assert [s["index"] for s in decision["keep"]] == [2]


def test_dts_hd_ma_ranks_as_lossless():
    decision = plugin.select_audio_streams(
        probe(
            stream(1, "eng", "eac3", 8, bitrate=2000000),
            stream(2, "eng", "dts", 6, bitrate=1500000, profile="DTS-HD MA"),
        ),
        "eng",
        settings(**{"Selection mode": plugin.MODE_SINGLE}),
    )
    assert [s["index"] for s in decision["keep"]] == [2]


def test_channel_profile_preference_and_fallback():
    base_settings = settings(**{"Selection mode": plugin.MODE_SINGLE, "Preferred profile": plugin.PROFILE_51})
    decision = plugin.select_audio_streams(
        probe(
            stream(1, "eng", "truehd", 8, title="Atmos"),
            stream(2, "eng", "ac3", 6, title="Compatibility"),
        ),
        "eng",
        base_settings,
    )
    assert [s["index"] for s in decision["keep"]] == [2]

    fallback = plugin.select_audio_streams(
        probe(stream(1, "eng", "truehd", 8)),
        "eng",
        base_settings,
    )
    assert fallback["changed"] is False
    assert [s["index"] for s in fallback["keep"]] == [1]


def test_strict_preferred_profile_skips_when_unavailable():
    decision = plugin.select_audio_streams(
        probe(stream(1, "eng", "truehd", 8), stream(2, "spa", "ac3", 6)),
        "eng",
        settings(
            **{
                "Selection mode": plugin.MODE_SINGLE,
                "Preferred profile": plugin.PROFILE_51,
                "Strict preferred profile": True,
            }
        ),
    )
    assert decision["changed"] is False
    assert decision["reason"] == "preferred channel profile unavailable"


def test_keep_all_mode_keeps_all_matching_language_including_commentary():
    decision = plugin.select_audio_streams(
        probe(stream(1, "eng"), stream(2, "eng", title="Commentary"), stream(3, "spa")),
        "eng",
        settings(**{"Selection mode": plugin.MODE_KEEP_ALL}),
    )
    assert [s["index"] for s in decision["keep"]] == [1, 2]
    assert [s["index"] for s in decision["remove"]] == [3]


def test_remove_commentary_mode_removes_dubs_and_commentary():
    decision = plugin.select_audio_streams(
        probe(stream(1, "fra"), stream(2, "eng"), stream(3, "fra", title="Audio commentary")),
        "fra",
        settings(**{"Selection mode": plugin.MODE_REMOVE_COMMENTARY}),
    )
    assert [s["index"] for s in decision["keep"]] == [1]
    assert [s["index"] for s in decision["remove"]] == [2, 3]


def test_single_stream_mode_removes_all_but_best_candidate():
    decision = plugin.select_audio_streams(
        probe(stream(1, "eng", "aac", 2), stream(2, "eng", "eac3", 6), stream(3, "spa", "ac3", 6)),
        "eng",
        settings(**{"Selection mode": plugin.MODE_SINGLE}),
    )
    assert [s["index"] for s in decision["keep"]] == [2]
    assert [s["index"] for s in decision["remove"]] == [1, 3]


def test_noop_idempotency_when_policy_already_satisfied():
    decision = plugin.select_audio_streams(
        probe(stream(1, "eng", "ac3", 6)),
        "eng",
        settings(**{"Selection mode": plugin.MODE_REMOVE_COMMENTARY}),
    )
    assert decision["changed"] is False
    assert decision["reason"] == "already matches policy"


def test_generated_mapping_preserves_non_audio_and_metadata():
    cmd = plugin.build_ffmpeg_command("/in.mkv", "/out.mkv", [stream(1), stream(3)])
    assert "-c" in cmd
    assert "copy" in cmd
    assert cmd[cmd.index("-map_metadata") + 1] == "0"
    assert cmd[cmd.index("-map_chapters") + 1] == "0"
    assert ["-map", "0:v?"] == cmd[cmd.index("0:v?") - 1: cmd.index("0:v?") + 1]
    assert "0:1" in cmd
    assert "0:3" in cmd
    assert "0:s?" in cmd
    assert "0:t?" in cmd
    assert "0:d?" in cmd
    assert not any(value.startswith("-c:a") or value.startswith("-c:v") for value in cmd)


def test_library_file_test_queues_only_when_safe_change(monkeypatch):
    monkeypatch.setattr(plugin, "_settings_dict", lambda library_id=None: settings())
    monkeypatch.setattr(plugin, "detect_original_language", lambda path, configured: "eng")
    monkeypatch.setattr(plugin, "run_ffprobe_json", lambda path: probe(stream(1, "eng"), stream(2, "spa")))
    data = {"path": "/movie.mkv"}
    result = plugin.on_library_management_file_test(data)
    assert result["add_file_to_pending_tasks"] is True


def test_worker_sets_noop_when_no_change(monkeypatch):
    monkeypatch.setattr(plugin, "_settings_dict", lambda library_id=None: settings())
    monkeypatch.setattr(plugin, "detect_original_language", lambda path, configured: "eng")
    monkeypatch.setattr(plugin, "run_ffprobe_json", lambda path: probe(stream(1, "eng")))
    result = plugin.on_worker_process({"file_in": "/in.mkv", "file_out": "/out.mkv"})
    assert result["exec_command"] is False


def test_worker_generates_command_for_safe_change(monkeypatch):
    monkeypatch.setattr(plugin, "_settings_dict", lambda library_id=None: settings())
    monkeypatch.setattr(plugin, "detect_original_language", lambda path, configured: "eng")
    monkeypatch.setattr(plugin, "run_ffprobe_json", lambda path: probe(stream(1, "eng"), stream(2, "spa")))
    result = plugin.on_worker_process({"file_in": "/in.mkv", "file_out": "/out.mkv"})
    assert result["exec_command"][0] == "ffmpeg"
    assert "0:1" in result["exec_command"]
    assert "0:2" not in result["exec_command"]


def test_english_movie_example_modes():
    media = probe(
        stream(1, "eng", "truehd", 8, title="TrueHD Atmos 7.1"),
        stream(2, "eng", "ac3", 6, title="Compatibility 5.1"),
        stream(3, "eng", "ac3", 2, title="Audio Commentary"),
    )
    keep_all = plugin.select_audio_streams(media, "eng", settings(**{"Selection mode": plugin.MODE_KEEP_ALL}))
    remove_commentary = plugin.select_audio_streams(media, "eng", settings(**{"Selection mode": plugin.MODE_REMOVE_COMMENTARY}))
    single = plugin.select_audio_streams(
        media,
        "eng",
        settings(**{"Selection mode": plugin.MODE_SINGLE, "Preferred profile": plugin.PROFILE_51}),
    )
    assert [s["index"] for s in keep_all["keep"]] == [1, 2, 3]
    assert [s["index"] for s in remove_commentary["keep"]] == [1, 2]
    assert [s["index"] for s in single["keep"]] == [2]


def test_french_movie_example_removes_dubs_and_optional_commentary():
    media = probe(
        stream(1, "fra", "dts", 6, title="DTS-HD MA 5.1", profile="DTS-HD MA"),
        stream(2, "eng", "eac3", 6, title="English dub"),
        stream(3, "deu", "ac3", 6, title="German dub"),
        stream(4, "fra", "aac", 2, title="French commentary"),
    )
    decision = plugin.select_audio_streams(media, "fre", settings(**{"Selection mode": plugin.MODE_REMOVE_COMMENTARY}))
    single = plugin.select_audio_streams(media, "fra", settings(**{"Selection mode": plugin.MODE_SINGLE}))
    assert [s["index"] for s in decision["keep"]] == [1]
    assert [s["index"] for s in decision["remove"]] == [2, 3, 4]
    assert [s["index"] for s in single["keep"]] == [1]


def test_arr_client_json_success(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps([{"path": "/movie"}]).encode()

    monkeypatch.setattr(plugin.urllib.request, "urlopen", lambda request, timeout: Response())
    client = plugin.ArrClient("http://radarr", "key", 5)
    assert client.radarr_movies() == [{"path": "/movie"}]
