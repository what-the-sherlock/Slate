---
title: "Postmortem: Ledger outage"
visibility: private
---
# Postmortem: Ledger outage, 12 August 2026

## Summary

Billing was unavailable for 4 hours because the TLS certificate on ledger-db expired.

## Root cause

The certificate was renewed by hand and not tracked by automated renewal.

## Actions

Move every certificate to automated renewal, and alert 21 days before any certificate expires.
