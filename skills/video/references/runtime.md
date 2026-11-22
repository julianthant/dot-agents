# Runtime and optional transcription

The bundled Python scripts use the standard library. Python 3.10+ is required. ffmpeg/ffprobe run
locally; yt-dlp fetches URL metadata, English captions and media. The upstream caption request uses
`en.*`; do not describe absent English captions as proof that no other-language captions exist.

Check binaries without reading secrets:
```sh
python3 --version
command -v ffmpeg
command -v ffprobe
command -v yt-dlp
```
Missing binaries can be installed through the user's available package manager within the authorized
setup scope. On this Mac, ffmpeg/ffprobe were present and yt-dlp was installed with `uv tool install
yt-dlp`. If a later host's PATH omits uv's tools, check its user executable directory and use its
verified path. Do not run the upstream setup wizard just to require a key or collect preferences.
No .env file or SETUP_COMPLETE marker is required for a captions/frames run of watch.py.

Always start with `--no-whisper`. It disables the external fallback while retaining native captions.
If a requested spoken answer needs unavailable captions, use a supplied transcript or explain the
missing evidence. Offer the optional fallback only when it would help. Reuse existing explicit
provider authorization; never request keys in chat or print them.

For authorized transcription, remove `--no-whisper` and choose `--whisper groq` or `--whisper openai`.
The runtime uploads extracted audio to the selected provider. It processes the entire audio before
filtering transcript timestamps, even with --start/--end; do not promise that focus flags limit the
upload to that interval. If authorization covers only an interval, prepare a local clipped copy,
transcribe only that copy and restore the source timestamp offset in the answer.

Keys are resolved from the environment, ~/.config/watch/.env, then the current directory's .env.
Run from neutral scratch space to avoid borrowing project credentials. Preserve existing config and
credentials; use the user's secure configuration mechanism if setup is explicitly requested. The
upstream config path remains `watch` for compatibility even though the skill is called /video.
There is no automatic fallback to another provider's authorization if the chosen provider fails.

Treat errors as evidence limits. Stop repeated authentication or region-lock retries and explain the
access problem. Do not export browser cookies or bypass access restrictions. Missing or failed
transcription can still permit a clearly labeled visual-only answer. Partial chunk failures must
remain visible; read stderr as well as the final report. Retain the original error for diagnosis.

The runtime may load normal yt-dlp configuration from the host. Check relevant options if behavior
suggests unexpected cookies, destinations or playlist handling. Do not assume every URL is public.
The skill does not install upstream SessionStart hooks or modify any global agent instructions.
