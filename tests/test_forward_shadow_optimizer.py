import copy
import unittest
from datetime import datetime, timedelta, timezone
from tools.forward_shadow_agent import default_state
from tools.forward_shadow_optimizer import report


NOW = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)
BASE = {"version": "V30-AUTO-CONFIG-1", "generation": 0,
        "filters": {"entry_score": 68, "quality": 52, "impulse": 8,
                    "buy_ratio": 1.1, "trades": 5},
        "previous_filters": None, "promotion": None}


def samples(positive=True):
    state = default_state()
    state["updated_at"] = NOW.isoformat()
    first = NOW - timedelta(days=3)
    for i in range(70):
        date = first + timedelta(hours=i)
        pnl = 2.0 if i < 49 or positive else -4.0
        state["strategies"]["quality"]["trades"].append({
            "address": "0x" + str(i % 9), "opened_at": (date-timedelta(minutes=10)).isoformat(),
            "closed_at": date.isoformat(), "valid_for_learning": True,
            "pnl_sda": pnl
        })
        # A different contender with poor TRAIN must not win solely because
        # its holdout contains enormous lucky trades.
        state["strategies"]["momentum"]["trades"].append({
            "address": "0x" + str(i % 9), "opened_at": (date-timedelta(minutes=10)).isoformat(),
            "closed_at": date.isoformat(), "valid_for_learning": True,
            "pnl_sda": -2.0 if i < 49 else 100.0
        })
    return state


class ForwardOptimizerTests(unittest.TestCase):
    def test_can_promote_after_positive_forward_holdout(self):
        result, conf = report(samples(), copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "PROMOTED_PAPER")
        self.assertTrue(result["promotion_applied"])
        self.assertEqual(conf["generation"], 1)
        self.assertEqual(conf["filters"]["entry_score"], 72)
        self.assertEqual(conf["previous_filters"], BASE["filters"])
        self.assertEqual(result["selected_challenger"]["name"], "quality")
        self.assertFalse(result["real_trading"])

    def test_bad_holdout_rejects_challenger(self):
        result, conf = report(samples(positive=False), copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "HOLDOUT_REJECTED")
        self.assertFalse(result["promotion_applied"])
        self.assertEqual(conf["generation"], 0)

    def test_outcomes_with_no_quote_quality_not_learned(self):
        state = samples()
        for row in state["strategies"]["quality"]["trades"]:
            row["valid_for_learning"] = False
        result, _ = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertNotEqual(result["status"], "PROMOTED_PAPER")

    def test_stale_scanner_refuses_promotion(self):
        state = samples()
        state["updated_at"] = (NOW - timedelta(hours=1)).isoformat()
        result, _ = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "WAITING_FOR_FRESH_SCANNER")

    def test_rollback_on_new_actual_paper_losses(self):
        config = copy.deepcopy(BASE)
        config.update({
            "generation": 1, "filters": {"entry_score": 72, "quality": 56, "impulse": 8,
                                         "buy_ratio": 1.2, "trades": 8},
            "previous_filters": BASE["filters"],
            "promotion": {"at": (NOW-timedelta(hours=3)).isoformat()}
        })
        trades = []
        for i in range(8):
            at = NOW-timedelta(hours=2, minutes=50-i)
            trades.append({"status": "CLOSED", "opened_at": at.isoformat(),
                           "closed_at": (at+timedelta(minutes=5)).isoformat(),
                           "address": "0x"+str(i), "closed_profit_sda": -4})
        result, new = report(samples(), config, {"closed_trades": trades}, NOW)
        self.assertEqual(result["status"], "ROLLED_BACK_PAPER")
        self.assertTrue(result["rollback_applied"])
        self.assertEqual(new["filters"], BASE["filters"])
        self.assertEqual(new["generation"], 2)

    def test_insufficient_events_no_autopromotion(self):
        state = default_state()
        state["updated_at"] = NOW.isoformat()
        result, conf = report(state, copy.deepcopy(BASE), {"closed_trades": []}, NOW)
        self.assertEqual(result["status"], "COLLECTING_FORWARD_TRADES")
        self.assertEqual(conf["generation"], 0)


if __name__ == "__main__":
    unittest.main()
