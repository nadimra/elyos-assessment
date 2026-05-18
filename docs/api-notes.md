# API Notes

Observations and quirks discovered while integrating the weather and research APIs.

Source data: `probes/probe_weather.py` and
`probes/probe_research.py`, each broad surveys. Raw results
in the matching `probe_*_results.json` files alongside.

## `/weather`

### Happy-path response

```
GET /weather?location=London
X-API-Key: <key>

200 OK
{"location": "London", "temperature_c": 9.2, "condition": "Patchy rain nearby", "humidity": 76}
```

### Critical quirks

1. **Rate-limit is hidden in a 200 body, not a 429.** When throttled, the response is:

   ```
   200 OK
   {"status": "throttled", "message": "Rate limit exceeded. Please wait.",
    "retry_after_seconds": 29, "data": null}
   ```

   `status_code == 200` is **not** sufficient to call this a success.
   Always inspect the body for `status == "throttled"` / `data == null`.

2. **Multiple readings seem to appear randomly with a different response shape.** 
   ```
   {"location": "Cambridge",
    "conditions": [
      {"temperature_c": 10.2, "condition": "Light rain",   "humidity": 82},
      {"temperature_c":  9.2, "condition": "light rain",   "humidity": 95}
    ],
    "note": "Multiple conditions reported"}
   ```

3. **Three different error body shapes.** Callers need to recognise all
   three:

   | Status | Body shape |
   |---|---|
   | 401, 404 | `{"error": "<message>"}` |
   | 422 | `{"detail": [{"type", "loc", "msg", "input"}]}` (FastAPI/Pydantic) |
   | 405 | `{"detail": "Method Not Allowed"}` (string) |

4. **The geocoder is far more permissive than "city name".** It accepts
   and silently resolves:

   - Zip codes (`12345` → Schenectady)
   - Coordinates (`51.5074,-0.1278` → "Strand" neighborhood, *not* "London")
   - Postcodes (`SW1A 1AA` → London)
   - Airport codes (`LHR` → "London Heathrow Airport")
   - Country names (`France` → Paris — silently picks the capital)
   - Continent names (`Europe` → a town literally called "Europe")
   - Fuzzy substrings of garbage (`London'; DROP TABLE cities;--` → "Tiar Drop";
     `<script>alert(1)</script>` → "Alert")
   - Made-up places that happen to match real ones (`Atlantis`, `Hogwarts`)

5. **Location params can be ambiguous.** Plain `Cambridge` / `Boston` / `Springfield`
   silently picks one of multiple real-world namesakes without telling
   the caller.

6. **Repeated / comma-joined / newline-injected inputs last token wins.** `?location=London&location=Tokyo` → Tokyo;
   `?location=London,Tokyo` → Tokyo; `?location=London\nTokyo` → Tokyo.

7. **Case + leading/trailing whitespace are normalised.** `LONDON`,
   `london`, `  London  ` all return the same canonical London response.

8. **Errors echo the user input verbatim.** A 1000-char garbage input
   came back in full inside `error`. Anything we render into a UI must
   be escaped — potential XSS sink.

9. **Unicode handling is inconsistent.** `東京` works (returns
   `東京都`), `São Paulo` works (returned as ASCII `Sao Paulo`),
   `Москва` works, but `Київ` and `Athina` (transliterated) returned
   404. The geocoder doesn't normalise reliably across scripts.

10. **Auth failures are fast and don't consume rate-limit budget.**
    Missing or invalid `X-API-Key` returns 401 in ~15 ms — handy for
    failing fast on config errors.

### Implications for the chat app

- **Throttle detection** in `get_weather`: check `body.get("status") == "throttled"`, return a structured `{"error": "rate limited", "retry_after_seconds": N}` so the LLM can apologize meaningfully.
- **Don't pre-parse the response shape**: keep `json.dumps(result)` as-is and let the LLM handle flat/array/note variance.
- **Clarify `location`.** If the user typed something
  that doesn't look like a place, or if the locations is ambiguous, surface the API's interpretation back
  to them so the LLM can ask "did you mean X?".
- **Allow LLM to accept different location inputs** The location string is more broad than just a simple city. We can accept other types of inputs such as coordinates.
- **Ensure that LLM mentions whenever 2 readings are shown** The LLM shouldn't average out the 2 readings, instead just highlight that 2 readings were reported to the user.

## `/research`

Findings come from `probes/probe_research.py` plus ad-hoc curls. Each call is 3-15s, so the probe is intentionally slow.

### Happy-path response
- Status `200`, content-type `application/json`, `content-length` always set (no chunked encoding — full blob delivery).
- Flat envelope:
  ```
  {"topic":"solar energy","summary":"Research summary for 'solar energy'. This analysis covers key aspects and recent developments in the field.","sources":["nature.com","sciencedirect.com","arxiv.org"],"generated_at":"2026-05-16T20:51:15+00:00"}
  ```

### Critical quirks

1. **The "research" is templated boilerplate, not real content.** The `summary` is a fixed string with `{topic}` substituted — identical across `solar energy`, `ancient rome`, `knitting`, `quantum computing`, nonsense strings, empty string, SQL/HTML injection, etc. No topic-specific data is ever returned.

2. **`sources` is fake / fixed.** Always the same three URLs (`nature.com`, `sciencedirect.com`, `arxiv.org`) regardless of topic. Strip them before forwarding to the LLM — they would mislead the user if surfaced as citations.

3. **Four response shapes — random per call, all HTTP 200.** Same exact topic call can return any of:

   | Variant | Body shape | Observed frequency |
   |---|---|---|
   | **Fresh** | `{"topic":..., "summary":"Research summary for...", "sources":[...], "generated_at":<now>}` | Most calls |
   | **Stale-cache** | `..., "cached": true, "generated_at": "2024-03-15T09:00:00Z", "cache_age_seconds": 26784000` | ~1 in 5-10 |
   | **Empty** | `{}` — totally empty object | Intermittent |
   | **Throttled** | `{"status":"throttled","message":"...","retry_after_seconds":N,"data":null}` | When budget exceeded |

   Implication: `research_topic` must check all four. Pass `cached:true` through to the LLM (instruct it to mention staleness); treat empty `{}` as an error so the LLM can apologize/retry.

4. **Latency exceeds the documented 3-8s band.** Observed 3.3s typical, 7.9s p95 in the burst test, **15.1s** for one empty-variant call. Set the httpx timeout to 30s — a 10s client timeout would cancel valid responses.

5. **No input validation on `topic`.** Empty strings, 1000-char strings, newlines, and SQL/HTML injection strings are all accepted.

6. **Rate-limit is hidden in a 200 body, not a 429.** Same as the weather endpoint, we get rate limit errors hidden as 200.

7. **Blob delivery only.** no streaming. Whatever pending UX the chat app shows must carry the entire wait.

8. **Different error shapes.** Three error shapes plus the 200-throttle envelope — any single-shape error handler will choke:

    | Code | Body |
    |---|---|
    | 200 (throttled) | `{"status":"throttled","data":null,...}` |
    | 401 | `{"error":"Invalid or missing API key"}` |
    | 405 | `{"detail":"Method Not Allowed"}` *(string)* |
    | 422 | `{"detail":[{"type":"missing","loc":[...],...}]}` *(list of dicts)* |

### Implications for the chat app

1. **Throttle handling**: reuse the `get_weather` envelope check verbatim — same shape, same return-as-error pattern.
2. **Empty-`{}` handling**: explicit check `if isinstance(body, dict) and not body` → return `{"error": "empty response"}`. Without this, the LLM sees `{}` as a successful tool result and confabulates.
3. **Strip `sources` before forwarding** to the LLM. Decorative, misleading, never topic-specific.
4. **Tool description must** (a) flag that summaries are brief/generic, (b) instruct the LLM to mention `cached:true` to the user, (c) ask for clarification on ambiguous topics (e.g. "football") before calling — each call is 3-15s and rate-limited, don't waste one.

## General

- **Rate-limit budget is shared across `/weather` and `/research`.** Confirmed after alternating between the calls.
- Throttle envelope is identical on both endpoints (`{"status":"throttled","retry_after_seconds":N}` at HTTP 200). `retry_after_seconds` varies dynamically (1-2s observed on /research, ~30s on /weather) — honor the body value, don't hardcode.
