# Keep Original Language Audio

Unmanic plugin that removes non-original-language audio streams from media files.

This plugin only selects and removes audio streams. It never transcodes audio, downmixes audio, transcodes video, or modifies subtitles, chapters, attachments, metadata, or other non-audio streams. Use a separate Audio Transcoder plugin after this plugin if the remaining audio should be converted to stereo AAC.

## Installation

Copy this plugin directory into your Unmanic plugins directory and enable **Keep Original Language Audio** in Unmanic.

The plugin requires `ffprobe` and `ffmpeg` to be available to Unmanic. Python dependencies are listed in `requirements.txt`.

## Configuration

### Radarr and Sonarr

Configure:

- Radarr URL
- Radarr API key
- Sonarr URL
- Sonarr API key
- Optional request timeout
- Optional path mappings

Radarr is used for movies and Sonarr is used for series. The plugin matches the current file path to Radarr movie paths/movie-file paths and Sonarr series paths/episode-file paths, then reads the original language from the matching response.

If Radarr/Sonarr are unavailable, not configured, time out, cannot match the file, or do not report an original language, the plugin leaves the file unchanged.

### Path mappings

Do not assume Unmanic sees the same filesystem paths as Radarr or Sonarr. If paths differ, add one mapping per line:

```text
/unmanic/library/path=/radarr-or-sonarr/library/path
```

Example:

```text
/library/movies=/data/movies
/library/tv=/data/tv
```

The left side is the path prefix visible to Unmanic. The right side is the corresponding path prefix visible to Radarr/Sonarr.

## Selection modes

Default: **Keep original-language audio and remove commentary/audio-description tracks**.

1. **Keep all original-language audio**
   - Keeps every audio stream whose language matches the detected original language.
   - Keeps commentary and compatibility tracks when they are in the original language.
   - Removes audio streams in other languages.

2. **Keep original-language audio and remove commentary/audio-description tracks**
   - Keeps original-language audio.
   - Removes original-language commentary/audio-description tracks.
   - Removes audio streams in other languages.

3. **Keep only one original-language audio stream**
   - Selects exactly one suitable original-language stream.
   - By default excludes commentary/audio-description candidates before ranking.
   - Uses the preferred profile and quality ranking below.

The plugin never intentionally removes all audio. If it cannot safely select at least one audio stream, it skips processing.

## Preferred profile

Single-stream mode supports:

- Best available
- Prefer stereo / 2.0
- Prefer 5.1
- Prefer 7.1

The profile is a preference, not a conversion rule. The plugin does not convert 7.1 to 5.1 or stereo. It only chooses which stream to keep.

Strict preferred profile is optional:

- Disabled by default: if the preferred channel profile is unavailable, the plugin falls back to the best available original-language stream.
- Enabled: if the preferred channel profile is unavailable, the plugin skips the file unchanged.

## Quality ranking

In single-stream mode, candidates are ranked predictably:

1. Correct original language.
2. Exclude commentary/audio-description streams when configured.
3. Match the preferred channel profile.
4. Prefer lossless codecs:
   - TrueHD
   - FLAC
   - DTS-HD MA
5. Prefer higher-quality lossy codecs:
   - E-AC-3
   - DTS
   - AC-3
   - AAC
   - Opus
   - MP3
6. Use bitrate as a tiebreaker.
7. Use channel count as a later tiebreaker.

The plugin does not rank solely by bitrate.

## Commentary and audio-description detection

Detection is separate from language matching and uses audio stream titles only. Matching is case-insensitive.

Detected markers include:

- commentary
- director commentary
- cast commentary
- audio commentary
- audio description
- descriptive
- narration
- descriptive audio
- AD as a standalone token

To minimize false positives, `comment`/`comments` is treated as commentary only when it is the whole title, and `AD` is matched only as a standalone token, not inside words such as `Adventure`.

## Language normalization

Language comparison is normalized before matching. The plugin uses `pycountry` for broad ISO language coverage, including ISO 639-1 codes, ISO 639-2 bibliographic/terminology variants, common language names, and case-insensitive values.

Common variants include:

- `eng`, `en`, `English` => English
- `fra`, `fre`, `fr`, `French` => French
- `nld`, `dut`, `nl`, `Dutch` => Dutch
- `rus`, `ru`, `Russian` => Russian
- `ukr`, `uk`, `Ukrainian` => Ukrainian

Missing or `und` stream language tags are treated as unsafe and do not match. If no stream safely matches the detected original language, the plugin leaves the file unchanged.

## FFmpeg behavior

The worker builds a stream-copy remux command:

- Copies video streams.
- Copies the selected audio streams.
- Copies subtitle streams.
- Copies attachments.
- Copies data streams.
- Preserves chapters.
- Preserves container metadata.

It uses `-c copy` and explicit stream mapping. It does not use audio or video encoders.

## Recommended Unmanic plugin flow

Library Management - File test:

1. Keep Original Language Audio
2. Transcode Audio
3. Transcode Video Files

Worker - Processing file:

1. Keep Original Language Audio
2. Re-order audio streams by language
3. Re-order subtitle streams by language
4. Transcode Audio
5. Transcode Video Files

Run this plugin before the separate Audio Transcoder. This plugin removes unwanted audio streams; the Audio Transcoder performs later conversion such as stereo AAC.

## Examples

### English movie

Input:

- English TrueHD Atmos 7.1
- English AC-3 5.1 compatibility track
- English AC-3 commentary
- English subtitles
- Spanish subtitles

All audio streams are tagged `eng`.

Results:

- Language-only mode keeps all three English audio tracks.
- Commentary-removal mode removes only the commentary audio track.
- Single-stream mode selects the best non-commentary English stream according to the configured preferred profile.
- Subtitles are preserved.

With single-stream mode and **Prefer 5.1**, the English AC-3 5.1 stream is kept. If no 5.1 stream exists and strict mode is disabled, the plugin falls back to the best available original-language stream.

### French movie

Input:

- French DTS-HD MA 5.1
- English E-AC-3 dub
- German AC-3 dub
- French commentary

Results:

- French audio is kept.
- English and German dubs are removed.
- French commentary is removed only when commentary removal is enabled.
- In single-stream mode, French DTS-HD MA 5.1 is selected unless another available profile is preferred.

## Fallback and idempotency

The plugin queues a file only when it can safely make an audio-only change. A file already matching the configured policy is not queued again, which avoids processing loops.

Safe no-op cases include:

- Missing Radarr/Sonarr configuration.
- API unavailable or timed out.
- File cannot be matched to Radarr/Sonarr.
- Original language is missing.
- Audio language tags are missing or `und`.
- No audio stream matches the original language.
- Strict preferred profile is enabled and the preferred profile is unavailable.
- The configured policy would remove all audio.

## Limitations

- Correct behavior depends on accurate Radarr/Sonarr original-language metadata.
- Correct stream selection depends on accurate audio language tags and useful audio titles.
- The plugin does not repair missing language tags.
- Stream-copy remuxing must be supported by the input/output container combination used by FFmpeg.
