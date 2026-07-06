"""Vessel traffic anomaly detector.

Collects AIS positions along a slice of the Norwegian coast, stores tracks in
SQLite, and flags anomalous vessel behaviour (AIS gaps, sudden stops, speed
anomalies, route deviation). Live positions come from BarentsWatch; historical
AIS from Kystverket is used offline to tune and validate the flag logic.
"""
