---
title: "Issue #405: Webhook retries stop after 3 attempts"
visibility: private
kind: github
author: arjun-rao
date: 2026-10-20T16:00:00Z
---
# Issue #405: Webhook retries stop after 3 attempts

Status: closed · Opened by arjun-rao on 2026-10-14 · Labels: bug, webhooks

Webhook deliveries give up after 3 failed attempts within a minute, so a short customer outage loses events.

## Comment by arjun-rao on 2026-10-20

Fixed: deliveries now retry up to 8 times with exponential backoff over about 6 hours.
