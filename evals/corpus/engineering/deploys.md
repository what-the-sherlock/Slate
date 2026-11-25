---
title: Deploying to production
visibility: public
---
# Deploying to production

## Release train

Production releases leave on the release train every Tuesday and Thursday at 11:00 CET. Hotfixes can ship any time with a reviewer's approval.

## Deploy freeze

There is a deploy freeze every Friday from 14:00 CET until Monday morning, and from December 20 to January 3.

## Canary and rollback

Every release first goes to a canary receiving 5% of traffic for 30 minutes. To roll back, run `kfctl rollback --to <release>` with the previous release tag; rollbacks need no approval.
