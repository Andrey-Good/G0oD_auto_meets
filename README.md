# G0oD Auto Meets

An agent-run assistant for online lectures and work meetings. It joins scheduled sessions, records audio and useful screen frames locally, and prepares a brief summary or a detailed report with slides.

**You use this repository through an AI agent, not a graphical interface.** Add the repository as a project in an agent environment, then send the agent a message like this:

> Set up this repository for me. Here is my schedule: `<schedule link or file>`. Additional details: `<anything important, optional>`.

The agent should ask for essential preferences, prepare the local tools, schedule meeting wakeups, and offer a permission-based test of joining a meeting, recording it, and transcribing a short sample. You do not need to run the recorder's commands yourself.

## How it works

- **The agent is the main operator.** It reads the calendar, follows meeting links, handles the conference interface, starts and monitors recordings, and writes the report.
- **[AGENTS.md](AGENTS.md) and the [skills](.agents/skills/) instruct the agent.** Shared skills cover setup, schedule updates, capture, and reporting. Calendar-specific links, preferences, and tested workarounds live in private skills excluded from Git.
- **The Python code gives the agent recording tools.** It captures audio and selected frames, runs local whisper.cpp transcription, stores the session, and renders HTML. Calendar access and wakeups come from the agent environment, not from a scheduler inside this package.

## What is available

- Background capture of a browser process tree's audio and a selected window on Windows 11, with optional microphone input and frame cropping.
- Chunked transcription during recording, later reprocessing, and recovery of interrupted WAV files without discarding the source audio.
- Selection of changed frames, merging of repeated slides, and timestamps for their later appearances. There is no fixed slide count.
- A **template for an HTML report** with audio, timestamps, frames, summary, decisions, tasks, and organizational notes. Until the agent writes the content, the report is marked as a draft.
- One local recorder settings file, diagnostics, event deduplication, and a synthetic demo.

## Working with the agent

**Browser Use is required** to inspect calendars and meeting pages and to verify that the right conference is open. Computer Use is needed when the agent must inspect or resolve unexpected desktop, window, or audio problems. The agent should request permission before a first connection or test recording when permission has not already been given.

The agent can use `auto-meets init` and `doctor` for setup; `browser` and `windows` to identify the meeting window; `start`, `status`, and `stop` for capture; and `process` and `render` for the finished report. It also has `list`, `import-wav`, and `demo`. See [usage](docs/USAGE.md) and [Windows acceptance checks](docs/TESTING.md) for command details.

Recordings and reports are stored locally under `data/<session-id>/`; open that session's `report.html` to read the result. `data/recorder.toml` holds this computer's technical settings. Private calendar skills are stored in `.agents/skills/local-*/`. Both locations are excluded from Git.

## Requirements and limits

The first real capture backend requires **Windows 11, Python 3.11+, and GStreamer 1.24+**. Linux and macOS can run the demo, import WAV files, transcribe, and render HTML, but do not yet have window/audio capture adapters. whisper.cpp runs locally; available CPU, CUDA, or Vulkan acceleration depends on the machine and installation.

Audio capture covers the browser's **whole process tree**, not a single tab. Other audible tabs in that browser can enter the recording. The window may be covered by another window, but minimized, locked-screen, remote-desktop, and protected-video capture are not established as reliable. One recording runs at a time per storage root.

The computer must be on and the agent application running before a lecture so its wakeup can occur. Agent behavior is not deterministic: meeting interfaces, audio routing, recognition, and summaries can fail, so test the whole path before relying on it. Agent models differ in quality; choose one that suits your needs. Chunk boundaries, names, numbers, deadlines, and small slide changes deserve review. There is no speaker diarization or OCR, and the Python package does not write the summary itself.

Only record meetings for which you have the necessary permission. Transcription runs locally, but text and images read by a cloud-hosted agent may reach its provider. Do not commit recordings, cookies, models, tokens, or private calendar skills. See [ARCHITECTURE.md](ARCHITECTURE.md) for design details and sources.
