---
title: System architecture
visibility: public
---
# System architecture

## Services

Dispatch, written in Go, assigns shipments to carriers. Ledger keeps billing records in PostgreSQL. Tracker ingests carrier status events from a Kafka topic called shipment-events.

## ETA model

Wayfinder is the machine-learning model that predicts arrival times. It is retrained every Sunday night on the previous eight weeks of deliveries and must beat the previous model's mean absolute error before it is promoted.

## Regions

Production runs in two cloud regions, Frankfurt and Dublin, with Frankfurt as primary.
