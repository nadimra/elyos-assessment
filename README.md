# Elyos Assessment

A terminal chat app that streams responses from Claude and uses two tools — `get_weather` and `research_topic` — backed by the Elyos interview API.

## Getting started

1. **Create and activate a virtualenv**

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies**

   ```sh
   pip install -r requirements.txt
   ```

3. **Set environment variables** in a `.env` file at the project root:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ELYOS_API_KEY=...
   ```

4. **Run the app**

   ```sh
   python main.py
   ```

   Type a message and press Enter. Press `Ctrl+C` to cancel an in-flight reply; type `quit` to exit.

## Main files

| File | Purpose |
|---|---|
| `main.py` | The entire chat app: input loop, streaming, tool execution, cancellation. |
| `docs/api-notes.md` | Observations and quirks discovered while integrating the `/weather` and `/research` endpoints. The main reference for *why* the code handles responses the way it does. |
| `probes/probe_weather.py`, `probes/probe_research.py` | Exploratory scripts used to map out the Elyos API behaviour. Their JSON output is checked in alongside. |
| `docs/claude-discussions/` | Transcripts of the Claude-assisted exploration for each milestone — the skeleton, the weather tool, and the research tool. Useful for seeing how the design evolved. |

## Directory structure

```
.
├── main.py                    # Chat app entry point
├── requirements.txt
├── README.md
├── docs/
│   ├── api-notes.md           # API quirks and implementation notes
│   ├── interview.md           # Original assessment brief
│   └── claude-discussions/    # Transcripts of Claude-assisted exploration
└── probes/
    ├── probe_weather.py       # Weather endpoint survey
    ├── probe_weather_results.json
    ├── probe_research.py      # Research endpoint survey
    └── probe_research_results.json
```
