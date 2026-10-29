# LLM Conversation for Home Assistant

One custom integration, four AI providers: add **Mistral**, **Anthropic Claude**,
**Google Gemini** and **GitHub Models (Copilot)** as conversation agents to
Home Assistant, with real tool calling through the
[Home Assistant LLM API](https://developers.home-assistant.io/docs/core/llm/).
Your AI of choice can answer questions *and* actually control your home
(`HassTurnOn`, climate, covers, media players, and more), with streaming
responses, per-agent models and prompts.

Built and verified against **Home Assistant 2026.10.4**.

## Features

- **Four providers, one codebase** — pick a provider when adding the
  integration:
  - **Mistral AI** — `api.mistral.ai` (API key from
    [console.mistral.ai](https://console.mistral.ai))
  - **Anthropic Claude** — Messages API (API key from
    [console.anthropic.com](https://console.anthropic.com))
  - **Google Gemini** — Gemini API (API key from
    [aistudio.google.com](https://aistudio.google.com))
  - **GitHub Models (Copilot)** — the OpenAI-compatible endpoint at
    `models.github.ai`, serving GPT, Claude, Gemini and open models with a
    GitHub Personal Access Token (`models: read` permission). Free tier
    available for personal accounts.
- **Device control via the LLM API** — the agent calls Home Assistant tools
  directly to toggle lights, set the thermostat, answer "what's the
  temperature in the living room?" and so on. You choose which APIs it may
  use in the options.
- **Streaming** — answers appear token by token in the Assist UI.
- **Complete voice pipeline on Mistral** — Voxtral speech-to-text and
  text-to-speech entities, so the entire Assist pipeline (STT → agent →
  TTS) can run on Mistral alone.
- **Tool-call loop** — up to 8 rounds of tool calling per question, with
  parallel tool calls supported.
- **Live model list** — the options flow fetches the current model catalogue
  from your provider, so new models show up without an update. A static
  fallback list and a free-text "custom value" option cover offline cases and
  brand-new models.
- **Multiple agents** — add several instances, e.g. a fast
  `mistral-small-2603` agent for voice satellites, a `claude-sonnet-5-5`
  agent for nuanced chat and a `gemini-3.8-flash` agent as backup.
- **Custom instructions** — per-agent system prompt with sensible Home
  Assistant defaults.
- **Image input** — image attachments in the chat log are sent to the model
  as multimodal content (for models that support it).
- **English and Dutch UI translations.**
- **No extra Python dependencies** — uses only what Home Assistant ships with.

## Installation

### Option A: HACS (recommended)

1. Install [HACS](https://hacs.xyz) if you haven't already.
2. In HACS, go to **⋮ → Custom repositories**.
3. Add this repository with category **Integration**.
4. Find **LLM Conversation** in HACS and download it.
5. Restart Home Assistant.

### Option B: Manual

1. Copy `custom_components/llm_conversation/` to the `custom_components/`
   directory of your Home Assistant configuration.
2. Restart Home Assistant.

## Configuration

1. In Home Assistant: **Settings → Devices & Services → Add Integration**,
   search for **LLM Conversation**.
2. Pick a provider and paste the API key:
   - Mistral: console.mistral.ai → API Keys
   - Anthropic: console.anthropic.com → API Keys
   - Google: aistudio.google.com → Get API key
   - GitHub Models: github.com → Settings → Developer settings →
     Fine-grained personal access token with **Models: read** permission
3. Open the entry and **Configure** it:
   - **Control Home Assistant**: select **Assist** to enable device control.
   - **Model**: pick from the live list, or type any model ID.
   - **Temperature**: 0.0 – 1.0, default 0.2 (not sent to Claude — current
     Claude models reject non-default sampling parameters).
   - **Maximum tokens**: default 1024.
   - **Instructions**: the system prompt. The integration adds the standard
     Home Assistant context (device names, areas, states) on top.
4. **Important**: for device control, expose the entities you want the agent
   to operate: **Settings → Devices & Services → Assist** → *Expose* tab.
   By default no entities are exposed.
5. Select the agent in **Assist** (or per voice satellite / pipeline) and
   test: *"What lights are on in the living room?"* or
   *"Zet de verwarming in de kantoorkamer op 20 graden."*

### Testing from a script

```yaml
action: conversation.process
data:
  agent_id: conversation.mistral_ai
  text: "Zet het licht in de woonkamer aan"
```

## Voice: a complete Mistral Assist pipeline

A Mistral entry also provides **speech-to-text** (Voxtral) and
**text-to-speech** (Voxtral TTS) entities. Combine the three and your whole
Assist pipeline runs on Mistral:

1. Add a Mistral entry (see [Configuration](#configuration)).
2. In the entry's **Configure** dialog, pick the **Speech-to-text model**
   (default `voxtral-mini-latest`) and the **Text-to-speech model**
   (default `voxtral-mini-tts-3-0`).
3. For TTS, paste a **Voice ID** from
   [Mistral Studio](https://studio.mistral.ai): create or pick a voice in
   the Voices section and copy its ID. Without a voice ID the TTS call may
   be rejected by the API.
4. Open **Settings → Assist** (or a pipeline / voice satellite) and set:
   - *Speech-to-text*: the **Speech to text** entity of this integration
     (e.g. `stt.mistral_ai_speech_to_text`)
   - *Conversation agent*: your Mistral agent
   - *Text-to-speech*: the **Text to speech** entity of this integration
     (e.g. `tts.mistral_ai_text_to_speech`)

Voxtral transcription covers Dutch, English, French, German and many more
languages; the STT entity never blocks a pipeline on a language mismatch.
TTS output is MP3. Note: Mistral's content moderation may reject TTS
requests with a 403 — keep spoken responses work-friendly, and check the
logs if speech stays silent.

## Good to know

- **Context size**: the Assist LLM API sends your exposed entity list to the
  model; with hundreds of entities prefer a larger model over the small /
  lite variants.
- **Troubleshooting**: enable debug logging with

  ```yaml
  logger:
    logs:
      custom_components.llm_conversation: debug
  ```

- **Rate limits / billing**: provider errors are surfaced in the conversation
  response and logs; GitHub Models is free with rate limits, the others bill
  per token.

## Development

Repository structure:

```
llm-conversation/
├── AGENTS.md                  ← architecture guide for AI coding agents & contributors
├── LICENSE                    ← MIT
├── hacs.json
├── test_smoke.py              ← offline test suite (no HA install needed)
├── scripts/test_realtime.py   ← live end-to-end test against Mistral + your HA
├── .github/workflows/validate.yaml   ← HACS + hassfest + smoke tests
└── custom_components/llm_conversation/
```

Quick checks from the repository root:

```bash
python3 -m py_compile $(find custom_components -name "*.py")
python3 test_smoke.py
```

The full architecture (canonical message format, provider adapter contract,
the checklist for adding a new provider) is documented in
[AGENTS.md](AGENTS.md) — written so that AI coding agents (Copilot, Claude
Code, Codex, Cursor, …) and human contributors can work on the codebase
consistently.

### Live end-to-end test

`scripts/test_realtime.py` verifies the whole Mistral pipeline in one go —
pure standard library, so it runs on your laptop or in the HA SSH/terminal
add-on. It checks your API key, the chat model, Voxtral TTS (WAV + MP3), a
TTS→STT round-trip, and optionally the live entities in your Home Assistant:

```bash
# Mistral only
python3 scripts/test_realtime.py --api-key "$(cat mistral.key)" --voice-id MIRAv2_XYZ

# Including your live Home Assistant (long-lived token: Profile → Security)
python3 scripts/test_realtime.py --api-key ... --voice-id ... \
  --ha-url http://homeassistant.local:8123 --ha-token ...
```

The HA part exercises the same endpoints the Assist pipeline uses:
`/api/conversation/process` for the agent, `/api/tts_get_url` for the TTS
entity and `/api/stt/{entity}` with raw 16 kHz PCM (resampled from the
TTS WAV in pure Python) for the STT entity. Generated audio files land in
the working directory (`test_tts.wav`, `test_tts.mp3`, `test_ha_tts.mp3`)
so you can listen to the voice before pointing Assist at it.

## Alternatives

The integrations in the default HACS catalogue — e.g. HomeStral and
mistralai-conversation — cover a single provider each. This project was
written from scratch against the current (2026.10) LLM API surface with a
shared multi-provider core.

## License

MIT
