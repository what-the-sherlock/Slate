---
title: "Issue #398: Label PDFs cut off on A6 printers"
visibility: private
kind: github
author: sofia-p
date: 2026-11-22T10:00:00Z
---
# Issue #398: Label PDFs cut off on A6 printers

Status: open · Opened by sofia-p on 2026-11-05 · Labels: bug, labels

Customers printing shipping labels on A6 thermal printers lose the bottom 8 mm, including the barcode.

## Comment by mei-chen on 2026-11-22

Root cause: we render labels at 300 DPI but scale them as if they were 203 DPI. The fix is planned for release 2.14.
