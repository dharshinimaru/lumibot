"""Signal research engine: many news + price signals, tested on real data, ranked by result after costs.

Owner request (2026-11-13): a system that finds trades with a high likelihood of a
positive result by looking through more data than a person could. Holding period
5-20 trading days (owner chose 2-20; 2-4 day holds conflict with the SPEC's 5-day
satellite minimum, so they are tested for information only and never traded).
"""
