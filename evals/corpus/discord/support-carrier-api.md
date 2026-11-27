---
title: "#support · Nov 26, 2026 14:30"
visibility: private
kind: discord
author: Sofia Petrova, Arjun Rao
date: 2026-11-26T15:02:00Z
---
# Kestrel > #support > 2026-11-26

Sofia Petrova (14:30): Several customers report error KF-4031 when booking DHL pickups.
Arjun Rao (14:41): KF-4031 means the carrier API rate-limited us. For now, retry the booking after 60 seconds.
Arjun Rao (15:02): I'm opening a PR so we back off automatically instead of failing.
