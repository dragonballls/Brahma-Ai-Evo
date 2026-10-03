# OBS Studio Control

Brahma Evo controls OBS Studio through obs-websocket 5.x.

## Supported

- Inspect OBS status, current scene, scenes, stream and recording state.
- Switch, create, remove and rename scenes.
- List scene sources.
- Show or hide sources.
- Move sources by exact coordinates or conversational position presets.
- Resize sources by width/height or explicit scale; single-dimension requests preserve aspect ratio.
- Rotate sources.
- Update OBS text sources for on-screen titles, labels and overlays.
- Set/get mixer volume and mute state.
- Start or stop streaming.
- Start, stop, pause and resume recording.
- Configure the active transition and duration.
- Read or set OBS stream-service settings when the service exposes the desired field.

## Conversational examples

- Switch OBS to Gaming.
- Put the webcam in the upper-right.
- Make the webcam 420 pixels wide.
- Hide the chat overlay.
- Set Mic/Aux to 70 percent.
- Mute the microphone.
- Start recording.
- Stop streaming.
- Change the transition to Fade and make it 500 milliseconds.
- Change my OBS title overlay to Minecraft Day 12.

## Connection

OBS Studio 28+ includes obs-websocket. The normal endpoint is 127.0.0.1:4455 and authentication is normally enabled.

Use the obs_control tool with action=configure when the endpoint or password needs to be supplied. On Windows, a supplied password is protected with Windows DPAPI before persistence. BRAHMA_OBS_WEBSOCKET_PASSWORD is also supported as an environment-provided secret.

Passwords are not included in normal status output.

## Stream titles

Brahma can directly edit an OBS text source used as an on-screen stream/video title. Platform-level stream metadata is service-specific, so Brahma exposes OBS stream-service settings for supported cases rather than pretending there is one universal title field.

## Performance

There is no permanent OBS polling worker. The integration connects lazily when an OBS command is requested and reuses the connection while Brahma is running.
