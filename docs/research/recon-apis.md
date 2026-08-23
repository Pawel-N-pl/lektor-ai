# API reference: OpenRouter + ElevenLabs (verified against live docs, 2026-08-12)

All facts below were read from the pages cited. Nothing is from memory.

---

## 1. OPENROUTER — structured outputs

### 1.1 Endpoint and headers

`POST https://openrouter.ai/api/v1/chat/completions`
Source: https://openrouter.ai/docs/quickstart#using-the-openrouter-api

| Header | Status | Notes |
|---|---|---|
| `Authorization: Bearer <key>` | required | |
| `Content-Type: application/json` | required | |
| `HTTP-Referer` | **required for attribution** | "This header is required for app attribution. Without it, no app page will be created and your usage will not appear in rankings." |
| `X-OpenRouter-Title` | optional | current name of the title header. "`X-Title` is still supported for backwards compatibility." Alone it does nothing — must be paired with `HTTP-Referer`. |
| `X-OpenRouter-Categories` | optional | e.g. `cli-agent` |

Source: https://openrouter.ai/docs/app-attribution
Note for localhost apps: "Apps using `localhost` URLs must also include `X-OpenRouter-Title` to be tracked."

### 1.2 `response_format` shape and `strict`

`strict: true` **is** supported and is an explicit best practice:

> "**Use strict mode**: Set `strict: true` so that providers with a native strict mode enforce your schema exactly. Enforcement varies by provider: some guarantee schema-conforming output, while others translate your schema into their own structured-output format or treat it as a strong hint, so exact compliance is not guaranteed on every endpoint. Strict modes may also restrict which JSON Schema features you can use."

Source: https://openrouter.ai/docs/guides/features/structured-outputs

Field layout: `response_format.type = "json_schema"`, and `response_format.json_schema` carries `name`, `strict`, `schema`. Same URL.

### 1.3 Does `google/gemini-3.6-flash` support it? Yes.

Authoritative, from `GET https://openrouter.ai/api/v1/models` (live, fetched today):

```
"id": "google/gemini-3.6-flash",
"canonical_slug": "google/gemini-3.6-flash-20260721",
"context_length": 1048576,
"supported_parameters": ["include_reasoning","max_tokens","reasoning","reasoning_effort",
                         "response_format","seed","stop","structured_outputs","temperature",
                         "tool_choice","tools","top_p"],
"pricing": {"prompt":"0.0000015","completion":"0.0000075","internal_reasoning":"0.0000075",
            "input_cache_read":"0.00000015","input_cache_write":"0.0000000833333333333333"},
"reasoning": {"mandatory": true, "default_enabled": true,
              "supported_efforts": ["high","medium","low","minimal"], "default_effort": "medium"}
```

Both `response_format` and `structured_outputs` are present, so the model qualifies under the `?supported_parameters=structured_outputs` filter on https://openrouter.ai/models.

**Endpoint-level detail** (`GET https://openrouter.ai/api/v1/models/google/gemini-3.6-flash-20260721/endpoints`) — this matters, because OpenRouter says support "is determined per endpoint, not just per model":

| Endpoint tag | `structured_outputs` | `temperature` / `top_p` | prompt / completion $ per token |
|---|---|---|---|
| `google-vertex/global` | yes | **no** | 0.0000015 / 0.0000075 |
| `google-vertex/global/flex` | yes | **no** | 0.00000075 / 0.00000375 |
| `google-vertex/global/priority` | yes | **no** | 0.0000027 / 0.0000135 |
| `google-ai-studio` | yes | yes | 0.0000015 / 0.0000075 |
| `google-ai-studio/flex` | yes | yes | 0.00000075 / 0.00000375 |
| `google-ai-studio/priority` | yes | yes | 0.0000027 / 0.0000135 |
| `google-vertex/us` | yes | **no** | 0.00000165 / 0.00000825 |

Every endpoint supports structured outputs; only the AI Studio endpoints accept `temperature`.

### 1.4 What happens on a provider that does not support it

Three documented behaviours:

1. **Soft routing preference (default).** "Even when `require_parameters` is `false`, a small set of parameters is used as a soft preference when choosing between providers of the same model: `tools`, `response_format` (including structured outputs), and `verbosity`. If some of a model's providers support one of these parameters and others don't, the request is only routed to the supporting providers. If none of a model's providers support the parameter, the request is still routed to that model and the parameter is ignored." — https://openrouter.ai/docs/guides/routing/provider-selection
2. **Hard guarantee.** `provider.require_parameters: true` — "the request won't even be routed to that provider." Same URL.
3. **Hard failure.** "Model doesn't support structured outputs: The request will fail with an error indicating lack of support." — https://openrouter.ai/docs/guides/features/structured-outputs

There is also an optional **Response Healing** plugin for non-streaming `json_schema` requests, "to reduce the risk of invalid JSON when models return imperfect formatting" (same URL, `/docs/guides/features/plugins/response-healing`).

### 1.5 Copy-pasteable request body for the condense job

```json
{
  "model": "google/gemini-3.6-flash",
  "temperature": 0.3,
  "reasoning": { "effort": "minimal" },
  "provider": { "require_parameters": true },
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "lektor_condensed_cues",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "cues": {
            "type": "array",
            "description": "One entry per cue marked DO SKRÓCENIA, in the same order. Never return KONTEKST cues.",
            "items": {
              "type": "object",
              "properties": {
                "id": {
                  "type": "integer",
                  "description": "The cue id in square brackets in the prompt."
                },
                "text": {
                  "type": "string",
                  "description": "Condensed Polish lektor line. Must not exceed the per-cue character limit given for this id in the prompt.",
                  "maxLength": 400
                }
              },
              "required": ["id", "text"],
              "additionalProperties": false
            }
          }
        },
        "required": ["cues"],
        "additionalProperties": false
      }
    }
  },
  "messages": [
    { "role": "system", "content": "<CONDENSE_SYSTEM>" },
    { "role": "user", "content": "[412] DO SKRÓCENIA (limit 48 znaków): ...\n[413] KONTEKST: ..." }
  ]
}
```

Two caveats on the hard per-cue character limit, both grounded in the OpenRouter text quoted in 1.2:

- The limit is **different for every cue**, so JSON Schema cannot express it. The only schema-level lever is a global `maxLength`, used above as a coarse ceiling.
- `strict: true` does not guarantee `maxLength` is honoured — "Strict modes may also restrict which JSON Schema features you can use" and enforcement "is not guaranteed on every endpoint". Keep the per-cue limit in the prompt **and** validate client-side (the current code already rejects any result longer than the original, which is the right instinct — extend it to reject over-budget results too).

`{"type": "json_object"}` is also valid and is what the current code sends; the docs use it in the `require_parameters` example (https://openrouter.ai/docs/guides/routing/provider-selection). It gives no schema enforcement.

### 1.6 Reading token usage / real spend

> "OpenRouter automatically returns detailed usage information with every response… This information is included in the last SSE message for streaming responses, or in the complete response for non-streaming requests. **No additional parameters are required.**"
>
> "**Deprecated Parameters** — The `usage: { include: true }` and `stream_options: { include_usage: true }` parameters are deprecated and have no effect."

Source: https://openrouter.ai/docs/cookbook/administration/usage-accounting.md

Response shape:

```json
{
  "usage": {
    "prompt_tokens": 194,
    "prompt_tokens_details": { "cached_tokens": 0, "cache_write_tokens": 100, "audio_tokens": 0 },
    "completion_tokens": 2,
    "completion_tokens_details": { "reasoning_tokens": 0 },
    "cost": 0.95,
    "cost_details": { "upstream_inference_cost": 19 },
    "total_tokens": 196
  }
}
```

- `cost` = "The total amount charged to your account" (in credits). This is the number to log — no estimation needed.
- `cost_details.upstream_inference_cost` = actual upstream provider cost.
- `completion_tokens_details.reasoning_tokens` matters here: **reasoning is mandatory on `google/gemini-3.6-flash`** (`"mandatory": true`, `"default_enabled": true`, `"default_effort": "medium"`) and reasoning tokens "are considered output tokens and charged accordingly" at $7.50/M. Send `"reasoning": {"effort": "minimal"}` (~10% of max_tokens) to cut this; `effort: "none"` is rejected — "When `true`, hide disable controls and do not send `effort: "none"` — the model rejects it."
  Sources: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens.md and the live `/api/v1/models` record.
- Alternative: keep the response `id` and query `GET /api/v1/generations` later ("Get a Generation"). Note: "When obtaining usage information via generation ID, the `upstream_inference_cost` field is only available for BYOK requests."

### 1.7 Error codes worth retrying (OpenRouter)

Source: https://openrouter.ai/docs/api_reference/errors-and-debugging.md

| Code | Meaning | Retry? |
|---|---|---|
| 400 | Bad Request (invalid/missing params, CORS) | no |
| 401 | Invalid credentials | no |
| 402 | Insufficient credits | no |
| 403 | Forbidden / guardrail / moderation | no |
| 408 | Request timed out | yes |
| 429 | Rate limited | yes, honour `Retry-After` |
| 502 | Model down or invalid upstream response | yes |
| 503 | No provider meets your routing requirements | yes, honour `Retry-After` |

> "On 429 and 503 responses, OpenRouter may include a standard HTTP `Retry-After` response header indicating how many seconds to wait before retrying… If you're using `fetch` directly, honor it before retrying."

Also important: "The HTTP Response will have the same status code as `error.code`… **Otherwise, the returned HTTP response status will be 200** and any error occurred while the LLM is producing the output will be emitted in the response body." So a 200 can still be an error — check `response.error?.code` / `error.message` before touching `choices`. Provider errors carry a normalized `error.metadata.error_type`; the raw upstream code is in `error.metadata.provider_code` for non-500s.

There is no documented `529` on OpenRouter.

---

## 2. ELEVENLABS TTS

### 2.1 Non-streaming endpoint

```
POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
```
Servers (all valid): `https://api.elevenlabs.io` (default), `api.us.elevenlabs.io`, `api.eu.residency.elevenlabs.io`, `api.in.residency.elevenlabs.io`, `api.sg.residency.elevenlabs.io`.
Source: https://elevenlabs.io/docs/api-reference/text-to-speech/convert (and `.md` variant)

**Headers**: `xi-api-key: <key>`, `Content-Type: application/json`. Response is the raw audio file.

**Query parameters**:

| Param | Type | Default | Notes |
|---|---|---|---|
| `enable_logging` | boolean | `true` | false = zero-retention mode; enterprise only; disables request stitching |
| `optimize_streaming_latency` | integer | null | **deprecated** |
| `output_format` | enum | `mp3_44100_128` | see 2.3 |

**Body**:

| Field | Type | Default | Notes |
|---|---|---|---|
| `text` | string | — | **required** |
| `model_id` | string | `eleven_multilingual_v2` | |
| `language_code` | string (ISO 639-1) | null | "used to enforce a language for the model and text normalization… **This parameter is not supported for multilingual_v2 models.**" So `"pl"` is usable with `eleven_flash_v2_5`, ignored/rejected with `eleven_multilingual_v2`. |
| `voice_settings` | object | null | see 2.2 |
| `pronunciation_dictionary_locators` | list | null | max 3, each `{pronunciation_dictionary_id, version_id}` |
| `seed` | integer | null | 0–4294967295, best-effort determinism |
| `previous_text` / `next_text` | string | null | prosody continuity across chunks |
| `previous_request_ids` / `next_request_ids` | list of string | null | max 3 each; take precedence over `previous_text`/`next_text` |
| `apply_text_normalization` | enum `auto`\|`on`\|`off` | `auto` | |
| `apply_language_text_normalization` | boolean | `false` | "Currently only supported for Japanese", heavy latency cost |
| `use_pvc_as_ivc` | boolean | `false` | **deprecated** |

Copy-pasteable minimal body (matching this project's use):

```json
{
  "text": "Nie mam pojęcia, o czym mówisz, ale brzmi to całkiem rozsądnie.",
  "model_id": "eleven_flash_v2_5",
  "language_code": "pl",
  "apply_text_normalization": "auto",
  "voice_settings": {
    "stability": 0.6,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": false,
    "speed": 1.15
  }
}
```
with `?output_format=mp3_44100_128`.

### 2.2 `voice_settings.speed` — exact range and semantics

Field spec (https://elevenlabs.io/docs/api-reference/text-to-speech/convert.md and `https://api.elevenlabs.io/openapi.json`, schema `VoiceSettingsResponseModel`):

> `speed` (double, optional, nullable, default: 1) — "Adjusts the speed of the voice. A value of 1.0 is the default speed, while values less than 1.0 slow down the speech, and values greater than 1.0 speed it up."

The OpenAPI spec declares **no** numeric `minimum`/`maximum` for `speed` (unlike `stability` and `similarity_boost`, which are bounded 0.0–1.0). The bounds are documented in prose, in two places, identically:

> "The default value is 1.0… Values below 1.0 will slow the voice down, **to a minimum of 0.7**. Values above 1.0 will speed up the voice, **to a maximum of 1.2**. Extreme values may affect the quality of the generated speech."
> — https://elevenlabs.io/docs/overview/capabilities/text-to-speech/best-practices (section "Pace")

> "Possible values range from **0.7 to 1.2**… This setting is available for all voices and all models."
> — https://elevenlabs.io/docs/help-center/product/speech-synthesis/text-to-speech/can-i-change-the-pace-of-the-voice

**Authoritative range: 0.7 … 1.2, default 1.0.** For `eleven_v3` specifically, "Speed is also controlled through audio tags" (best-practices page, "Audio tags").

### 2.3 `output_format` — every valid value and the plan gates

Complete enum, verbatim from https://elevenlabs.io/docs/api-reference/text-to-speech/convert.md:

```
alaw_8000
mp3_22050_32   mp3_24000_48   mp3_44100_32   mp3_44100_64
mp3_44100_96   mp3_44100_128  mp3_44100_192
opus_48000_32  opus_48000_64  opus_48000_96  opus_48000_128  opus_48000_192
pcm_8000  pcm_16000  pcm_22050  pcm_24000  pcm_32000  pcm_44100  pcm_48000
ulaw_8000
wav_8000  wav_16000  wav_22050  wav_24000  wav_32000  wav_44100  wav_48000
```

Naming: `codec_sampleRate_bitrate`. Plan gating, quoted verbatim from the same page:

> "MP3 with 192kbps bitrate requires you to be subscribed to **Creator tier or above**. PCM and WAV formats with 44.1kHz sample rate requires you to be subscribed to **Pro tier or above**."

Everything else (including the default `mp3_44100_128`, all Opus, `pcm_48000`, `wav_48000`, µ-law/A-law) carries no documented tier requirement. µ-law "is commonly used for Twilio audio inputs."

### 2.4 With-timestamps variant — yes, it exists

```
POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps
```
> "Generate speech from text with **precise character-level timing information** for audio-text synchronization."

Source: https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps

Identical query params and identical request body to the plain `convert` endpoint (same `voice_settings`, `model_id`, `language_code`, etc.). The difference is the response: **JSON, not a binary file**.

```json
{
  "audio_base64": "base64_encoded_audio_string",
  "alignment": {
    "characters": ["H", "e", "l", "l", "o"],
    "character_start_times_seconds": [0, 0.1, 0.2, 0.3, 0.4],
    "character_end_times_seconds":   [0.1, 0.2, 0.3, 0.4, 0.5]
  },
  "normalized_alignment": {
    "characters": ["H", "e", "l", "l", "o"],
    "character_start_times_seconds": [0, 0.1, 0.2, 0.3, 0.4],
    "character_end_times_seconds":   [0.1, 0.2, 0.3, 0.4, 0.5]
  }
}
```

Fields: `audio_base64` (string, required), `alignment` (object, optional/nullable) = "Timestamp information for each character in **the original text**", `normalized_alignment` = "for each character in **the normalized text**". Each alignment object has three required parallel arrays: `characters`, `character_start_times_seconds`, `character_end_times_seconds`.

A streaming sibling exists: `POST /v1/text-to-speech/{voice_id}/stream/with-timestamps` (https://elevenlabs.io/docs/api-reference/text-to-speech/stream-with-timestamps).

**Cost**: no surcharge is documented anywhere. TTS billing is per character of input text ("Text to Speech 1 credit per character", https://elevenlabs.io/pricing), and neither the endpoint reference nor the pricing page lists a separate line item for timestamps. Treat this as "no documented extra cost" rather than a positive guarantee — the docs are silent, not affirmative. The real overhead is transport: base64 inflates the audio payload ~33%.

Relevance to this project: this endpoint is exactly the tool for the ducking envelope. Instead of deriving spans from `len(audio)/sr` per clip, `alignment.character_end_times_seconds[-1]` gives the true speech end, and interior gaps let the duck envelope follow actual speech rather than a whole-clip rectangle.

### 2.5 Error codes worth retrying (ElevenLabs)

Errors are `{"detail": {"type", "code", "message", "status", "request_id", "param"}}`.
Source: https://elevenlabs.io/docs/eleven-api/resources/errors

| `type` | HTTP | Retry? |
|---|---|---|
| `validation_error` | 400 | no |
| `invalid_request` | 400 | no |
| `authentication_error` | 401 | no |
| `payment_required` | 402 | no (out of credits) |
| `authorization_error` | 403 | no |
| `not_found` | 404 | no |
| `conflict` | 409 | no |
| `rate_limit_error` | 429 | **yes** |
| `internal_error` | 500 | yes |
| `service_unavailable` | 503 | yes |

429 splits into two distinct codes with different correct responses:

> "The error `code` will be `rate_limit_exceeded` or `concurrent_limit_exceeded` respectively. In the case of rate limiting, you should implement **exponential backoff**… In the case of concurrency, you should **wait for the current requests to complete** before making new ones."

The help-center page names the same two conditions as `too_many_concurrent_requests` and `system_busy` ("Generally, if you retry the request, it will succeed"): https://elevenlabs.io/docs/help-center/technical/api-error-code-429

Useful for a live-tuned pool: **"The response headers include `current-concurrent-requests` and `maximum-concurrent-requests` which you can use to monitor your concurrency."** — https://elevenlabs.io/docs/overview/models#concurrency-and-priority

Concurrency limits, verbatim from that section (matches this project's config comment):

| Plan | Multilingual v2 | Flash | Priority |
|---|---|---|---|
| Free | 2 | 4 | 3 |
| Starter | 3 | 6 | 4 |
| Creator | 5 | 10 | 5 |
| Pro | 10 | 20 | 5 |
| Scale | 15 | 30 | 5 |
| Business | 15 | 30 | 5 |
| Enterprise | Elevated | Elevated | 6 |

Always log `detail.request_id` — support asks for it.

### 2.6 Current model line-up, exact `model_id` strings, Polish support

Source: https://elevenlabs.io/docs/overview/models

| `model_id` | What it is | Polish? | Max input chars |
|---|---|---|---|
| `eleven_v3` | flagship, most expressive, 70+ languages | **yes** — "Polish (pol)" listed | 5,000 |
| `eleven_multilingual_v2` | most lifelike, 29 languages | **yes** — `pl` in the language list | 10,000 |
| `eleven_flash_v2_5` | ultra-fast (~75 ms), 32 languages | **yes** — all v2 languages + `hu`, `no`, `vi` | 40,000 |
| `eleven_flash_v2` | ultra-fast | **no** — `en` only | 30,000 |
| `eleven_ttv_v3` | Text to Voice (voice design) | yes (70+) | n/a |
| `eleven_multilingual_ttv_v2` | Text to Voice | yes | n/a |
| `eleven_multilingual_sts_v2` | Speech to Speech (voice changer) | yes | 10 min audio |
| `eleven_english_sts_v2` | Speech to Speech, English only | no | 5 min audio |
| `scribe_v2`, `scribe_v2_realtime` | STT, 90+ languages | yes | n/a |
| `eleven_text_to_sound_v2`, `music_v1`, `music_v2` | not TTS | — | — |

Deprecated, still functional: `eleven_turbo_v2_5` (32 languages incl. `pl`, → use `eleven_flash_v2_5`), `eleven_turbo_v2` (`en` only), `scribe_v1`.
Char limits from https://elevenlabs.io/docs/help-center/product/speech-synthesis/text-to-speech/whats-the-maximum-amount-of-characters-and-text-i-can-generate

Flash v2.5 caveat that bears directly on subtitle text:

> "When using Flash v2.5, numbers aren't normalized by default in a way you might expect… By default, normalization is disabled for Flash v2.5 to maintain the low latency. However, **Enterprise customers** can now enable text normalization for v2.5 models by setting the `apply_text_normalization` parameter to 'on'… The Multilingual v2 model does a better job of normalizing numbers." — https://elevenlabs.io/docs/overview/models#considerations

Model-choice guidance: https://elevenlabs.io/docs/eleven-api/choosing-the-right-model — quality → `eleven_v3`; balanced/multilingual → `eleven_flash_v2_5` ("the best all-round choice").

---

## 3. Contradictions in `wsl/lektor.py`

Ordered roughly by how much damage each one does.

**3.1 `speed` can exceed the documented maximum. — CONFIRMED, will break generations.**
`lektor.config.json` sets `"timing": {"max_speed": 1.3}`, and `generate_clips` (line 514) does `speed = min(t["max_speed"], max(1.0, predicted / window_s))`, then `TTS.get` puts it into `voice_settings["speed"]`. The documented range is **0.7 – 1.2**. Any cue whose predicted duration overruns its window by more than 20% will be sent with `speed` in (1.2, 1.3]. Fix: clamp to 1.2 in code (not just config), and lower `max_speed` to 1.2. The existing `max_atempo: 1.25` fallback already covers the rest locally, at no credit cost.

**3.2 `PLAN_RATES` is wrong (line 732).**
```python
PLAN_RATES = {"creator": 0.30, "pro": 0.24, "scale": 0.18, "business": 0.12}
```
Real, per `_bmad-output/api-prices.md`: flat $0.05 per 1000 chars for Flash/Turbo and $0.10 per 1000 chars for Multilingual v2 / v3, **identical on every plan** — there is no per-plan rate ladder at all. The `--plan` argument is therefore meaningless for price and should only select the *included characters* allowance. The `factor = 0.5 if flash else 1.0` on line 740 is directionally right (Flash is "50% lower price per character for API generations", https://elevenlabs.io/docs/overview/models), but `flash = "flash" in cfg["voice"]["model_id"]` misses `eleven_turbo_v2_5` and `eleven_turbo_v2`, which bill at the same $0.05 rate.

**3.3 4xx errors are retried five times. — CONFIRMED bug, both API clients.**
`TTS.get` (lines 390–405) and `condense_batch` (lines 241–259) both call `r.raise_for_status()` inside a `try` whose `except Exception` catches everything and sleeps into the next attempt. A 401 (bad key), 402 (out of credits), 400 or 422 (bad `speed`, see 3.1) therefore burns 5 attempts with up to ~45 s of sleep each before surfacing. Per the ElevenLabs error table, only `rate_limit_error` (429), `internal_error` (500) and `service_unavailable` (503) are retryable; per OpenRouter, only 408/429/502/503. Split the exception handling so `HTTPError` on a non-retryable status fails fast, and surface `detail.request_id` / `error.message`.

**3.4 OpenRouter retry set includes a code that does not exist; `Retry-After` is ignored.**
Line 249: `if r.status_code in (429, 500, 502, 503, 529)`. OpenRouter documents 400/401/402/403/408/429/502/503 — there is no 529 (that is Anthropic's direct-API overload code). Missing: **408** (request timed out), which is retryable. And OpenRouter explicitly sends `Retry-After` on 429 and 503 ("honor it before retrying"); the code uses blind `2**attempt` backoff instead.

**3.5 Errors arriving as HTTP 200 are not detected.**
Line 254: `content = r.json()["choices"][0]["message"]["content"]`. OpenRouter: "the returned HTTP response status will be 200 and any error occurred while the LLM is producing the output will be emitted in the response body." The code will raise `KeyError: 'choices'`, be swallowed by the catch-all, and retry blind — losing `error.code` and `error.message` entirely.

**3.6 Attribution headers are incomplete.**
Lines 234–238 send only `X-Title: lektor-ai`. `X-Title` is legacy ("still supported for backwards compatibility"), and `HTTP-Referer` is the required header — "Setting only `X-OpenRouter-Title` without a URL will not create an app entry." Either send both (`HTTP-Referer` + `X-OpenRouter-Title`) or drop both; the current half-measure does nothing.

**3.7 `json_object` where `json_schema` is available, with no schema validation on the way back.**
Line 228 uses `{"type": "json_object"}`. `google/gemini-3.6-flash` advertises `structured_outputs` on **every** endpoint (§1.3), so the full `json_schema` + `strict: true` body in §1.5 is available and would eliminate the `data["cues"]` / `item["id"]` / `item["text"]` KeyError paths on line 256. Add `provider: {"require_parameters": true}` if you want a hard guarantee rather than the soft routing preference.

**3.8 `temperature: 0.3` is silently dropped on four of the seven endpoints.**
`temperature` appears in `supported_parameters` for `google-ai-studio*` only; the four `google-vertex/*` endpoints do not list it, and default routing "can still receive the request, but will ignore unknown parameters." If deterministic-ish condensation matters, pin with `provider: {"only": ["google-ai-studio"]}` or `require_parameters: true` — otherwise delete the field and stop pretending it does something.

**3.9 The LLM cost estimate ignores mandatory reasoning tokens (lines 765–767).**
```python
llm_chars = total * 1.4
${llm_chars/4/1e6*1.5 + llm_chars/4/1e6*0.3*7.5}
```
`google/gemini-3.6-flash` has `reasoning.mandatory: true`, `default_enabled: true`, `default_effort: "medium"` (~50% of max_tokens), and reasoning tokens bill as **output** at $7.50/M. The estimate assumes output = 30% of input and no reasoning at all, so it will undercount, potentially by a lot. Two fixes: send `"reasoning": {"effort": "minimal"}`, and log the real number from `response["usage"]["cost"]` / `usage["completion_tokens_details"]["reasoning_tokens"]` rather than estimating (usage is returned automatically now — do **not** send the deprecated `usage: {include: true}`).

**3.10 Cache files are hardcoded `.mp3` regardless of `output_format` (line 373).**
`_path()` returns `…/{key}.mp3`, but `voice.output_format` is configurable across 27 values. Set it to `pcm_44100` or `wav_48000` and the cache fills with mislabelled files; `decode()`'s `ffmpeg -i pipe:0` will then fail on headerless PCM. Either derive the extension from `output_format` or document that only the mp3_* family is supported. Related plan gates to enforce or at least warn about: `mp3_44100_192` needs Creator+, PCM/WAV at 44.1 kHz needs Pro+.

**3.11 `language_code` is never sent.**
The config pins `eleven_flash_v2_5`, for which `language_code: "pl"` is valid and "enforce[s] a language for the model and text normalization". Given a Polish script read by a possibly-English-trained voice, this is free insurance. It must **not** be sent when `model_id` is `eleven_multilingual_v2` ("not supported for multilingual_v2 models"), so it has to be conditional on the model.

**3.12 429 is handled identically for both sub-cases.**
Line 393 treats every 429 as "sleep and retry". `concurrent_limit_exceeded` / `too_many_concurrent_requests` means the `ThreadPoolExecutor(max_workers=concurrency)` is oversubscribed and should shrink, not spin. The response headers `current-concurrent-requests` / `maximum-concurrent-requests` are available on every response and would let the pool self-tune instead of being hand-set to 3.

**3.13 `stage_sample` defaults omit the current flagship (line 823).**
Defaults are `eleven_flash_v2_5` and `eleven_multilingual_v2`. `eleven_v3` is the current flagship TTS model, supports Polish, and bills at the same $0.10/1000 chars as multilingual v2 — worth being in the A/B list. Note its 5,000-char input cap and that its speed control is via audio tags, not `voice_settings.speed`.

**3.14 Not a bug, an unexploited capability.**
The pipeline currently infers where the lektor speaks from decoded clip length (`assemble_voice` → `spans` → `build_duck_envelope`). `/v1/text-to-speech/{voice_id}/with-timestamps` returns per-character start/end times in seconds for the same request body at no documented extra cost. That is a strictly better source for both clip placement and the ducking envelope, and it would let the envelope follow real speech instead of treating each clip as one solid rectangle.