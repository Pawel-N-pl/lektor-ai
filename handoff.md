# Handoff Spec: Batch ElevenLabs Polish Voiceover Generator

## Goal

Create a Python automation script that batch processes two seasons of TV show `.mkv` files. For each episode, it extracts the Polish subtitle track, generates time-synced audio using ElevenLabs API, and merges the new voiceover track back into a new `.mkv` container.

## Environment & Requirements

* Python 3.10+
* Dependencies: `elevenlabs`, `pysrt`, `pydub`
* System binaries required: `ffmpeg`, `mkvmerge`

## Workflow Steps

1. **Batch Scan**:
* Recursively scan a target directory (e.g., `./Season_1/`, `./Season_2/`) for all `.mkv` files.


2. **Extract Subtitles**:
* Use `ffmpeg` or `mkvextract` via `subprocess` to extract track ID 32 (`S_TEXT/UTF8`) to a temporary `.srt` file.


3. **Subtitle Parsing & Audio Generation**:
* Parse the `.srt` file using `pysrt`.
* Clean the text (remove HI tags like `[sighs]` or formatting tags).
* Convert subtitle timestamps into milliseconds.
* Iterate through cues, sending text to ElevenLabs TTS API (`eleven_multilingual_v2` model) to generate audio clips for each line.
* Build a continuous audio track using `pydub`, placing each audio clip at its precise start timestamp with silent gaps in between.


4. **Muxing (Merging)**:
* Export the combined audio track as `voiceover.wav`.
* Call `mkvmerge` to merge the original `.mkv` and `voiceover.wav` into a new file in `./output/` with suffix `_PL_TTS.mkv`.
* Retain all original video/audio streams and append the new voiceover as a secondary audio track.


5. **Cleanup**:
* Remove temporary `.srt` and `.wav` chunk files after processing each episode.



## Configuration File (`config.json`)

Please structure the code to read parameters from a `config.json` file:json
{
"elevenlabs_api_key": "YOUR_API_KEY",
"voice_id": "YOUR_PREFERRED_VOICE_ID",
"model_id": "eleven_multilingual_v2",
"subtitle_track_index": 32,
"input_dir": "./input_seasons",
"output_dir": "./output_seasons"
}

```

## Error Handling
- Skip files if track 32 is missing or corrupted, logging the error to `error_log.txt`.
- Implement basic rate-limit retries for ElevenLabs API calls.

```