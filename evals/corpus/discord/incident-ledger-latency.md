---
title: "#incidents · Nov 17, 2026 08:15"
visibility: private
kind: discord
author: Mei Chen, Arjun Rao
date: 2026-11-17T09:05:00Z
---
# Kestrel > #incidents > 2026-11-17

Mei Chen (08:15): Ledger p99 latency jumped to 2.4 seconds since about 07:50. Customers see slow tracking pages.
Arjun Rao (08:22): Query plans show a sequential scan on shipments.tracking_ref.
Mei Chen (08:51): Found it: the index on shipments.tracking_ref was dropped by Friday's migration. I've recreated it concurrently.
Mei Chen (09:05): p99 is back to 180 ms. I'll write the postmortem and add a migration check for dropped indexes.
