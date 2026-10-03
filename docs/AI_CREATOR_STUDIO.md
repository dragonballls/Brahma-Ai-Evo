# Brahma AI Creator Studio

Creator Studio is Brahma's conversational video/audio production pipeline.

## Workflow

Idea or request -> analyze actual footage -> deterministic edit plan -> captions/audio cleanup/effects -> title/description/hashtags/tags/chapters -> thumbnail -> rights review -> render -> optional YouTube publish.

## Current capabilities

- Use an existing video or combine multiple source videos.
- Analyze video through the existing Brahma multimodal video-understanding path.
- Generate an editable JSON project manifest before rendering.
- Select or preserve source segments, including per-segment speed changes.
- Generate captions from timestamped analysis cues.
- Clean and normalize audio.
- Add optional background music with automatic ducking under the main audio.
- Render 16:9, 9:16, or 1:1 output.
- Apply clean, cinematic, or vibrant global grading plus fades.
- Generate a thumbnail from a real video frame with concise text.
- Generate a creator-ready script and hook.
- Generate titles, alternative titles, descriptions, hashtags, tags, chapters, thumbnail concepts, pinned-comment suggestions, and Shorts metadata.
- Record microphone audio.
- Generate narration audio through the installed Edge TTS command when available.
- Search the web for copyright-safe or properly licensed music candidates.
- Upload directly to YouTube when OAuth credentials are configured; otherwise prepare the project and open YouTube Studio.
- Record via the existing OBS integration when the user wants OBS to capture the session.

## Open-source building blocks and architecture references

The Creator architecture is informed by these repositories:

- `kwakseongjae/dawn-cut` — MIT. Useful for text-oriented editing, deterministic edit artifacts, subtitles, silence handling, and local-first architecture.
- `deonmenezes/edit-ai` — MIT. Useful for the model-as-editor-agent pattern, tool-driven timeline changes, and approval before destructive operations.
- `andriidrok1/autobroll` — MIT. Useful for short-form workflows, captions, B-roll/keyframe concepts, and Remotion-style deterministic rendering.
- `wakamex/youtube-cli-uploader` — MIT. Useful reference for private-first YouTube Data API upload/authentication.

DeepFilterNet was considered for speech cleanup, but its repository metadata did not provide a clear SPDX license in this audit, so Brahma does not copy its code.

Brahma implements its own integration layer and keeps the existing assistant architecture, OmniRoute, video understanding, settings control, OBS control, and low-power behavior intact.

## Copyright and licensing

Creator Studio does not disguise copyrighted media or attempt to defeat Content ID or other identification systems.

For supplied music, Brahma reports that ownership/licensing must be verified. The music discovery action searches for copyright-safe or properly licensed candidates; it does not assert that a web result is licensed merely because it was found.

Platform-level copyright decisions remain with the platform and the rights holder. When rights are uncertain, Brahma can recommend removing or replacing the music.

## Performance

Creator processing is lazy: no rendering worker runs while Brahma is idle. Media analysis, transcription work, rendering, and audio processing happen only when the relevant Creator command is requested.
