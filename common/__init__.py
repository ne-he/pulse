"""Shared building blocks for every PULSE service: config, schemas, the Redis
Streams bus, and AQI health logic. If a service touches the data contract, it
imports it from here so the whole monorepo stays consistent."""
