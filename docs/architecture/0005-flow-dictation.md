# ADR 0005 — Flow dictation (hold-to-dictate)

Status: accepted 2026-10-12 (coordinator). Amends `docs/spec/speech.md` for the
desktop dictation path only; the cloud capture path is unchanged.

## Decision

Metis gets a second voice mode next to Capture, modelled on hold-to-dictate
tools: hold a key, speak, cleaned text is inserted into the focused app.

| | Capture (existing) | Dictate (new) |
| --- | --- | --- |
| Output | `CaptureDetail` + proposals | text inserted into the focused app |
| Transport | whole-recording upload, job, poll | PCM chunks over loopback/tunnel, finalize on release |
| Model calls | extraction (budgeted) | none; deterministic cleanup |
| Durable | source + proposals | nothing, unless `flow.keep_history` |

1. **Streaming is provisional, finalize is authoritative.** Chunks are
   buffered in memory; partial decodes show in the HUD only and carry
   `provisional: true`. Nothing creates tasks, reminders or sources from a
   partial. Spec's "no continuous cloud transcription" still holds: audio is
   sent only between key-down and key-up, never before, never idle.
2. **Cleanup is deterministic by default** (`metis_speech/polish.py`): fillers,
   case, terminal punctuation, the user's dictionary, snippets, `scratch that`,
   `new line`/`new paragraph`, per-app style. A local Ollama cleanup pass may be
   explicitly enabled with `METIS_FLOW_MODEL`; it runs only on the final result,
   is labelled as model output, and falls back on timeout/schema/semantic-guard
   failure. Raw ASR and deterministic text are both returned alongside.
3. **Settings are `Preferences.flow`** (`FlowSettings` in contracts): same
   revision/409 rules, no new route. Client sends the settings with session
   create; the server keeps nothing between sessions.
4. **Transport is HTTP chunk posts on the existing warm speech server**
   (`/v1/flow/sessions…`), not WebSocket: stdlib-only, same bearer secret,
   same loopback bind. One decode at a time; a new hold supersedes an abandoned
   session.
5. **No new infrastructure**: the GCP VM runs the same server as a systemd unit
   (`metis-speech`, 127.0.0.1:8787, secret in `/etc/metis-speech.env`, reached
   through the IAP tunnel). Fixture identity rules unchanged.

## Not built yet (deliberately)

- **Desktop insertion.** `apps/desktop` still routes key-up to Capture. Needs:
  a Dictate shortcut, chunk streaming from `audio.rs`, paste-and-restore
  clipboard insertion, and macOS Accessibility permission handling. Owner:
  desktop worker; protocol above is the contract.
- **Command Mode** (rewrite selected text) is not built. The optional local
  cleanup pass can apply explicit spoken self-corrections, but it cannot create
  tasks/reminders and does not change the authoritative capture transcript.
  Any future paid cleanup provider must go through budget reserve/settle (rule
  7); model output is always labelled.
- **Context awareness beyond app name.** Only the focused app's name selects a
  style. Reading surrounding text is a privacy decision, not an implementation
  detail, and is not enabled.
- **Settings UI** in `SettingsScreen.tsx` for dictionary/snippets/app styles.

## Honest-state rules

- A dictation result with `speech.adapter_mode == "fixture"` must be shown as a
  fixture, never inserted silently as if transcribed.
- `no_speech: true` inserts nothing.
- Dictations are not saved unless the user turned on `flow.keep_history`.
