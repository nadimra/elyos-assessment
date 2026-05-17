# API Notes

Observations and quirks discovered while integrating the weather and research APIs.

Source data: `probes/probe_weather.py` (49 calls, broad survey). Raw
results in the matching `probe_weather_results.json` file.

## `/weather`

### Happy-path response

```
GET /weather?location=London
X-API-Key: <key>

200 OK
{"location": "London", "temperature_c": 9.2, "condition": "Patchy rain nearby", "humidity": 76}
```

Latency ~100–200 ms warm, ~1.3 s cold (Cloud Run). The `temperature_c`
reading drifts slightly across calls (e.g. 9.2 → 9.4), so the
response isn't perfectly cacheable.

### Critical quirks

1. **Rate-limit is hidden in a 200 body, not a 429.** When throttled
   (observed at ~5 requests per 30 s window), the response is:

   ```
   200 OK
   {"status": "throttled", "message": "Rate limit exceeded. Please wait.",
    "retry_after_seconds": 29, "data": null}
   ```

   `status_code == 200` is **not** sufficient to call this a success.
   Always inspect the body for `status == "throttled"` / `data == null`.

2. **Two happy-path response shapes, returned randomly.** 
   ```
   {"location": "Cambridge",
    "conditions": [
      {"temperature_c": 10.2, "condition": "Light rain",   "humidity": 82},
      {"temperature_c":  9.2, "condition": "light rain",   "humidity": 95}
    ],
    "note": "Multiple conditions reported"}
   ```

   The shape is **not a property of the query** — the same query (e.g.
   `Cambridge`, `London, UK`) returned multi on one probe run and
   single on the next, and vice-versa.

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

   So the echoed `location` field cannot be trusted to match what the
   user asked for.

5. **Location params can be ambiguous.** Plain `Cambridge` / `Boston` / `Springfield`
   silently picks one of multiple real-world namesakes without telling
   the caller.

6. **Repeated / comma-joined / newline-injected inputs: last token
   wins.** `?location=London&location=Tokyo` → Tokyo;
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
- **Cap user input length** before forwarding to the API. 1000-char
  blobs returning 404 with the input echoed is a waste of a request and
  a potential injection sink.
- **Don't trust the echoed `location`.** If the user typed something
  that doesn't look like a place, surface the API's interpretation back
  to them so the LLM can ask "did you mean X?".
- **Treat each call as independent.** No caching by (query → shape);
  the same query can return either shape on consecutive calls.

## `/research`


### Happy-path response


### Critical quirks


### Implications for the chat app


## General
