# Changelog

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
