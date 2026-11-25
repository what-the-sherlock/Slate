---
title: API error codes
visibility: public
---
# API error codes

Every error response from the Kestrel API carries a code.

- **KF-1001**: the shipment id is malformed or doesn't exist.
- **KF-2003**: rate limited. Wait for the number of seconds in the Retry-After header, then retry.
- **KF-4040**: no route found between the origin and destination.
- **KF-5002**: the carrier didn't answer in time. Safe to retry with the same idempotency key.

The default API rate limit is 600 requests per minute per API key.
