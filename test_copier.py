import datetime
import json
import os
import unittest

import db

TEST_DB = "test_copier.db"


class TestCopierBot(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        db.init_db(db_path=TEST_DB)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_database_persistence(self):
        self.assertIsNone(db.get_last_processed_id("Biology", db_path=TEST_DB))

        db.set_last_processed_id("Biology", 1500, db_path=TEST_DB)
        self.assertEqual(db.get_last_processed_id("Biology", db_path=TEST_DB), 1500)

        db.set_last_processed_id("Biology", 1505, db_path=TEST_DB)
        self.assertEqual(db.get_last_processed_id("Biology", db_path=TEST_DB), 1505)

    def test_iso_datetime_parsing(self):
        dt_str = "2026-08-03T08:00:00"
        parsed = datetime.datetime.fromisoformat(dt_str)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 8)
        self.assertEqual(parsed.day, 3)



if __name__ == "__main__":
    unittest.main()
