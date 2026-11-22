# Focused inspection

Use the question to select evidence. Keep source timestamps with every observation and quotation.

| Need | Runtime flags |
| --- | --- |
| Spoken explanation or lecture outline | --detail transcript |
| General visual overview | --detail efficient |
| Scene transitions and demonstrations | --detail balanced |
| Specific interval | --start 2:15 --end 2:45 |
| Exact moments, including cues in the transcript | --detail transcript --timestamps 2:20,2:34 |
| Small code, slide text or UI labels | --resolution 1024 on the relevant interval |
| Smaller image budget | --max-frames 20, adjusted to the task |
| Subtle motion that deduplication might hide | --no-dedup on a short interval |

These are arguments to scripts/watch.py; add --no-whisper unless the named provider upload is
authorized. Source times accept seconds, MM:SS or HH:MM:SS. Cue timestamps remain absolute and are
counted against the cap. Focus filters captions to the requested window. Frames alone cannot reveal
speech or all motion between samples; the upstream sampler caps at 2 fps.

Efficient mode uses keyframes and caps at 50; balanced mode uses scene changes with uniform fallback
and caps at 100. A cap is a maximum, not a target number of images to load. Start with a useful budget
and expand only where the answer needs it. Avoid uncapped token-burner mode unless exhaustive candidate
inspection is specifically needed; even that mode cannot establish continuous-frame coverage.
Do not quote fixed image-token estimates because tokenization depends on the platform and image size.

A speaker saying "look here" is a cue to inspect that moment, not a sufficient visual description.
Read the transcript, select relevant cue times, then extract and view those images. Preserve exact
commands only when legible; increase resolution or narrow the interval instead of guessing characters.
Reuse a local downloaded source for a second extraction when one exists, with a fresh scratch output
directory. Keep the first run's transcript as evidence because local-file runs do not fetch URL captions.

For an application demo, distinguish shown behavior from promised behavior. For debugging a recording,
identify the first visible discrepancy and then route actual code diagnosis to /diagnose if requested.
If the user asks to create or edit a video, explain that this skill provides inspection only and use an
available appropriate creation capability without pretending the runtime can render edits.
