#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unmanic plugin: keep only original-language audio streams.

The plugin determines a file's original language from Radarr/Sonarr, inspects
audio streams with ffprobe, and builds an FFmpeg stream-copy remux command that
removes only unwanted audio streams.
"""

import json
import logging
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
MODE_REMOVE_COMMENTARY = (
    "Keep original-language audio and remove commentary/audio-description tracks"
)
MODE_SINGLE = "Keep only one original-language audio stream"

PROFILE_BEST = "Best available"
PROFILE_STEREO = "Prefer stereo / 2.0"
PROFILE_51 = "Prefer 5.1"
PROFILE_71 = "Prefer 7.1"

PLUGIN_LOG_PREFIX = "[Keep Original Language Audio]"

LOGGER = logging.getLogger(
    "Unmanic.Plugin.unmanic_plugin_keep_original_language_audio"
)


class Settings(PluginSettings):
    """Plugin settings shown in the Unmanic WebUI."""

    settings = {
        "Selection mode": MODE_REMOVE_COMMENTARY,
        "Preferred profile": PROFILE_BEST,
        "Strict preferred profile": False,
        "Exclude commentary/audio-description in single-stream mode": True,
        "Radarr URL": "http://localhost:7878",
        "Radarr API key": "",
        "Sonarr URL": "http://localhost:8989",
        "Sonarr API key": "",
        "Request timeout seconds": 10,
        "Path mappings": "",
    }

    form_settings = {
        "Selection mode": {
            "input_type": "select",
            "label": "Audio selection mode",
            "select_options": [
                {
                    "value": MODE_KEEP_ALL,
                    "label": "Keep all",
                },
                {
                    "value": MODE_REMOVE_COMMENTARY,
                    "label": "Keep original and remove commentary",
                },
                {
                    "value": MODE_SINGLE,
                    "label": "Keep one",
                },
            ],
        },
        "Preferred profile": {
            "input_type": "select",
            "label": "Preferred stream profile for single-stream mode",
            "select_options": [
                {
                    "value": PROFILE_BEST,
                    "label": PROFILE_BEST,
                },
                {
                    "value": PROFILE_STEREO,
                    "label": PROFILE_STEREO,
                },
                {
                    "value": PROFILE_51,
                    "label": PROFILE_51,
                },
                {
                    "value": PROFILE_71,
                    "label": PROFILE_71,
                },
            ],
        },
        "Strict preferred profile": {
            "input_type": "checkbox",
            "label": (
                "Skip unchanged when the preferred channel profile is unavailable"
            ),
        },
        "Exclude commentary/audio-description in single-stream mode": {
            "input_type": "checkbox",
            "label": (
                "Exclude commentary/audio-description candidates "
                "in single-stream mode"
            ),
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
            "label": (
                "Path mappings, one per line: "
                "/unmanic/path=/radarr-or-sonarr/path"
            ),
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


def _log(data, message, level=logging.INFO):
    """
    Log to both Unmanic's task log and the normal Python logger.

    The worker_log entry is useful for task-specific logs. The Python logger
    is useful for library scan and API diagnostics.
    """
    formatted = f"{PLUGIN_LOG_PREFIX} {message}"

    if isinstance(data, dict):
        worker_log = data.setdefault("worker_log", [])
        worker_log.append(formatted)

    try:
        LOGGER.log(level, formatted)
    except Exception:
        # Logging must never break plugin processing.
        pass


def _log_stream(stream, label="stream", data=None, level=logging.INFO):
    """Log useful metadata for one audio stream."""
    if not isinstance(stream, dict):
        _log(data, f"{label}: invalid stream object", level)
        return

    _log(
        data,
        (
            f"{label}: "
            f"index={stream.get('index')!r}, "
            f"codec={stream.get('codec_name')!r}, "
            f"channels={stream.get('channels')!r}, "
            f"bitrate={stream.get('bit_rate')!r}, "
            f"language={stream_language(stream)!r}, "
            f"title={stream_title(stream)!r}, "
            f"commentary_or_ad={is_commentary_or_audio_description(stream)}"
        ),
        level,
    )


def normalize_language(value):
    """Normalize language codes/names to ISO-639-2 terminology-style codes."""
    if not value:
        return None

    if isinstance(value, dict):
        value = (
            value.get("name")
            or value.get("code")
            or value.get("isoCode")
            or value.get("id")
        )

    text = str(value).strip().lower().replace("_", "-")

    if not text or text in {
        "und",
        "undefined",
        "unknown",
        "none",
        "null",
    }:
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
    if not isinstance(record, dict):
        return None

    for key in (
        "originalLanguage",
        "language",
        "original_language",
    ):
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
            LOGGER.warning(
                "%s Ignoring invalid path mapping without separator: %s",
                PLUGIN_LOG_PREFIX,
                cleaned,
            )
            continue

        local_prefix, service_prefix = [
            part.strip() for part in cleaned.split(separator, 1)
        ]

        if local_prefix and service_prefix:
            mappings.append(
                (
                    _normalize_path(local_prefix),
                    _normalize_path(service_prefix),
                )
            )

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
            mapped_path = _normalize_path(
                os.path.join(service_prefix, suffix)
            )
            candidates.append(mapped_path)

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
            LOGGER.warning(
                "%s ARR API client is not configured; "
                "URL or API key is missing",
                PLUGIN_LOG_PREFIX,
            )
            return None

        query = dict(query or {})
        url = self.base_url + path

        if query:
            url += "?" + urllib.parse.urlencode(query)

        LOGGER.info(
            "%s ARR API request starting: url=%s timeout=%ss",
            PLUGIN_LOG_PREFIX,
            url,
            self.timeout,
        )

        request = urllib.request.Request(
            url,
            headers={
                "X-Api-Key": self.api_key,
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                body = response.read().decode("utf-8")

                LOGGER.info(
                    "%s ARR API response received: url=%s status=%s bytes=%s",
                    PLUGIN_LOG_PREFIX,
                    url,
                    getattr(response, "status", "unknown"),
                    len(body),
                )

            parsed = json.loads(body)

            LOGGER.info(
                "%s ARR API JSON parsed successfully: url=%s type=%s",
                PLUGIN_LOG_PREFIX,
                url,
                type(parsed).__name__,
            )

            return parsed

        except urllib.error.HTTPError as error:
            LOGGER.warning(
                "%s ARR API HTTP error: url=%s status=%s reason=%s",
                PLUGIN_LOG_PREFIX,
                url,
                error.code,
                error.reason,
            )
            return None

        except urllib.error.URLError as error:
            LOGGER.warning(
                "%s ARR API connection error: url=%s reason=%s",
                PLUGIN_LOG_PREFIX,
                url,
                error.reason,
            )
            return None

        except TimeoutError:
            LOGGER.warning(
                "%s ARR API timeout: url=%s timeout=%ss",
                PLUGIN_LOG_PREFIX,
                url,
                self.timeout,
            )
            return None

        except json.JSONDecodeError as error:
            LOGGER.warning(
                "%s ARR API returned invalid JSON: url=%s error=%s",
                PLUGIN_LOG_PREFIX,
                url,
                error,
            )
            return None

        except OSError as error:
            LOGGER.warning(
                "%s ARR API operating-system error: url=%s error=%s",
                PLUGIN_LOG_PREFIX,
                url,
                error,
            )
            return None

    def radarr_movies(self):
        data = self.get_json("/api/v3/movie")
        movies = data if isinstance(data, list) else []

        LOGGER.info(
            "%s Radarr returned %s movie record(s)",
            PLUGIN_LOG_PREFIX,
            len(movies),
        )

        return movies

    def sonarr_series(self):
        data = self.get_json("/api/v3/series")
        series = data if isinstance(data, list) else []

        LOGGER.info(
            "%s Sonarr returned %s series record(s)",
            PLUGIN_LOG_PREFIX,
            len(series),
        )

        return series

    def sonarr_episode_files(self, series_id):
        data = self.get_json(
            "/api/v3/episodefile",
            {"seriesId": series_id},
        )
        files = data if isinstance(data, list) else []

        LOGGER.info(
            "%s Sonarr returned %s episode file(s) for series_id=%s",
            PLUGIN_LOG_PREFIX,
            len(files),
            series_id,
        )

        return files


def find_radarr_language(file_path, settings, client_class=ArrClient):
    LOGGER.info(
        "%s Starting Radarr lookup: file=%s",
        PLUGIN_LOG_PREFIX,
        file_path,
    )

    client = client_class(
        settings.get("Radarr URL"),
        settings.get("Radarr API key"),
        settings.get("Request timeout seconds"),
    )

    if not getattr(client, "configured", False):
        LOGGER.warning(
            "%s Radarr lookup skipped: client is not configured",
            PLUGIN_LOG_PREFIX,
        )
        return None, 0

    mappings = parse_path_mappings(
        settings.get("Path mappings")
    )
    candidates = map_local_path_to_service_path(
        file_path,
        mappings,
    )

    LOGGER.info(
        "%s Radarr path mappings=%s candidates=%s",
        PLUGIN_LOG_PREFIX,
        mappings,
        candidates,
    )

    best_language = None
    best_score = 0
    movies = client.radarr_movies()

    for movie_number, movie in enumerate(movies, start=1):
        if not isinstance(movie, dict):
            LOGGER.debug(
                "%s Ignoring invalid Radarr record #%s",
                PLUGIN_LOG_PREFIX,
                movie_number,
            )
            continue

        language = _language_from_record(movie)

        if not language:
            LOGGER.debug(
                "%s Radarr record #%s has no recognized original language",
                PLUGIN_LOG_PREFIX,
                movie_number,
            )
            continue

        service_paths = []
        movie_file = (
            movie.get("movieFile")
            if isinstance(movie.get("movieFile"), dict)
            else {}
        )

        for value in (
            movie_file.get("path"),
            movie.get("path"),
        ):
            if value:
                service_paths.append(str(value))

        if movie.get("path") and movie_file.get("relativePath"):
            service_paths.append(
                os.path.join(
                    str(movie.get("path")),
                    str(movie_file.get("relativePath")),
                )
            )

        LOGGER.debug(
            "%s Radarr record #%s title=%r language=%s service_paths=%s",
            PLUGIN_LOG_PREFIX,
            movie_number,
            movie.get("title"),
            language,
            service_paths,
        )

        for candidate in candidates:
            for service_path in service_paths:
                score = _path_match_score(
                    candidate,
                    service_path,
                )

                if score > 0:
                    LOGGER.info(
                        "%s Radarr path match: title=%r candidate=%s "
                        "service_path=%s language=%s score=%s",
                        PLUGIN_LOG_PREFIX,
                        movie.get("title"),
                        candidate,
                        service_path,
                        language,
                        score,
                    )

                if score > best_score:
                    best_score = score
                    best_language = language

    if best_score > 0:
        LOGGER.info(
            "%s Radarr lookup succeeded: language=%s score=%s",
            PLUGIN_LOG_PREFIX,
            best_language,
            best_score,
        )
    else:
        LOGGER.warning(
            "%s Radarr lookup found no matching movie path for file=%s",
            PLUGIN_LOG_PREFIX,
            file_path,
        )

    return best_language, best_score


def find_sonarr_language(file_path, settings, client_class=ArrClient):
    LOGGER.info(
        "%s Starting Sonarr lookup: file=%s",
        PLUGIN_LOG_PREFIX,
        file_path,
    )

    client = client_class(
        settings.get("Sonarr URL"),
        settings.get("Sonarr API key"),
        settings.get("Request timeout seconds"),
    )

    if not getattr(client, "configured", False):
        LOGGER.warning(
            "%s Sonarr lookup skipped: client is not configured",
            PLUGIN_LOG_PREFIX,
        )
        return None, 0

    mappings = parse_path_mappings(
        settings.get("Path mappings")
    )
    candidates = map_local_path_to_service_path(
        file_path,
        mappings,
    )

    LOGGER.info(
        "%s Sonarr path mappings=%s candidates=%s",
        PLUGIN_LOG_PREFIX,
        mappings,
        candidates,
    )

    best_language = None
    best_score = 0
    series_list = client.sonarr_series()

    for series_number, series in enumerate(series_list, start=1):
        if not isinstance(series, dict):
            LOGGER.debug(
                "%s Ignoring invalid Sonarr record #%s",
                PLUGIN_LOG_PREFIX,
                series_number,
            )
            continue

        language = _language_from_record(series)

        if not language:
            LOGGER.debug(
                "%s Sonarr record #%s has no recognized language",
                PLUGIN_LOG_PREFIX,
                series_number,
            )
            continue

        service_paths = []

        if series.get("path"):
            service_paths.append(str(series.get("path")))

        series_id = series.get("id")

        if series_id is not None:
            episode_files = client.sonarr_episode_files(series_id)

            for episode_file in episode_files:
                if not isinstance(episode_file, dict):
                    continue

                if episode_file.get("path"):
                    service_paths.append(
                        str(episode_file.get("path"))
                    )
                elif (
                    episode_file.get("relativePath")
                    and series.get("path")
                ):
                    service_paths.append(
                        os.path.join(
                            str(series.get("path")),
                            str(episode_file.get("relativePath")),
                        )
                    )

        LOGGER.debug(
            "%s Sonarr record #%s title=%r language=%s service_paths=%s",
            PLUGIN_LOG_PREFIX,
            series_number,
            series.get("title"),
            language,
            service_paths,
        )

        for candidate in candidates:
            for service_path in service_paths:
                score = _path_match_score(
                    candidate,
                    service_path,
                )

                if score > 0:
                    LOGGER.info(
                        "%s Sonarr path match: title=%r candidate=%s "
                        "service_path=%s language=%s score=%s",
                        PLUGIN_LOG_PREFIX,
                        series.get("title"),
                        candidate,
                        service_path,
                        language,
                        score,
                    )

                if score > best_score:
                    best_score = score
                    best_language = language

    if best_score > 0:
        LOGGER.info(
            "%s Sonarr lookup succeeded: language=%s score=%s",
            PLUGIN_LOG_PREFIX,
            best_language,
            best_score,
        )
    else:
        LOGGER.warning(
            "%s Sonarr lookup found no matching series path for file=%s",
            PLUGIN_LOG_PREFIX,
            file_path,
        )

    return best_language, best_score


def detect_original_language(file_path, settings, client_class=ArrClient):
    LOGGER.info(
        "%s Detecting original language for file=%s",
        PLUGIN_LOG_PREFIX,
        file_path,
    )

    radarr_language, radarr_score = find_radarr_language(
        file_path,
        settings,
        client_class,
    )

    sonarr_language, sonarr_score = find_sonarr_language(
        file_path,
        settings,
        client_class,
    )

    LOGGER.info(
        "%s Language lookup results: "
        "radarr_language=%s radarr_score=%s "
        "sonarr_language=%s sonarr_score=%s",
        PLUGIN_LOG_PREFIX,
        radarr_language,
        radarr_score,
        sonarr_language,
        sonarr_score,
    )

    if radarr_score <= 0 and sonarr_score <= 0:
        LOGGER.warning(
            "%s Could not determine original language: "
            "neither Radarr nor Sonarr matched the file",
            PLUGIN_LOG_PREFIX,
        )
        return None

    if (
        radarr_score == sonarr_score
        and radarr_language
        and sonarr_language
        and radarr_language != sonarr_language
    ):
        LOGGER.error(
            "%s Conflicting original languages at equal path score: "
            "Radarr=%s Sonarr=%s",
            PLUGIN_LOG_PREFIX,
            radarr_language,
            sonarr_language,
        )
        return None

    if radarr_score >= sonarr_score:
        selected_language = radarr_language
        selected_source = "Radarr"
    else:
        selected_language = sonarr_language
        selected_source = "Sonarr"

    LOGGER.info(
        "%s Selected original language: language=%s source=%s",
        PLUGIN_LOG_PREFIX,
        selected_language,
        selected_source,
    )

    return selected_language


def run_ffprobe_json(filepath):
    """Run ffprobe and parse stream metadata. Returns None on any failure."""
    LOGGER.info(
        "%s Starting ffprobe: file=%s",
        PLUGIN_LOG_PREFIX,
        filepath,
    )

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-print_format",
        "json",
        filepath,
    ]

    try:
        output = subprocess.check_output(
            command,
            text=True,
            timeout=30,
        )

        probe = json.loads(output)
        streams = probe.get("streams", []) if isinstance(probe, dict) else []

        LOGGER.info(
            "%s ffprobe succeeded: file=%s streams=%s",
            PLUGIN_LOG_PREFIX,
            filepath,
            len(streams),
        )

        return probe

    except subprocess.TimeoutExpired:
        LOGGER.warning(
            "%s ffprobe timed out after 30 seconds: file=%s",
            PLUGIN_LOG_PREFIX,
            filepath,
        )
        return None

    except subprocess.CalledProcessError as error:
        LOGGER.warning(
            "%s ffprobe returned non-zero exit code: file=%s "
            "returncode=%s stderr=%s",
            PLUGIN_LOG_PREFIX,
            filepath,
            error.returncode,
            getattr(error, "stderr", None),
        )
        return None

    except subprocess.SubprocessError as error:
        LOGGER.warning(
            "%s ffprobe subprocess error: file=%s error=%s",
            PLUGIN_LOG_PREFIX,
            filepath,
            error,
        )
        return None

    except OSError as error:
        LOGGER.warning(
            "%s ffprobe operating-system error: file=%s error=%s",
            PLUGIN_LOG_PREFIX,
            filepath,
            error,
        )
        return None

    except json.JSONDecodeError as error:
        LOGGER.warning(
            "%s ffprobe returned invalid JSON: file=%s error=%s",
            PLUGIN_LOG_PREFIX,
            filepath,
            error,
        )
        return None


def stream_language(stream):
    tags = (
        stream.get("tags")
        if isinstance(stream.get("tags"), dict)
        else {}
    )

    return normalize_language(
        tags.get("language") or stream.get("language")
    )


def stream_title(stream):
    tags = (
        stream.get("tags")
        if isinstance(stream.get("tags"), dict)
        else {}
    )

    return str(
        tags.get("title")
        or stream.get("title")
        or ""
    )


def is_commentary_or_audio_description(stream):
    """Detect commentary/AD using defensible title-only rules."""
    title = stream_title(stream).strip()

    if not title:
        return False

    lowered = title.casefold()

    if re.search(
        r"\b(?:director|cast|audio)?\s*commentary\b",
        lowered,
    ):
        return True

    if re.fullmatch(r"comments?", lowered):
        return True

    if re.search(
        r"\baudio\s+description\b"
        r"|\bdescriptive\s+audio\b"
        r"|\bdescriptive\b"
        r"|\bnarration\b",
        lowered,
    ):
        return True

    return bool(
        re.search(
            r"(?:^|[\s\[(])ad(?:$|[\s\])])",
            title,
            flags=re.IGNORECASE,
        )
    )


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
    codec = str(
        stream.get("codec_name") or ""
    ).casefold()

    profile = str(
        stream.get("profile")
        or stream.get("codec_long_name")
        or ""
    ).casefold()

    if codec == "dts" and (
        "ma" in profile
        or "master audio" in profile
        or "dts-hd" in profile
    ):
        return 280

    if codec in LOSSLESS_CODECS:
        return LOSSLESS_CODECS[codec]

    return LOSSY_CODECS.get(codec, 0)


def _audio_streams(probe):
    return [
        stream
        for stream in (probe or {}).get("streams", [])
        if stream.get("codec_type") == "audio"
    ]


def _stream_index(stream):
    try:
        return int(stream.get("index"))
    except (TypeError, ValueError):
        return None


def _candidate_sort_key(stream, preferred_channels=None):
    preferred = (
        1
        if preferred_channels
        and _channels(stream) == preferred_channels
        else 0
    )

    return (
        preferred,
        _codec_score(stream),
        _bitrate(stream),
        _channels(stream),
        -(_stream_index(stream) or 0),
    )


def select_audio_streams(probe, original_language, settings):
    """
    Return a policy decision containing kept/removed audio streams and reason.
    """
    LOGGER.info(
        "%s Starting audio selection: original_language=%r mode=%r "
        "preferred_profile=%r strict_profile=%r",
        PLUGIN_LOG_PREFIX,
        original_language,
        settings.get("Selection mode"),
        settings.get("Preferred profile"),
        settings.get("Strict preferred profile"),
    )

    normalized_language = normalize_language(original_language)
    audio_streams = _audio_streams(probe)

    LOGGER.info(
        "%s Audio stream count=%s normalized_original_language=%s",
        PLUGIN_LOG_PREFIX,
        len(audio_streams),
        normalized_language,
    )

    for number, stream in enumerate(audio_streams, start=1):
        _log_stream(
            stream,
            label=f"audio stream #{number}",
        )

    if not audio_streams:
        LOGGER.warning(
            "%s No audio streams found",
            PLUGIN_LOG_PREFIX,
        )
        return {
            "changed": False,
            "keep": [],
            "remove": [],
            "reason": "no audio streams",
        }

    if not normalized_language:
        LOGGER.warning(
            "%s No normalized original language available",
            PLUGIN_LOG_PREFIX,
        )
        return {
            "changed": False,
            "keep": audio_streams,
            "remove": [],
            "reason": "missing original language",
        }

    matching = [
        stream
        for stream in audio_streams
        if stream_language(stream) == normalized_language
    ]

    LOGGER.info(
        "%s Matching original-language audio streams=%s of %s",
        PLUGIN_LOG_PREFIX,
        len(matching),
        len(audio_streams),
    )

    if not matching:
        LOGGER.warning(
            "%s No audio streams matched original language=%s",
            PLUGIN_LOG_PREFIX,
            normalized_language,
        )
        return {
            "changed": False,
            "keep": audio_streams,
            "remove": [],
            "reason": "no matching original-language audio",
        }

    mode = settings.get("Selection mode") or MODE_REMOVE_COMMENTARY

    if mode not in {
        MODE_KEEP_ALL,
        MODE_REMOVE_COMMENTARY,
        MODE_SINGLE,
    }:
        LOGGER.warning(
            "%s Unknown selection mode=%r; using default mode=%r",
            PLUGIN_LOG_PREFIX,
            mode,
            MODE_REMOVE_COMMENTARY,
        )
        mode = MODE_REMOVE_COMMENTARY

    LOGGER.info(
        "%s Applying selection mode=%s",
        PLUGIN_LOG_PREFIX,
        mode,
    )

    if mode == MODE_KEEP_ALL:
        keep = matching

        LOGGER.info(
            "%s Keep-all mode selected %s stream(s)",
            PLUGIN_LOG_PREFIX,
            len(keep),
        )

    elif mode == MODE_REMOVE_COMMENTARY:
        keep = [
            stream
            for stream in matching
            if not is_commentary_or_audio_description(stream)
        ]

        LOGGER.info(
            "%s Commentary-removal mode retained %s of %s "
            "matching stream(s)",
            PLUGIN_LOG_PREFIX,
            len(keep),
            len(matching),
        )

    else:
        exclude_commentary = (
            settings.get(
                "Exclude commentary/audio-description in single-stream mode"
            )
            is not False
        )

        if exclude_commentary:
            candidates = [
                stream
                for stream in matching
                if not is_commentary_or_audio_description(stream)
            ]
        else:
            candidates = list(matching)

        LOGGER.info(
            "%s Single-stream mode candidates=%s "
            "exclude_commentary=%s",
            PLUGIN_LOG_PREFIX,
            len(candidates),
            exclude_commentary,
        )

        if not candidates:
            LOGGER.warning(
                "%s Only commentary/audio-description streams matched",
                PLUGIN_LOG_PREFIX,
            )
            return {
                "changed": False,
                "keep": audio_streams,
                "remove": [],
                "reason": "only commentary/audio-description matched",
            }

        profile = settings.get("Preferred profile") or PROFILE_BEST
        preferred_channels = PROFILE_CHANNELS.get(profile)

        LOGGER.info(
            "%s Single-stream profile=%s preferred_channels=%s",
            PLUGIN_LOG_PREFIX,
            profile,
            preferred_channels,
        )

        if (
            preferred_channels
            and settings.get("Strict preferred profile")
        ):
            candidates = [
                stream
                for stream in candidates
                if _channels(stream) == preferred_channels
            ]

            LOGGER.info(
                "%s Strict profile filtering retained %s candidate(s)",
                PLUGIN_LOG_PREFIX,
                len(candidates),
            )

            if not candidates:
                LOGGER.warning(
                    "%s Preferred channel profile unavailable",
                    PLUGIN_LOG_PREFIX,
                )
                return {
                    "changed": False,
                    "keep": audio_streams,
                    "remove": [],
                    "reason": "preferred channel profile unavailable",
                }
        else:
            preferred_channels = (
                preferred_channels
                if profile != PROFILE_BEST
                else None
            )

        for candidate_number, candidate in enumerate(
            candidates,
            start=1,
        ):
            _log_stream(
                candidate,
                label=f"single-stream candidate #{candidate_number}",
            )

        keep = [
            max(
                candidates,
                key=lambda stream: _candidate_sort_key(
                    stream,
                    preferred_channels,
                ),
            )
        ]

        _log_stream(
            keep[0],
            label="single-stream selected",
        )

    if not keep:
        LOGGER.warning(
            "%s Selection would remove all audio streams",
            PLUGIN_LOG_PREFIX,
        )
        return {
            "changed": False,
            "keep": audio_streams,
            "remove": [],
            "reason": "selection would remove all audio",
        }

    keep_indices = {
        _stream_index(stream)
        for stream in keep
    }

    remove = [
        stream
        for stream in audio_streams
        if _stream_index(stream) not in keep_indices
    ]

    changed = bool(remove)

    LOGGER.info(
        "%s Final audio selection: keep_indices=%s "
        "remove_indices=%s changed=%s",
        PLUGIN_LOG_PREFIX,
        sorted(
            index
            for index in keep_indices
            if index is not None
        ),
        [
            _stream_index(stream)
            for stream in remove
        ],
        changed,
    )

    return {
        "changed": changed,
        "keep": keep if changed else audio_streams,
        "remove": remove,
        "reason": (
            "audio streams selected"
            if changed
            else "already matches policy"
        ),
    }


def build_ffmpeg_command(file_in, file_out, kept_audio_streams):
    """Build an FFmpeg stream-copy remux command."""
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
            command.extend(
                [
                    "-map",
                    f"0:{index}",
                ]
            )

    command.extend(
        [
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
        ]
    )

    LOGGER.info(
        "%s Built FFmpeg command: %s",
        PLUGIN_LOG_PREFIX,
        " ".join(command),
    )

    return command


def _settings_dict(library_id=None):
    try:
        settings = Settings(library_id=library_id).get_setting()

        LOGGER.debug(
            "%s Loaded settings for library_id=%s: keys=%s",
            PLUGIN_LOG_PREFIX,
            library_id,
            sorted(settings.keys()) if isinstance(settings, dict) else None,
        )

        return settings

    except Exception as error:
        LOGGER.exception(
            "%s Failed to load settings for library_id=%s: %s",
            PLUGIN_LOG_PREFIX,
            library_id,
            error,
        )
        return {}


def _cached_or_detected_plan(file_path, data, settings):
    shared = data.setdefault("shared_info", {})

    if (
        shared.get("original_audio_file") == file_path
        and "original_audio_plan" in shared
    ):
        plan = shared.get("original_audio_plan")

        _log(
            data,
            (
                f"Using cached audio plan for file={file_path}: "
                f"changed={plan.get('changed')} "
                f"reason={plan.get('reason')}"
            ),
        )

        return plan

    _log(
        data,
        f"Starting plan calculation for file={file_path}",
    )

    original_language = shared.get("original_audio_language")

    if original_language:
        _log(
            data,
            f"Using cached original language={original_language}",
        )
    else:
        _log(
            data,
            "Original language not cached; querying Radarr/Sonarr",
        )

        original_language = detect_original_language(
            file_path,
            settings,
        )

    if not original_language:
        _log(
            data,
            "Plan stopped: original language unavailable",
            logging.WARNING,
        )

        plan = {
            "changed": False,
            "keep": [],
            "remove": [],
            "reason": "original language unavailable",
        }
    else:
        _log(
            data,
            f"Original language available: {original_language}",
        )

        probe = (
            shared.get("original_audio_probe")
            or run_ffprobe_json(file_path)
        )

        if not probe:
            _log(
                data,
                "Plan stopped: ffprobe unavailable",
                logging.WARNING,
            )

            plan = {
                "changed": False,
                "keep": [],
                "remove": [],
                "reason": "ffprobe unavailable",
            }
        else:
            _log(
                data,
                "ffprobe data available; applying audio policy",
            )

            plan = select_audio_streams(
                probe,
                original_language,
                settings,
            )

            shared["original_audio_probe"] = probe

    shared["original_audio_file"] = file_path
    shared["original_audio_language"] = original_language
    shared["original_audio_plan"] = plan

    _log(
        data,
        (
            f"Plan complete: changed={plan.get('changed')} "
            f"keep={len(plan.get('keep') or [])} "
            f"remove={len(plan.get('remove') or [])} "
            f"reason={plan.get('reason')}"
        ),
    )

    return plan


def on_library_management_file_test(data, **kwargs):
    """
    Queue only files where a safe audio stream-copy remux would remove audio.
    """
    file_path = data.get("path")

    _log(
        data,
        (
            "Library file test started: "
            f"path={file_path!r} "
            f"library_id={data.get('library_id')!r}"
        ),
    )

    if not file_path:
        _log(
            data,
            "Library file test skipped: data contains no path",
            logging.WARNING,
        )
        return data

    settings = _settings_dict(data.get("library_id"))
    plan = _cached_or_detected_plan(
        file_path,
        data,
        settings,
    )

    if plan.get("changed") and plan.get("keep"):
        data["add_file_to_pending_tasks"] = True

        _log(
            data,
            (
                "Library file test PASSED: queuing file "
                f"because {len(plan.get('remove') or [])} "
                "audio stream(s) would be removed"
            ),
        )
    else:
        _log(
            data,
            (
                "Library file test did not queue file: "
                f"reason={plan.get('reason')} "
                f"changed={plan.get('changed')} "
                f"keep={len(plan.get('keep') or [])} "
                f"remove={len(plan.get('remove') or [])}"
            ),
            logging.INFO,
        )

    return data


def on_worker_process(data):
    """
    Configure Unmanic to run an audio-only stream-copy remux.
    """
    file_in = data.get("file_in")
    file_out = data.get("file_out")

    _log(
        data,
        (
            "Worker processing started: "
            f"file_in={file_in!r} "
            f"file_out={file_out!r} "
            f"library_id={data.get('library_id')!r}"
        ),
    )

    if not file_in or not file_out:
        _log(
            data,
            "Worker processing skipped: missing file_in or file_out",
            logging.WARNING,
        )

        data["exec_command"] = False
        return data

    settings = _settings_dict(data.get("library_id"))

    plan = _cached_or_detected_plan(
        file_in,
        data,
        settings,
    )

    if not plan.get("changed") or not plan.get("keep"):
        _log(
            data,
            (
                "Worker processing skipped: no safe audio change required; "
                f"reason={plan.get('reason')}"
            ),
            logging.INFO,
        )

        data["exec_command"] = False
        return data

    command = build_ffmpeg_command(
        file_in,
        file_out,
        plan["keep"],
    )

    _log(
        data,
        (
            "Worker processing configured: "
            f"removing {len(plan.get('remove') or [])} "
            "audio stream(s) with stream copy"
        ),
    )

    data["exec_command"] = command

    _log(
        data,
        "Worker processing complete: FFmpeg command assigned",
    )

    return data