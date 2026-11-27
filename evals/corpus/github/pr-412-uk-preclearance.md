---
title: "Pull request #412: Add UK customs pre-clearance"
visibility: private
kind: github
author: mei-chen
date: 2026-11-24T13:20:00Z
---
# Pull request #412: Add UK customs pre-clearance

Status: merged · Opened by mei-chen on 2026-11-10 · Labels: customs, feature

Adds pre-clearance submissions for shipments into the United Kingdom through the HMRC CDS API. Declarations are sent 24 hours before arrival. Switzerland follows in a separate PR.

## Comment by arjun-rao on 2026-11-23

Looks good. Please add a retry for CDS timeouts before merging.

## Comment by mei-chen on 2026-11-24

Added retries with backoff. Merging.
