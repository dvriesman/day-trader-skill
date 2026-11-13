import datetime
import unittest

from _load import load

b3 = load("trader-alerts", "b3")
D = datetime.date


class Clock(unittest.TestCase):
    def test_sao_paulo_is_utc_minus_3_all_year(self):
        for month in (1, 6, 11):
            utc = datetime.datetime(2026, month, 10, 13, 25)
            self.assertEqual(b3.to_brt(utc), datetime.datetime(2026, month, 10, 10, 25))
        self.assertEqual(b3.parse_now("2026-11-12 10:25"), datetime.datetime(2026, 11, 12, 13, 25))
        self.assertEqual(b3.parse_now("2026-11-12T13:25:00Z"), datetime.datetime(2026, 11, 12, 13, 25))

    def test_session_follows_us_daylight_time(self):
        self.assertEqual(b3.session(D(2026, 11, 12)), (datetime.time(10, 0), datetime.time(17, 55)))
        self.assertEqual(b3.session(D(2026, 6, 10)), (datetime.time(10, 0), datetime.time(16, 55)))
        self.assertEqual(b3.session(D(2026, 3, 6)), (datetime.time(10, 0), datetime.time(17, 55)))   # before Mar 8
        self.assertEqual(b3.session(D(2026, 3, 9)), (datetime.time(10, 0), datetime.time(16, 55)))   # after Mar 8
        self.assertEqual(b3.call_end(datetime.time(17, 55)), datetime.time(18, 0))

    def test_holidays_and_late_open(self):
        for d in ("2026-02-16", "2026-02-17", "2026-04-03", "2026-04-21", "2026-05-01", "2026-06-04",
                  "2026-09-07", "2026-10-12", "2026-11-02", "2026-11-20", "2026-12-24", "2026-12-25",
                  "2026-12-31"):
            self.assertFalse(b3.trading_day(D.fromisoformat(d))[0], d)
        self.assertTrue(b3.trading_day(D(2026, 7, 9))[0])         # Sao Paulo's own holiday: B3 trades
        self.assertTrue(b3.trading_day(D(2026, 1, 26))[0])
        self.assertEqual(b3.session(D(2026, 2, 18))[0], datetime.time(13, 0))   # Ash Wednesday
        self.assertEqual(b3.next_open(D(2026, 11, 13)), "2026-11-16 10:00")
        self.assertIn("provisional", b3.calendar_note(D(2027, 3, 1)))
        self.assertIn("covers", b3.calendar_note(D(2028, 3, 1)))

    def test_config_overrides_the_grid(self):
        self.assertEqual(b3.session(D(2026, 11, 12), {"session_close": "16:55"})[1], datetime.time(16, 55))


class Symbols(unittest.TestCase):
    def test_b3_tickers_only(self):
        for ok in ("PETR4", "petr4", "PETR4.SA", "VALE3", "TAEE11", "B3SA3", "BOVA11", "AAPL34", "PETR4F"):
            self.assertTrue(b3.is_ticker(ok), ok)
        for bad in ("AAPL", "BRK.B", "PETRK440", "../x", "PETR4;rm", "", "PETR444", "1234F"):
            self.assertFalse(b3.is_ticker(bad), bad)

    def test_yahoo_and_lots(self):
        self.assertEqual(b3.yahoo("petr4f"), "PETR4.SA")
        self.assertEqual(b3.from_yahoo("VALE3.SA"), "VALE3")
        self.assertEqual(b3.fractional("PETR4"), "PETR4F")
        self.assertEqual(b3.split_lots(214), (200, 14))
        self.assertEqual(b3.split_lots(51), (0, 51))

    def test_money_and_numbers(self):
        self.assertEqual(b3.brl(4518.5), "R$ 4.518,50")
        self.assertEqual(b3.brl(-93.27), "-R$ 93,27")
        self.assertEqual(b3.px(44.1), "44,10")
        self.assertEqual(b3.parse_number("44,10"), 44.1)
        self.assertEqual(b3.parse_number("R$ 1.234,56"), 1234.56)
        self.assertEqual(b3.parse_number("1,234.56"), 1234.56)


if __name__ == "__main__":
    unittest.main()
