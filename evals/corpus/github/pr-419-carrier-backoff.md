---
title: "Pull request #419: Back off on carrier rate limits (KF-4031)"
visibility: private
kind: github
author: arjun-rao
date: 2026-11-26T17:10:00Z
---
# Pull request #419: Back off on carrier rate limits (KF-4031)

Status: open · Opened by arjun-rao on 2026-11-26 · Labels: carriers

When a carrier API answers 429, pickup bookings now wait and retry with exponential backoff (up to 5 minutes) instead of failing with KF-4031.
