# V31 chronological holdout — observed-tick sensitivity

Input: uploaded positions (5).json, 2026-10-09. Chronological 70/30 split per exit-version, only full closes with price samples.

| Version | Train | Holdout | Delta train, -2% stop and 2% assumed cost | Delta holdout |
|---|---:|---:|---:|---:|
| V30.2 | 36 | 16 | +4.43 SDA | +3.46 SDA |
| V30.3 | 30 | 13 | +4.17 SDA | -0.17 SDA |

47 V30.2 trades excluded for missing usable samples. The test uses first observed tick below the stop, not an executable order fill. No market-depth simulation, realistic price impact, capital scheduling or statistical significance. A positive delta is NOT a positive absolute strategy P/L. No production changes are authorized by these results.
