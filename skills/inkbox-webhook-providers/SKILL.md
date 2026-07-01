---
name: inkbox-webhook-providers
description: Use when adding support for verifying a new inbound-webhook source (a third-party service that will POST signed events to this agent's /webhook endpoint), or when asked how the plugin decides whether an incoming webhook is authentic. Explains the webhook_providers registry and how to onboard a new provider.
user-invocable: false
---

# Adding a webhook provider

Every request that reaches the plugin's `/webhook` endpoint is signed by
whoever sent it, but each source signs differently — a different header, a
different signed payload, and a different algorithm — so there is no single
signature to check. The plugin handles this with a small registry in the
`webhook_providers/` package: each source is a `WebhookProvider` in **its own
module** that knows how to recognise its own requests and verify their
signature. The package auto-imports every module in it at startup, so adding a
source is drop-in: **create one file, no central list to edit.**

## How verification is decided

`adapter._handle_webhook` **authenticates first, then routes on the verified
source** — never on the payload's claimed `event_type`. On each inbound webhook:

1. `match_provider(headers)` returns the first registered provider whose
   signature header is present, or `None`.
2. If a provider matched, it must `verify()` (unless `INKBOX_REQUIRE_SIGNATURE`
   is off); a present-but-invalid signature is rejected `401`.
3. The **verified source** decides routing:
   - `source == "inkbox"` → dispatched to the mail/text/iMessage/call handler by
     payload shape.
   - a verified **third-party** source → handed to the agent as an external event.
   - **no provider matched** (unknown source) → external pass-through **only**
     when `INKBOX_EXTERNAL_EVENTS_ENABLED` is true (off by default); otherwise
     dropped with `200 ignored`.

Because routing keys off *who signed it*, a forged payload can't impersonate an
Inkbox event — an unsigned body claiming `message.received` is just an unknown
source, never the mail handler. Onboarding a provider moves a source from
"unverified pass-through" to "cryptographically verified" (and, once verified,
its events reach the agent regardless of the pass-through flag).

## Steps to onboard a source

1. **Drop a new file** `webhook_providers/<name>.py` with a `WebhookProvider`
   subclass decorated with `@register_provider`. That's the whole registration
   step — the package auto-imports it at startup, no other file changes:

   ```python
   # webhook_providers/github.py
   import hashlib
   import hmac

   from .base import WebhookProvider, register_provider


   @register_provider
   class GithubProvider(WebhookProvider):
       name = "github"                       # surfaced to the agent as source=github
       provider_header = "X-Hub-Signature-256"

       def verify(self, *, body, headers, url, secret):
           sent = ""
           for k, v in headers.items():      # header names are case-insensitive
               if k.lower() == "x-hub-signature-256":
                   sent = v
                   break
           if not sent.startswith("sha256="):
               return False
           expected = hmac.new(
               secret.encode(), body, hashlib.sha256
           ).hexdigest()
           return hmac.compare_digest(expected, sent.removeprefix("sha256="))
   ```

2. **Provide the secret.** `adapter._provider_secret(name)` resolves it: Inkbox
   uses the configured signing key; every other provider reads
   `INKBOX_WEBHOOK_SECRET_<NAME>` from the environment (e.g.
   `INKBOX_WEBHOOK_SECRET_GITHUB`). An empty/unset secret fails verification
   closed.

3. **Point the source at this agent.** Register the agent's `/webhook` URL with
   that service and set its secret to the same value.

4. **Test it** — add a case to `tests/test_webhook_providers.py` covering a
   valid and an invalid signature.

## Getting `verify` right (the common mistakes)

- **Sign the raw body, not a re-serialized copy.** `body` is the exact bytes
  received; parsing and re-dumping JSON changes whitespace and breaks the HMAC.
- **Some schemes sign the URL + params, not the body.** That is why `verify`
  receives `url` as well as `body`; use whichever the source signs.
- **Match the algorithm.** Not everything is HMAC-SHA256 — some use SHA-1, and
  some use public-key signatures (a public key, not a shared secret).
- **Always use a constant-time compare** (`hmac.compare_digest`) and **fail
  closed** (return `False`) on any missing header, bad prefix, or missing
  secret.
- **`provider_header` must be unique.** If a source needs more than one header
  to identify it, override `matches(headers)` instead of setting
  `provider_header`.
