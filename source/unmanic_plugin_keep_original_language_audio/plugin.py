#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unmanic plugin: keep only original-language audio streams.

The plugin determines a file's original language from Radarr/Sonarr, inspects
audio streams with ffprobe, and builds an FFmpeg stream-copy remux command that
removes only unwanted audio streams.
"""
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

try:
    import pycountry
except Exception:  # pragma: no cover - dependency is declared for Unmanic/runtime
    pycountry = None

try:
    from unmanic.libs.unplugins.settings import PluginSettings
except Exception:  # pragma: no cover - used by unit tests outside Unmanic
    class PluginSettings:
        settings = {}

        def __init__(self, *args, **kwargs):
            self._settings = dict(self.settings)

        def get_setting(self, key=None):
            if key is None:
                return dict(self._settings)
            return self._settings.get(key)

        def get_default_setting(self, key=None):
            if key is None:
                return dict(self.settings)
            return self.settings.get(key)

        def get_form_settings(self):
            return dict(self.form_settings)


MODE_KEEP_ALL = "Keep all original-language audio"
MODE_REMOVE_COMMENTARY = "Keep original-language audio and remove commentary/audio-description tracks"
MODE_SINGLE = "Keep only one original-language audio stream"

PROFILE_BEST = "Best available"
PROFILE_STEREO = "Prefer stereo / 2.0"
PROFILE_51 = "Prefer 5.1"
PROFILE_71 = "Prefer 7.1"

PLUGIN_LOG_PREFIX = "[Keep Original Language Audio]"


class Settings(PluginSettings):
    """Plugin settings shown in the Unmanic WebUI."""

    settings = {
        "Selection mode": MODE_REMOVE_COMMENTARY,
        "Preferred profile": PROFILE_BEST,
        "Strict preferred profile": False,
        "Exclude commentary/audio-description in single-stream mode": True,
        "Radarr URL": "",
        "Radarr API key": "",
        "Sonarr URL": "",
        "Sonarr API key": "",
        "Request timeout seconds": 10,
        "Path mappings": "",
    }
    form_settings = {
        "Selection mode": {
            "input_type": "select",
            "label": "Audio selection mode",
            "select_options": [
                {"value": MODE_KEEP_ALL, "label": "Keep all"},
                {
                    "value": MODE_REMOVE_COMMENTARY,
                    "label": "Keep original and remove commentary",
                },
                {"value": MODE_SINGLE, "label": "Keep one"},
            ],
        },
        "Preferred profile": {
            "input_type": "select",
            "label": "Preferred stream profile for single-stream mode",
            "select_options": [
                {"value": PROFILE_BEST, "label": PROFILE_BEST},
                {"value": PROFILE_STEREO, "label": PROFILE_STEREO},
                {"value": PROFILE_51, "label": PROFILE_51},
                {"value": PROFILE_71, "label": PROFILE_71},
            ],
        },
        "Strict preferred profile": {
            "input_type": "checkbox",
            "label": "Skip unchanged when the preferred channel profile is unavailable",
        },
        "Exclude commentary/audio-description in single-stream mode": {
            "input_type": "checkbox",
            "label": "Exclude commentary/audio-description candidates in single-stream mode",
        },
        "Radarr URL": {
            "input_type": "text",
            "label": "Radarr URL",
        },
        "Radarr API key": {
            "input_type": "text",
            "label": "Radarr API key",
        },
        "Sonarr URL": {
            "input_type": "text",
            "label": "Sonarr URL",
        },
        "Sonarr API key": {
            "input_type": "text",
            "label": "Sonarr API key",
        },
        "Request timeout seconds": {
            "label": "Radarr/Sonarr request timeout in seconds",
            "input_type": "slider",
            "slider_options": {
                "min": 1,
                "max": 120,
                "step": 1,
                "suffix": "s",
            },
        },
        "Path mappings": {
            "input_type": "textarea",
            "label": "Path mappings, one per line: /unmanic/path=/radarr-or-sonarr/path",
        },
    }


FALLBACK_LANGUAGE_ALIASES = {
    "en": "eng",
    "eng": "eng",
    "english": "eng",
    "fr": "fra",
    "fra": "fra",
    "fre": "fra",
    "french": "fra",
    "nl": "nld",
    "nld": "nld",
    "dut": "nld",
    "dutch": "nld",
    "de": "deu",
    "deu": "deu",
    "ger": "deu",
    "german": "deu",
    "es": "spa",
    "spa": "spa",
    "spanish": "spa",
    "it": "ita",
    "ita": "ita",
    "italian": "ita",
    "ja": "jpn",
    "jpn": "jpn",
    "japanese": "jpn",
    "ko": "kor",
    "kor": "kor",
    "korean": "kor",
    "pt": "por",
    "por": "por",
    "portuguese": "por",
    "zh": "zho",
    "chi": "zho",
    "zho": "zho",
    "chinese": "zho",
}

LOSSLESS_CODECS = {
    "truehd": 300,
    "flac": 290,
}
LOSSY_CODECS = {
    "eac3": 240,
    "e-ac-3": 240,
    "dts": 230,
    "ac3": 220,
    "aac": 210,
    "opus": 200,
    "mp3": 190,
}
PROFILE_CHANNELS = {
    PROFILE_STEREO: 2,
    PROFILE_51: 6,
    PROFILE_71: 8,
}


def normalize_language(value):
    """Normalize language codes/names to ISO-639-2 terminology-style codes."""
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("code") or value.get("isoCode") or value.get("id")
    text = str(value).strip().lower().replace("_", "-")
    if not text or text in {"und", "undefined", "unknown", "none", "null"}:
        return None
    text = text.split("-")[0]
    if pycountry:
        for attr in ("alpha_2", "alpha_3", "bibliographic"):
            try:
                language = pycountry.languages.get(**{attr: text})
            except (KeyError, TypeError):
                language = None
            if language:
                return language.alpha_3
        for language in pycountry.languages:
            for attr in ("name", "common_name", "inverted_name"):
                if str(getattr(language, attr, "")).casefold() == text:
                    return language.alpha_3
    return FALLBACK_LANGUAGE_ALIASES.get(text)


def _language_from_record(record):
    for key in ("originalLanguage", "language", "original_language"):
        if key in record:
            normalized = normalize_language(record.get(key))
            if normalized:
                return normalized
    return None


def parse_path_mappings(raw):
    """Parse configured path mappings as local-prefix/service-prefix pairs."""
    mappings = []
    for line in str(raw or "").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        separator = None
        for candidate in ("=>", "=", "|"):
            if candidate in cleaned:
                separator = candidate
                break
        if not separator:
            continue
        local_prefix, service_prefix = [part.strip() for part in cleaned.split(separator, 1)]
        if local_prefix and service_prefix:
            mappings.append((_normalize_path(local_prefix), _normalize_path(service_prefix)))
    return mappings


def _normalize_path(path):
    return os.path.normpath(str(path or "").replace("\\", "/"))


def _casefold_path(path):
    return _normalize_path(path).casefold()


def map_local_path_to_service_path(path, mappings):
    """Return candidate service paths for a local Unmanic path."""
    local_path = _normalize_path(path)
    candidates = [local_path]
    folded = _casefold_path(local_path)
    for local_prefix, service_prefix in mappings:
        prefix = _casefold_path(local_prefix)
        if folded == prefix or folded.startswith(prefix.rstrip("/") + "/"):
            suffix = local_path[len(local_prefix):].lstrip("/\\")
            candidates.append(_normalize_path(os.path.join(service_prefix, suffix)))
    return list(dict.fromkeys(candidates))


def _path_match_score(candidate_path, service_path):
    candidate = _casefold_path(candidate_path)
    service = _casefold_path(service_path)
    if not candidate or not service:
        return 0
    if candidate == service:
        return len(service) + 10000
    if candidate.startswith(service.rstrip("/") + "/"):
        return len(service)
    return 0


class ArrClient:
    """Minimal Radarr/Sonarr HTTP API client."""

    def __init__(self, base_url, api_key, timeout=10):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        try:
            self.timeout = max(1, int(timeout or 10))
        except (TypeError, ValueError):
            self.timeout = 10

    @property
    def configured(self):
        return bool(self.base_url and self.api_key)

    def get_json(self, path, query=None):
        if not self.configured:
            return None
        query = dict(query or {})
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"X-Api-Key": self.api_key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
            return json.loads(body)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None

    def radarr_movies(self):
        data = self.get_json("/api/v3/movie")
        return data if isinstance(data, list) else []

    def sonarr_series(self):
        data = self.get_json("/api/v3/series")
        return data if isinstance(data, list) else []

    def sonarr_episode_files(self, series_id):
        data = self.get_json("/api/v3/episodefile", {"seriesId": series_id})
        return data if isinstance(data, list) else []


def find_radarr_language(file_path, settings, client_class=ArrClient):
    client = client_class(settings.get("Radarr URL"), settings.get("Radarr API key"), settings.get("Request timeout seconds"))
    if not getattr(client, "configured", False):
        return None, 0
    candidates = map_local_path_to_service_path(file_path, parse_path_mappings(settings.get("Path mappings")))
    best_language = None
    best_score = 0
    for movie in client.radarr_movies():
        if not isinstance(movie, dict):
            continue
        language = _language_from_record(movie)
        if not language:
            continue
        service_paths = []
        movie_file = movie.get("movieFile") if isinstance(movie.get("movieFile"), dict) else {}
        for value in (movie_file.get("path"), movie.get("path")):
            if value:
                service_paths.append(value)
        if movie.get("path") and movie_file.get("relativePath"):
            service_paths.append(os.path.join(str(movie.get("path")), str(movie_file.get("relativePath"))))
        for candidate in candidates:
            for service_path in service_paths:
                score = _path_match_score(candidate, service_path)
                if score > best_score:
                    best_score = score
                    best_language = language
    return best_language, best_score


def find_sonarr_language(file_path, settings, client_class=ArrClient):
    client = client_class(settings.get("Sonarr URL"), settings.get("Sonarr API key"), settings.get("Request timeout seconds"))
    if not getattr(client, "configured", False):
        return None, 0
    candidates = map_local_path_to_service_path(file_path, parse_path_mappings(settings.get("Path mappings")))
    best_language = None
    best_score = 0
    for series in client.sonarr_series():
        if not isinstance(series, dict):
            continue
        language = _language_from_record(series)
        if not language:
            continue
        service_paths = [series.get("path")] if series.get("path") else []
        series_id = series.get("id")
        if series_id is not None:
            for episode_file in client.sonarr_episode_files(series_id):
                if not isinstance(episode_file, dict):
                    continue
                if episode_file.get("path"):
                    service_paths.append(episode_file.get("path"))
                elif episode_file.get("relativePath") and series.get("path"):
                    service_paths.append(os.path.join(str(series.get("path")), str(episode_file.get("relativePath"))))
        for candidate in candidates:
            for service_path in service_paths:
                score = _path_match_score(candidate, service_path)
                if score > best_score:
                    best_score = score
                    best_language = language
    return best_language, best_score


def detect_original_language(file_path, settings, client_class=ArrClient):
    radarr_language, radarr_score = find_radarr_language(file_path, settings, client_class)
    sonarr_language, sonarr_score = find_sonarr_language(file_path, settings, client_class)
    if radarr_score <= 0 and sonarr_score <= 0:
        return None
    if radarr_score == sonarr_score and radarr_language and sonarr_language and radarr_language != sonarr_language:
        return None
    return radarr_language if radarr_score >= sonarr_score else sonarr_language


def run_ffprobe_json(filepath):
    """Run ffprobe and parse stream metadata. Returns None on any failure."""
    try:
        output = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-print_format", "json", filepath],
            text=True,
            timeout=30,
        )
        return json.loads(output)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def stream_language(stream):
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    return normalize_language(tags.get("language") or stream.get("language"))


def stream_title(stream):
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    return str(tags.get("title") or stream.get("title") or "")


def is_commentary_or_audio_description(stream):
    """Detect commentary/AD using defensible title-only rules."""
    title = stream_title(stream).strip()
    if not title:
        return False
    lowered = title.casefold()
    if re.search(r"\b(?:director|cast|audio)?\s*commentary\b", lowered):
        return True
    if re.fullmatch(r"comments?", lowered):
        return True
    if re.search(r"\baudio\s+description\b|\bdescriptive\s+audio\b|\bdescriptive\b|\bnarration\b", lowered):
        return True
    return bool(re.search(r"(?:^|[\s\[(])ad(?:$|[\s\])])", title, flags=re.IGNORECASE))


def _channels(stream):
    try:
        return int(stream.get("channels") or 0)
    except (TypeError, ValueError):
        return 0


def _bitrate(stream):
    for key in ("bit_rate", "max_bit_rate"):
        try:
            return int(stream.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _codec_score(stream):
    codec = str(stream.get("codec_name") or "").casefold()
    profile = str(stream.get("profile") or stream.get("codec_long_name") or "").casefold()
    if codec == "dts" and ("ma" in profile or "master audio" in profile or "dts-hd" in profile):
        return 280
    if codec in LOSSLESS_CODECS:
        return LOSSLESS_CODECS[codec]
    return LOSSY_CODECS.get(codec, 0)


def _audio_streams(probe):
    return [stream for stream in (probe or {}).get("streams", []) if stream.get("codec_type") == "audio"]


def _stream_index(stream):
    try:
        return int(stream.get("index"))
    except (TypeError, ValueError):
        return None


def _candidate_sort_key(stream, preferred_channels=None):
    preferred = 1 if preferred_channels and _channels(stream) == preferred_channels else 0
    return (preferred, _codec_score(stream), _bitrate(stream), _channels(stream), -(_stream_index(stream) or 0))


def select_audio_streams(probe, original_language, settings):
    """Return a policy decision containing kept/removed audio streams and no-op reason."""
    normalized_language = normalize_language(original_language)
    audio_streams = _audio_streams(probe)
    if not audio_streams:
        return {"changed": False, "keep": [], "remove": [], "reason": "no audio streams"}
    if not normalized_language:
        return {"changed": False, "keep": audio_streams, "remove": [], "reason": "missing original language"}

    matching = [stream for stream in audio_streams if stream_language(stream) == normalized_language]
    if not matching:
        return {"changed": False, "keep": audio_streams, "remove": [], "reason": "no matching original-language audio"}

    mode = settings.get("Selection mode") or MODE_REMOVE_COMMENTARY
    if mode not in {MODE_KEEP_ALL, MODE_REMOVE_COMMENTARY, MODE_SINGLE}:
        mode = MODE_REMOVE_COMMENTARY

    if mode == MODE_KEEP_ALL:
        keep = matching
    elif mode == MODE_REMOVE_COMMENTARY:
        keep = [stream for stream in matching if not is_commentary_or_audio_description(stream)]
    else:
        exclude_commentary = settings.get("Exclude commentary/audio-description in single-stream mode") is not False
        candidates = [stream for stream in matching if not is_commentary_or_audio_description(stream)] if exclude_commentary else list(matching)
        if not candidates:
            return {"changed": False, "keep": audio_streams, "remove": [], "reason": "only commentary/audio-description matched"}
        profile = settings.get("Preferred profile") or PROFILE_BEST
        preferred_channels = PROFILE_CHANNELS.get(profile)
        if preferred_channels and settings.get("Strict preferred profile"):
            candidates = [stream for stream in candidates if _channels(stream) == preferred_channels]
            if not candidates:
                return {"changed": False, "keep": audio_streams, "remove": [], "reason": "preferred channel profile unavailable"}
        keep = [max(candidates, key=lambda stream: _candidate_sort_key(stream, preferred_channels))]

    if not keep:
        return {"changed": False, "keep": audio_streams, "remove": [], "reason": "selection would remove all audio"}

    keep_indices = {_stream_index(stream) for stream in keep}
    remove = [stream for stream in audio_streams if _stream_index(stream) not in keep_indices]
    changed = bool(remove)
    return {
        "changed": changed,
        "keep": keep if changed else audio_streams,
        "remove": remove,
        "reason": "audio streams selected" if changed else "already matches policy",
    }


def build_ffmpeg_command(file_in, file_out, kept_audio_streams):
    """Build an FFmpeg stream-copy remux command preserving non-audio streams."""
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        file_in,
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-map",
        "0:v?",
    ]
    for stream in kept_audio_streams:
        index = _stream_index(stream)
        if index is not None:
            command.extend(["-map", f"0:{index}"])
    command.extend([
        "-map",
        "0:s?",
        "-map",
        "0:t?",
        "-map",
        "0:d?",
        "-c",
        "copy",
        "-max_muxing_queue_size",
        "2048",
        file_out,
    ])
    return command


def _settings_dict(library_id=None):
    return Settings(library_id=library_id).get_setting()


def _log(data, message):
    worker_log = data.setdefault("worker_log", [])
    worker_log.append(f"{PLUGIN_LOG_PREFIX} {message}")


def _cached_or_detected_plan(file_path, data, settings):
    shared = data.setdefault("shared_info", {})
    if shared.get("original_audio_file") == file_path and "original_audio_plan" in shared:
        return shared.get("original_audio_plan")

    original_language = shared.get("original_audio_language")
    if not original_language:
        original_language = detect_original_language(file_path, settings)
    if not original_language:
        plan = {"changed": False, "keep": [], "remove": [], "reason": "original language unavailable"}
    else:
        probe = shared.get("original_audio_probe") or run_ffprobe_json(file_path)
        if not probe:
            plan = {"changed": False, "keep": [], "remove": [], "reason": "ffprobe unavailable"}
        else:
            plan = select_audio_streams(probe, original_language, settings)
            shared["original_audio_probe"] = probe
    shared["original_audio_file"] = file_path
    shared["original_audio_language"] = original_language
    shared["original_audio_plan"] = plan
    return plan


def on_library_management_file_test(data, **kwargs):
    """
    Queue only files where a safe audio stream-copy remux would remove audio.
    """
    file_path = data.get("path")
    if not file_path:
        return data
    settings = _settings_dict(data.get("library_id"))
    plan = _cached_or_detected_plan(file_path, data, settings)
    if plan.get("changed") and plan.get("keep"):
        data["add_file_to_pending_tasks"] = True
    return data


def on_worker_process(data):
    """
    Configure Unmanic to run an audio-only stream-copy remux.
    """
    file_in = data.get("file_in")
    file_out = data.get("file_out")
    if not file_in or not file_out:
        _log(data, "Missing file_in or file_out; skipping")
        return data
    settings = _settings_dict(data.get("library_id"))
    plan = _cached_or_detected_plan(file_in, data, settings)
    if not plan.get("changed") or not plan.get("keep"):
        _log(data, f"No safe audio change required ({plan.get('reason', 'unknown reason')}); skipping")
        data["exec_command"] = False
        return data
    command = build_ffmpeg_command(file_in, file_out, plan["keep"])
    _log(data, f"Removing {len(plan.get('remove') or [])} audio stream(s) with stream copy")
    data["exec_command"] = command
    return data
