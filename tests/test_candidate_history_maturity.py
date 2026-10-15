"""Hindsight-safe horizon labeling from the scanner's OWN observed swaps.

These observations are theoretical quote outcomes; never treat a historical
transaction price as a real executed entry/exit.
"""
import unittest
from datetime import datetime, timedelta, timezone
from tools.candidate_price_tracker import (
    update, original_transaction_quotes, observed_horizon_quote,
)
from tools.missed_pump_learning import normalize, net_sda

T=datetime(2026,10,15,4,0,tzinfo=timezone.utc)
ADDR="0x123abc"


def stamp(minutes):
    return (T+timedelta(minutes=minutes)).isoformat()


def candidate():
    return {
        "at":stamp(0), "address":ADDR, "quote_at":stamp(-1),
        "phase":"WATCH","score":80,"quality":70,
        "snapshot_impulse":10,"impulse":0,"delta_impulse":0,
        "data_age_sec":60,"trades_1h":9,"buy_ratio":2.2,
        "volume_1h":1500,"flow_1h":800,"flow_15m":500,
        "m15":3.0,"m1h":5,"m4h":8,"price_sda":1.0,
        "eligible":False,"gates":["lifecycle","impulse"],
    }


def tx(minutes,price,side="buy",volume=100,hash=None):
    return {
        "timestamp":stamp(minutes), "price":price,
        "type":side,"volume":volume,
        "hash":hash or f"tx-{minutes}-{price}",
    }


def source(rows,minutes=90):
    return {"updated_at":stamp(minutes),
            "tokens":{ADDR:{"transactions":rows}}}


def market(minutes=90,price=0.70):
    return {"updated_at":stamp(minutes),"tokens":{ADDR:{"analysis":{
        "last_transaction":stamp(minutes),
        "price_in_sda":price,
    }}}}


class FutureQuoteHistoryTests(unittest.TestCase):
    def test_recovers_first_after_hour_not_subsequent_pump_peak(self):
        initial=update({"funnels":[{"at":stamp(0),
            "candidate_snapshots":[candidate()]}]},market(1,1.0))
        self.assertEqual(len(initial["observations"]),1)
        self.assertEqual(initial["observations"][0]["outcomes"],{})
        later=update({"observations":initial["observations"],"funnels":[]},
                     market(),
                     history_market=source([
                         tx(-1,1),tx(61,1.04),tx(66,1.8),
                         tx(90,.70),
                     ]))
        row=later["observations"][0]
        out=row["outcomes"]["60m"]
        self.assertEqual(out["observed_at"],stamp(61))
        self.assertEqual(out["price_sda"],1.04)
        self.assertEqual(out["tx_hash"],"tx-61-1.04")
        self.assertEqual(out["source"],"observed_later_market_transaction_not_executable_fill")
        self.assertAlmostEqual(out["net_sda_model"],net_sda(1.0,1.04),4)
        self.assertEqual(row["entry_price_sda"],1.0)
        self.assertEqual(row["features"]["score"],80)
        self.assertEqual(len(normalize(later["observations"])),1)

    def test_missing_misaligned_or_future_history_never_creates_outcome(self):
        state={"funnels":[{"at":stamp(0),
               "candidate_snapshots":[candidate()]}]}
        initial=update(state,market(1,1.0))
        state={"observations":initial["observations"],"funnels":[]}
        # History and market must be from exact same scanner cycle.
        self.assertNotIn("60m",update(state,market(),
            history_market=source([tx(61,1.05)],89))["observations"][0]["outcomes"])
        # A future event beyond the scanner snapshot is not already observed.
        self.assertNotIn("60m",update(state,market(),
            history_market=source([tx(91,100.0)]))["observations"][0]["outcomes"])
        # A >75min first trade cannot be falsely labeled as a 60min exit.
        self.assertNotIn("60m",update(state,market(),
            history_market=source([tx(80,1.3)]))["observations"][0]["outcomes"])
        # No legitimate on-chain type means no reliable quote.
        self.assertNotIn("60m",update(state,market(),
            history_market=source([tx(61,2.0,side="estimate")]))["observations"][0]["outcomes"])
        self.assertNotIn("60m",update(state,market(),
            history_market=source([tx(61,-3)]))["observations"][0]["outcomes"])

    def test_legacy_missing_original_features_cannot_be_backfilled(self):
        row={"id":f"{stamp(0)}|{ADDR}","at":stamp(0),
             "address":ADDR,"entry_price_sda":1.0,
             "eligible":False,"gates":["score"],"score":80,"outcomes":{}}
        result=update({"observations":[row],"funnels":[]},market(),
                      history_market=source([tx(62,100.0)]))
        self.assertEqual(result["observations"][0]["outcomes"],{})
        self.assertEqual(normalize(result["observations"]),[])

    def test_never_overwrite_existing_valid_label(self):
        first=update({"funnels":[{"at":stamp(0),
              "candidate_snapshots":[candidate()]}]},market(1,1))
        row=first["observations"][0]
        row["outcomes"]["60m"]={
            "observed_at":stamp(65),"price_sda":.85,"elapsed_minutes":65,
        }
        result=update({"observations":[row],"funnels":[]},market(),
                     history_market=source([tx(60,5.0)]))
        self.assertEqual(result["observations"][0]["outcomes"]["60m"]["price_sda"],.85)

    def test_outcome_returns_negative_after_friction(self):
        first=update({"funnels":[{"at":stamp(0),
            "candidate_snapshots":[candidate()]}]},market(1,1))
        row=update({"observations":first["observations"],"funnels":[]},
                   market(),history_market=source([tx(62,.98)])
                   )["observations"][0]
        self.assertLess(row["outcomes"]["60m"]["net_sda_model"],0)
        self.assertEqual(len(normalize([row])),1)

if __name__=="__main__":
    unittest.main()
