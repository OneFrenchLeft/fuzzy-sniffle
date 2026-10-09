"""Streak/joker abuse regressions: python test_streak_abuse.py."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import config
with patch.object(config, 'ADMIN_PASSWORD', 'test-only'):
    import db
import streak


class StreakAbuseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = datetime(2026, 10, 9, 23, 55)
        for target, name, value in (
            (db, 'DB_PATH', Path(tmp.name) / 'test.db'),
            (db, 'now_paris', lambda: self.now),
            (streak, 'now_paris', lambda: self.now),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        self.conn = db.db()
        self.addCleanup(self.conn.close)
        self.params = dict(config.DEFAULT_PARAMS)
        self.conn.execute("INSERT INTO users(prenom) VALUES ('Alice')")
        self.conn.execute("INSERT INTO user_jokers(prenom, count) VALUES ('Alice', 0)")
        # Establish that this chain started after the legacy ledger existed.
        self.conn.execute(
            "INSERT INTO joker_ledger(prenom, day, delta, reason, balance_after, created_at) "
            "VALUES ('Other', '2026-01-01', 0, 'admin_grant', 0, '2026-01-01T12:00:00')")
        self.conn.commit()

    def chain(self, length, end=0):
        for offset in range(end - length + 1, end + 1):
            day = (self.now + timedelta(days=offset)).date().isoformat()
            self.conn.execute("INSERT OR IGNORE INTO sr_daily_streak VALUES ('Alice', ?, 1)", (day,))
        self.conn.commit()

    def stock(self):
        return self.conn.execute("SELECT count FROM user_jokers WHERE prenom='Alice'").fetchone()[0]

    def card(self, number=1):
        self.conn.execute("INSERT INTO forgecards(numero, fiche_file, correction_file) VALUES (?, 'a', 'b')", (number,))
        self.conn.commit()

    def test_full_stock_milestone_cannot_be_claimed_after_spending(self):
        self.chain(4)
        self.conn.execute("UPDATE user_jokers SET count=2 WHERE prenom='Alice'")
        self.conn.commit()
        streak.reconcile_streak(self.conn, 'Alice')
        db.apply_joker_change(self.conn, 'Alice', -1, 'test_spend')
        self.conn.commit()
        for _ in range(3):
            self.assertEqual(streak.reconcile_streak(self.conn, 'Alice'), 4)
        self.assertEqual(self.stock(), 1)

    def test_bulk_award_is_not_repaid_when_stock_drops(self):
        self.chain(12)
        streak.reconcile_streak(self.conn, 'Alice')
        self.assertEqual(self.stock(), 2)
        db.apply_joker_change(self.conn, 'Alice', -1, 'test_spend')
        self.conn.commit()
        streak.reconcile_streak(self.conn, 'Alice')
        self.assertEqual(self.stock(), 1)

    def test_new_chain_earns_its_own_milestones(self):
        self.chain(4, end=-5)
        current = self.now
        self.now -= timedelta(days=5)
        streak.reconcile_streak(self.conn, 'Alice')
        self.now = current
        self.chain(4)
        streak.reconcile_streak(self.conn, 'Alice')
        self.assertEqual(self.stock(), 2)

    def test_backfilled_day_does_not_repay_the_same_milestone(self):
        self.chain(4)
        streak.reconcile_streak(self.conn, 'Alice')
        self.chain(5)
        self.assertEqual(streak.reconcile_streak(self.conn, 'Alice'), 5)
        self.assertEqual(self.stock(), 1)

    def test_next_milestone_awards_normally(self):
        self.chain(4)
        streak.reconcile_streak(self.conn, 'Alice')
        self.now += timedelta(days=4)
        self.chain(8)
        self.assertEqual(streak.reconcile_streak(self.conn, 'Alice'), 8)
        self.assertEqual(self.stock(), 2)

    def test_old_schema_is_migrated_without_losing_stock(self):
        import sqlite3
        legacy_path = db.DB_PATH.with_name('legacy.db')
        with sqlite3.connect(legacy_path) as legacy:
            legacy.execute('CREATE TABLE user_jokers(prenom TEXT PRIMARY KEY, count INTEGER, last_milestone INTEGER)')
            legacy.execute("INSERT INTO user_jokers VALUES ('Alice', 2, 3)")
        with patch.object(db, 'DB_PATH', legacy_path):
            db.init_db()
            db.init_db()
            with sqlite3.connect(legacy_path) as legacy:
                row = legacy.execute('SELECT count, last_milestone, milestone_chain_start FROM user_jokers').fetchone()
                self.assertEqual(row, (2, 3, None))

    def test_parallel_reconciliation_awards_only_once(self):
        self.chain(4)
        barrier = Barrier(4)
        def reconcile(_):
            conn = db.db()
            try:
                barrier.wait(timeout=5)
                return streak.reconcile_streak(conn, 'Alice')
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(reconcile, range(4))), [4] * 4)
        self.assertEqual(self.stock(), 1)

    def test_uninitialized_deck_is_not_an_empty_deck(self):
        self.card()
        db.log_event(self.conn, 'Alice', 'login')
        self.conn.commit()
        self.assertEqual(streak.streak_verdict(self.conn, 'Alice', self.params), 'due')
        self.assertEqual(streak.close_day(self.conn, 'Alice', '2026-10-09', self.params), 'missed')
        self.assertEqual(streak.compute_streak(self.conn, 'Alice'), 0)

    def test_new_card_without_user_state_is_still_due(self):
        self.card(1)
        self.card(2)
        self.conn.execute("INSERT INTO sr_state_user(prenom, numero, next_review) VALUES ('Alice', 1, '2026-10-15')")
        db.log_event(self.conn, 'Alice', 'login')
        self.conn.commit()
        self.assertEqual(streak.streak_verdict(self.conn, 'Alice', self.params), 'due')

    def test_really_empty_catalog_retains_free_validation(self):
        self.assertEqual(streak.close_day(self.conn, 'Alice', '2026-10-09', self.params), 'validated_free')


if __name__ == '__main__':
    unittest.main()
