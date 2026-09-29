# G0oD Auto Meets

An agent-run assistant for online lectures and work meetings. It joins scheduled sessions, records audio and useful screen frames locally, and prepares a brief summary or a detailed report with slides.

**You use this through an AI agent, not a graphical interface.** Install the Codex plugin from this repository's marketplace:

```powershell
codex plugin marketplace add Andrey-Good/G0oD_auto_meets
codex plugin add good-auto-meets@good-auto-meets
```

The first command registers this GitHub marketplace; subsequent plugin installs need only the second command. The plugin is not yet listed in the public Plugins Directory. In any Codex project, start a new chat and invoke the installed skill:

If PowerShell says `plugin add` is unknown, update an older npm-installed Codex CLI with `npm install -g @openai/codex@latest`, then retry. Alternatively, install the plugin from this marketplace in the Codex desktop app's Plugins directory. Start a new chat after installation so the skill becomes available.

> $good-auto-meets Set up my lecture and meeting assistant. Here is my schedule: `<schedule link or file>`. Additional details: `<anything important, optional>`.

The agent should ask for essential preferences, prepare local tools, schedule meeting wakeups, and offer a test of joining, recording, and transcribing a short sample with your permission. You do not need to run recorder commands yourself.

## How it works

- **The agent is the main operator.** It reads the calendar, follows meeting links, handles the conference interface, starts and monitors recordings, and writes the report.
- **The [plugin skill](plugins/good-auto-meets/skills/good-auto-meets/SKILL.md) instructs the installed agent.** It routes setup, schedule updates, capture, and reporting. [AGENTS.md](AGENTS.md) and the [repository skills](.agents/skills/) serve people working directly in this source repository. Calendar-specific links, preferences, and tested workarounds live in private user skills.
- **The bundled Python code gives the agent recording tools.** It captures audio and selected frames, runs local whisper.cpp transcription, stores the session, and renders HTML. Calendar access and wakeups come from the agent environment, not from a scheduler inside this package.

## What is available

- Background capture of a browser process tree's audio and a selected window on Windows 11, with optional microphone input and frame cropping.
- Chunked transcription during recording, later reprocessing, and recovery of interrupted WAV files without discarding the source audio.
- Selection of changed frames, merging of repeated slides, and timestamps for their later appearances. There is no fixed slide count.
- Optional continuous capture of meeting chat into the same session, using a small local browser extension. Chat messages can be cited in the HTML report.
- A **template for an HTML report** with audio, timestamps, frames, summary, decisions, tasks, and organizational notes. Until the agent writes the content, the report is marked as a draft.
- One local recorder settings file, diagnostics, event deduplication, and a synthetic demo.

## Working with the agent

Chat capture is optional. If you choose it, the agent asks whether you want it to load the [browser extension](browser-extension/) through Computer Use (where permitted) or prefer to do that yourself. It must be loaded once in Chrome or Edge; the agent then runs `chat-start` with the meeting URL and a message selector. The extension activates in the matching tab automatically. It asks for access to websites so it can work across meeting platforms, but sends chat text only to the local collector while a session is active. See [usage](docs/USAGE.md).

After an extension update, reload it in each browser's extensions page so the browser runs the new version. `chat-status` reports a version mismatch; audio and frame capture continue if chat is unavailable.

**Browser Use is required** to inspect calendars and meeting pages and to verify that the right conference is open. Computer Use is needed when the agent must inspect or resolve unexpected desktop, window, or audio problems. The agent should request permission before a first connection or test recording when permission has not already been given.

On first use, the plugin's [runner](plugins/good-auto-meets/scripts/auto_meets.py) uses this repository if it is the current project, or clones it into `~/Documents/G0oD_auto_meets`. It creates a private Python environment and recorder settings there, outside the plugin cache. The agent can then use `init`, `doctor`, `browser`, `windows`, `start`, `status`, `stop`, `process`, `render`, `list`, `import-wav`, and `demo` through that runner. It also installs or locates the required GStreamer, whisper.cpp, and ASR model during setup. See [usage](docs/USAGE.md) and [Windows acceptance checks](docs/TESTING.md) for command details.

All meeting files stay inside the repository under `data/sessions/<UTC-date-time>_<title>_<id>/`: audio, selected frames, transcript, logs, and `report.html`. Settings are in `data/recorder.toml`; scheduling state and locks are in `data/runtime/`; the optional browser profile is in `data/browser/`. The entire `data/` tree is Git-ignored. Open a session's `report.html` for the result. Private calendar skills live in `~/.agents/skills/local-auto-meets-*/`, outside the plugin cache and Git. Use `AUTO_MEETS_HOME` to point the runner at another checkout of this repository.

## Requirements and limits

The first real capture backend requires **Windows 11, Python 3.11+, and GStreamer 1.24+**. Plugin installation alone does not install GStreamer, whisper.cpp, or a speech model: the agent must install or locate them during first setup, configure their paths, run diagnostics, and offer a short real capture test. Linux and macOS can run the demo, import WAV files, transcribe, and render HTML, but do not yet have window/audio capture adapters. whisper.cpp runs locally; available CPU, CUDA, or Vulkan acceleration depends on the machine and installation.

Audio capture covers the browser's **whole process tree**, not a single tab. Other audible tabs in that browser can enter the recording. The window may be covered by another window, but minimized, locked-screen, remote-desktop, and protected-video capture are not established as reliable. One recording runs at a time per storage root.

The computer must be on and the agent application running before a lecture so its wakeup can occur. Agent behavior is not deterministic: meeting interfaces, audio routing, recognition, and summaries can fail, so test the whole path before relying on it. Agent models differ in quality; choose one that suits your needs. Chunk boundaries, names, numbers, deadlines, and small slide changes deserve review. There is no speaker diarization or OCR, and the Python package does not write the summary itself.

Only record meetings for which you have the necessary permission. Transcription runs locally, but text and images read by a cloud-hosted agent may reach its provider. Do not commit recordings, cookies, models, tokens, or private calendar skills. See [ARCHITECTURE.md](ARCHITECTURE.md) for design details and sources.
