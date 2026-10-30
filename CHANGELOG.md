# Changelog

Newest version first.

## 2.2.2

### Added
- **Automated setup script** (`scripts/setup_assist.py`): does everything
  after the HACS install, driven entirely by the HA API. It creates the
  Mistral config entry (or reuses the existing one, also when renamed),
  saves the options (Assist device control, Voxtral STT/TTS models, voice
  ID) while keeping every value you didn't pass, waits until the
  conversation/stt/tts entities are available, and creates a dedicated
  **Mistral** Assist pipeline that becomes the preferred one. Other
  pipelines are left untouched. Pure standard library, so it runs on your
  laptop or in the HA SSH/terminal add-on. Secrets come from the
  environment or a hidden prompt, never from command-line arguments, and
  are never printed (also not in error messages).
- `test_setup_mock.py`: offline test for the setup script against a mock
  Home Assistant that mirrors the 2026.10.4 API responses (API status,
  config/options flow, entity registry, assist pipeline websocket
  commands). Runs in CI.
- README: "Automated setup" section with bash and Windows PowerShell
  examples (`py` instead of `python3`).

### Fixed
- `scripts/test_realtime.py`: the "HA connectivity" check expected plain
  text from `GET /api/`, but Home Assistant answers JSON
  `{"message": "API running."}`, so it always failed against a working
  Home Assistant. Now covered by `test_setup_mock.py`.

### Changed
- **Minimum Home Assistant version raised to 2026.10.0** (`hacs.json`),
  matching the LLM API surface the integration is built and verified
  against (2026.10.4).
- CHANGELOG ordered newest first.
- Version bumped to 2.2.2.

## 2.2.1

### Added
- Brand assets (`brand/icon.png` 256×256, `icon@2x.png` 512×512) so HACS
  validation passes without a Home Assistant brands-repo entry.

### Changed
- AGENTS.md updated for the 2.2.0 changes (usage chunks, truncation,
  reauth, network-error handling, Anthropic caching, Gemini ids, single
  client registry).

## 2.2.0

### Fixed
- **Setup no longer gets stuck after a power cut.** Network errors while
  Home Assistant starts (internet not up yet) now raise
  `ConfigEntryNotReady`, so Home Assistant retries instead of leaving the
  entry in a permanent setup error. Network errors during a conversation
  give a clear "Error connecting to the provider" instead of a crash.
- **Config flow error messages.** A network problem showed "Invalid
  authentication"; it now shows "Failed to connect". Only a 401/403 maps
  to `invalid_auth`.
- **Truncated answers are reported.** When the model hits *Maximum tokens*
  the agent now raises a clear error instead of returning a silently cut-off
  answer (as 1.x did).
- **Gemini tool-call ids** are now unique per round; every round started at
  `call_0`, which could mismatch tool results in multi-step requests.
- **Empty transcriptions** (silence/noise) return an STT error instead of
  sending an empty sentence to the agent.
- **Smoke tests in CI.** `test_smoke.py` loaded the component from a
  hard-coded `/home/user/...` path, so the GitHub workflow could never pass.
  It now resolves the path relative to the test file.
- **Realtime test defaults.** Default entity IDs now match what Home
  Assistant actually creates for a "Mistral AI" entry
  (`conversation.mistral_ai`, `stt.mistral_ai_speech_to_text`,
  `tts.mistral_ai_text_to_speech`); README updated to match.

### Added
- **Token usage in conversation traces** for all providers, so cost per
  question is visible in the Assist debug view.
- **Anthropic prompt caching**: tools and the system prompt (Home Assistant
  instructions + exposed entities) are marked cacheable. These are resent on
  every tool-call round; cache reads cost a fraction of normal input tokens.
- **Re-authentication prompt** when the provider rejects the API key during
  a conversation.
- Smoke tests 12-16 covering the above.

### Changed
- One client map (`CLIENT_CLASSES`) shared by setup and config flow;
  platform list helper; removed an unused import.
- Realtime script: secrets via environment variables recommended (command
  line arguments end up in shell history); fetching the TTS audio no longer
  crashes the run on a network error.
