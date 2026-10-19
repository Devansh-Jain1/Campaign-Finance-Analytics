import tempfile
import unittest
from pathlib import Path

import pandas as pd
from fec_analysis import COLUMNS, read_cycle, analyze


def record(candidate='H0MA00001', receipts='1200', transfers='400', refunds=('30', '20')):
    row = [''] * 30
    for name, value in {'CAND_ID':candidate, 'CAND_NAME':'TEST, CANDIDATE',
                        'CAND_PTY_AFFILIATION':'DEM', 'TTL_RECEIPTS':receipts,
                        'TRANS_FROM_AUTH':transfers, 'INDIV_REFUNDS':refunds[0],
                        'CMTE_REFUNDS':refunds[1], 'CVG_END_DT':'06/30/2026'}.items():
        row[COLUMNS.index(name)] = value
    return '|'.join(row) + '\n'


class FecSchemaRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'weball26.txt'
    def tearDown(self):
        self.tmp.cleanup()
    def test_receipts_are_position_six_not_transfers_position_seven(self):
        self.path.write_text(record())
        frame,_ = read_cycle(self.path,2026)
        self.assertEqual(frame.TTL_RECEIPTS.iloc[0],1200)
        self.assertEqual(frame.TRANS_FROM_AUTH.iloc[0],400)
        self.assertEqual(frame.TOTAL_REFUNDS.iloc[0],50)
        self.assertEqual(frame.PERIOD_LABEL.iloc[0],'2025-2026')
    def test_missing_refund_is_unknown_not_zero(self):
        self.path.write_text(record(refunds=('', '20')))
        frame,_ = read_cycle(self.path,2026)
        self.assertTrue(pd.isna(frame.TOTAL_REFUNDS.iloc[0]))
    def test_corrupt_numeric_data_rejected(self):
        self.path.write_text(record(receipts='oops'))
        with self.assertRaisesRegex(ValueError,'malformed.*TTL_RECEIPTS'):
            read_cycle(self.path,2026)
    def test_duplicate_within_period_rejected(self):
        self.path.write_text(record()*2)
        with self.assertRaisesRegex(ValueError,'duplicate candidate'):
            read_cycle(self.path,2026)
    def test_schema_width_change_rejected(self):
        self.path.write_text(record().rstrip()+'|extra\n')
        with self.assertRaisesRegex(ValueError,'expected 30'):
            read_cycle(self.path,2026)
    def test_future_coverage_is_flagged_and_excluded_from_rankings(self):
        self.path.write_text(record().replace('06/30/2026','07/01/2090'))
        combined,counts,unique,receipts,refunds,audit=analyze(self.path.parent,[2026])
        self.assertTrue(combined.COVERAGE_DATE_FUTURE_AT_RUN.iloc[0])
        self.assertTrue(receipts.empty)
        self.assertEqual(audit['candidate_period_records'],1)
    def test_repeated_id_across_periods_not_counted_as_two_unique_ids(self):
        self.path.write_text(record())
        self.path.with_name('weball24.txt').write_text(record().replace('06/30/2026','12/31/2024'))
        combined,counts,unique,receipts,refunds,audit=analyze(self.path.parent,[2024,2026])
        self.assertEqual(audit['candidate_period_records'],2)
        self.assertEqual(audit['distinct_fec_candidate_ids'],1)
        self.assertEqual(set(receipts.REPORTING_CYCLE),{2026})
        self.assertEqual(counts.CANDIDATE_PERIOD_RECORDS.sum(),2)


if __name__ == '__main__':
    unittest.main()
