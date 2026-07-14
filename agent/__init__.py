"""Service 2 — Agent. Turns machine alerts (anomaly / drift) into natural-language
incident cards with a likely cause and a forecast outlook. Uses Gemini when a key
is present, otherwise a deterministic template so the demo always produces a card."""
