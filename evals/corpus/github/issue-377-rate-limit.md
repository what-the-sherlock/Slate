---
title: "Issue #377: Raise the default API rate limit"
visibility: private
kind: github
author: arjun-rao
date: 2026-10-08T15:00:00Z
---
# Issue #377: Raise the default API rate limit

Status: closed · Opened by priya-shah on 2026-10-01 · Labels: api, limits

Enterprise trials keep hitting KF-2003 during bulk imports.

## Comment by arjun-rao on 2026-10-08

Done: the default limit is now 1,200 requests per minute per API key, up from 600. The API error docs are out of date until the next docs release.
