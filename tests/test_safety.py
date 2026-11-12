"""Offline pre-deployment safety checks; never connects to brokers."""
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

class TradingSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        os.environ["DB_PATH"]=os.path.join(cls.tmp.name,"watchlist.sqlite")
        os.environ["APP_PASSWORD"]="offline-test-only"
        cls.m=importlib.import_module("app")
        cls.m.db().close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_initial_symbols(self):
        self.assertEqual(set(self.m.active_symbols()),{"2449","6173","2327"})

    def test_watchlist_persists_after_connection(self):
        with self.m.db() as c:
            c.execute("INSERT INTO watchlist(symbol,name,sector,added_at) VALUES(?,?,?,?)",
                      ("3037","欣興","PCB",self.m.now().isoformat()))
        self.assertIn("3037",self.m.active_symbols())
        self.m.db().close()
        self.assertIn("3037",self.m.active_symbols())

    def test_no_signal_on_stale_quote(self):
        q={"stock_id":"2449","date":"2020-01-01T10:00:00+08:00","close":225,
           "average_price":224,"high":230,"low":220,
           "five_min_confirmed":True,"candle_count":6,"volume_ratio":2}
        status,reason,setup=self.m.signal(q)
        self.assertEqual(status,"資料不足")
        self.assertIsNone(setup)

    def test_no_signal_without_candles(self):
        q={"stock_id":"2449","date":self.m.now().isoformat(),"close":225,
           "average_price":224,"high":230,"low":220,
           "five_min_confirmed":False,"candle_count":0,"volume_ratio":2}
        with patch.object(self.m,"market_open",return_value=True):
            status,reason,setup=self.m.signal(q)
        self.assertEqual(status,"資料不足")
        self.assertIsNone(setup)

    def test_risk_rejects_10000_exceedance(self):
        # A 2-lot stop loss can exceed the total daily risk once fees, tax and slippage are included.
        risk=self.m.risk_values(entry=220,stop=215,target=230,lots=2)
        self.assertFalse(risk["passes"])
        self.assertGreater(risk["risk_including_buffer"],10000)

    def test_no_auto_order(self):
        self.assertFalse(self.m.DECISION_POLICY["auto_order"])

if __name__=="__main__":
    unittest.main()
