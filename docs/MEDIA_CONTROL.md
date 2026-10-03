# JARVIS Media Control & Video Understanding

Brahma Evo's existing Spotify and YouTube tools now expose a unified conversational media workflow.

## Spotify

JARVIS can:
- play a song, album, artist, or playlist
- resolve a playlist by name and start its Spotify context
- pause/resume/skip
- control volume
- inspect now-playing, queue, and available Spotify Connect devices

Spotify playlist playback uses the user's authenticated Spotify account and Spotify Connect device. Spotify's Web API requires a Premium account for playback control.

## YouTube and browser video

JARVIS can:
- play a YouTube video from a search query or direct URL
- open and play a YouTube playlist by name or URL
- control the active HTML5 video with play, pause, toggle, seek, volume, mute, fullscreen, and arbitrary requested playback rates
- read the active video's playback state
- control videos on normal web pages when the page exposes an HTML5 `<video>` element

Examples:
- "Jarvis, play this video at 1.70x."
- "Jarvis, pause the video."
- "Jarvis, jump to 2:10."
- "Jarvis, put the playlist Bomba on."
- "Jarvis, play the video with the title ..."

## Watch / analyze

The video-understanding path can inspect a public YouTube video directly with Gemini, including:
- spoken dialogue
- visible scenes
- on-screen text
- symbols and codes
- timestamp-focused sections

A user can ask:
- "Watch this video and summarize it."
- "Look at 4:20 to 4:45 and tell me what the message says."
- "Watch this video and decode the Morse code in that section."

For a timestamp-specific question, JARVIS passes the interval to the video model and explicitly asks it to inspect both visual and audio information rather than relying only on a transcript.

Local video files can also be uploaded temporarily to Gemini for analysis. The uploaded file is deleted after analysis when the provider permits deletion.

## Resource / privacy behavior

YouTube URL analysis does not require downloading the whole public video into the PC. Local-file analysis uses a temporary provider upload for the requested analysis.

Playback control runs in JARVIS's managed browser session. It does not claim to control a completely unrelated browser window unless that page is the session JARVIS is controlling.

## Limits

YouTube playback speed is implemented against the page's HTML5 video element. A specific service/player can still clamp or override unsupported rates.

Spotify's current Web API provides playback control and seeking, but not a normal track playback-speed control. Therefore a request such as "Spotify at 1.70x" is reported as unsupported instead of pretending it was changed.

Video analysis requires a configured Gemini API key and, for YouTube URL analysis, a public YouTube video.
