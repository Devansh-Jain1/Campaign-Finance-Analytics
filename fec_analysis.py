"""Reproducible exploratory analysis of FEC weball candidate-period summaries."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

SCHEMA_URL = 'https://www.fec.gov/campaign-finance-data/all-candidates-file-description/'
# Official FEC positions 1..30, kept in exactly that order. No anonymous offsets.
COLUMNS = [
    'CAND_ID', 'CAND_NAME', 'CAND_ICI', 'PTY_CD', 'CAND_PTY_AFFILIATION',
    'TTL_RECEIPTS', 'TRANS_FROM_AUTH', 'TTL_DISB', 'TRANS_TO_AUTH', 'COH_BOP',
    'COH_COP', 'CAND_CONTRIB', 'CAND_LOANS', 'OTHER_LOANS', 'CAND_LOAN_REPAY',
    'OTHER_LOAN_REPAY', 'DEBTS_OWED_BY', 'TTL_INDIV_CONTRIB', 'CAND_OFFICE_ST',
    'CAND_OFFICE_DISTRICT', 'SPEC_ELECTION', 'PRIM_ELECTION', 'RUN_ELECTION',
    'GEN_ELECTION', 'GEN_ELECTION_PRECENT', 'OTHER_POL_CMTE_CONTRIB',
    'POL_PTY_CONTRIB', 'CVG_END_DT', 'INDIV_REFUNDS', 'CMTE_REFUNDS',
]
MONEY_COLUMNS = COLUMNS[5:18] + COLUMNS[25:27] + COLUMNS[28:30]
DEFAULT_CYCLES = list(range(2000, 2027, 2))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_cycle(path: Path, cycle: int) -> tuple[pd.DataFrame, dict]:
    if cycle % 2 or cycle < 1980:
        raise ValueError(f'Cycle must be an even FEC reporting-period end year: {cycle}')
    raw = path.read_bytes()
    try:
        text, encoding = raw.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        text, encoding = raw.decode('latin-1'), 'latin-1'
    rows = list(csv.reader(io.StringIO(text), delimiter='|', quoting=csv.QUOTE_NONE))
    if not rows:
        raise ValueError(f'{path.name}: empty input')
    for n, row in enumerate(rows, 1):
        if len(row) != len(COLUMNS):
            raise ValueError(f'{path.name}:{n}: expected 30 fields, found {len(row)}')
    frame = pd.DataFrame(rows, columns=COLUMNS, dtype='string')
    for col in COLUMNS:
        frame[col] = frame[col].str.strip().replace('', pd.NA)
    if frame.CAND_ID.isna().any():
        raise ValueError(f'{path.name}: candidate ID is missing')
    if frame.CAND_ID.duplicated().any():
        raise ValueError(f'{path.name}: duplicate candidate IDs; investigate before analysis')
    missing = {}
    for col in MONEY_COLUMNS:
        original = frame[col]
        numeric = pd.to_numeric(original, errors='coerce')
        if (original.notna() & numeric.isna()).any():
            raise ValueError(f'{path.name}: malformed nonempty numeric value in {col}')
        if numeric.isin([float('inf'), float('-inf')]).any():
            raise ValueError(f'{path.name}: nonfinite numeric value in {col}')
        frame[col] = numeric.astype('Float64')
        missing[col] = int(original.isna().sum())
    coverage = pd.to_datetime(frame.CVG_END_DT, format='%m/%d/%Y', errors='coerce')
    if (frame.CVG_END_DT.notna() & coverage.isna()).any():
        raise ValueError(f'{path.name}: malformed nonempty coverage date')
    frame['COVERAGE_END_DATE'] = coverage
    as_of = pd.Timestamp(datetime.now(timezone.utc).date())
    frame['COVERAGE_DATE_FUTURE_AT_RUN'] = coverage.gt(as_of)
    frame['REPORTING_CYCLE'] = cycle
    frame['PERIOD_START_YEAR'] = cycle - 1
    frame['PERIOD_LABEL'] = f'{cycle - 1}-{cycle}'
    # Preserve unknown components: missing is not a known zero refund.
    frame['TOTAL_REFUNDS'] = frame.INDIV_REFUNDS + frame.CMTE_REFUNDS
    # This is reported gross receipts, NOT donations, unique donor money, or
    # inflation-adjusted/net receipts. Transfers are retained separately.
    quality = {
        'cycle': cycle, 'period': f'{cycle-1}-{cycle}', 'records': len(frame),
        'source_file': path.name, 'source_sha256': digest(raw), 'encoding': encoding,
        'missing_money_values': missing,
        'missing_party': int(frame.CAND_PTY_AFFILIATION.isna().sum()),
        'coverage_min': str(coverage.min().date()),
        'coverage_max': str(coverage.max().date()),
        'future_coverage_dates_at_run': int(frame.COVERAGE_DATE_FUTURE_AT_RUN.sum()),
    }
    return frame, quality


def download_cycle(cache: Path, cycle: int) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    name = f'weball{cycle % 100:02}.txt'
    path, record = cache / name, cache / f'{name}.source.json'
    if path.exists() and record.exists():
        saved = json.loads(record.read_text())
        if digest(path.read_bytes()) != saved['text_sha256']:
            raise ValueError(f'{path}: cached data does not match provenance hash')
        return path
    url = f'https://www.fec.gov/files/bulk-downloads/{cycle}/weball{cycle % 100:02}.zip'
    request = Request(url, headers={'User-Agent': 'Campaign-Finance-Analytics/1.0 (public research)'})
    with urlopen(request, timeout=45) as response:
        archive = response.read()
        last_modified = response.headers.get('Last-Modified')
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        members = [n for n in zf.namelist() if Path(n).name.lower() == name]
        if len(members) != 1:
            raise ValueError(f'{url}: expected exactly one {name}')
        content = zf.read(members[0])
    path.write_bytes(content)
    record.write_text(json.dumps({
        'url': url, 'retrieved_at_utc': datetime.now(timezone.utc).isoformat(),
        'server_last_modified': last_modified, 'zip_sha256': digest(archive),
        'text_sha256': digest(content), 'schema_url': SCHEMA_URL,
    }, indent=2) + '\n')
    return path


def analyze(data_dir: Path, cycles: list[int], download=False):
    frames, quality, sources = [], [], []
    for cycle in cycles:
        path = download_cycle(data_dir, cycle) if download else data_dir / f'weball{cycle%100:02}.txt'
        frame, audit = read_cycle(path, cycle)
        frames.append(frame)
        quality.append(audit)
        provenance = data_dir / f'{path.name}.source.json'
        sources.append(json.loads(provenance.read_text()) if provenance.exists() else {
            'source_file': path.name, 'text_sha256': audit['source_sha256'], 'origin': 'user-supplied file'
        })
    combined = pd.concat(frames, ignore_index=True)
    counts = (combined.assign(PARTY=combined.CAND_PTY_AFFILIATION.fillna('UNKNOWN'))
              .groupby(['REPORTING_CYCLE', 'PERIOD_LABEL', 'PARTY']).size()
              .rename('CANDIDATE_PERIOD_RECORDS').reset_index())
    unique = (combined.assign(PARTY=combined.CAND_PTY_AFFILIATION.fillna('UNKNOWN'))
              .groupby('PARTY').CAND_ID.nunique().rename('DISTINCT_FEC_CANDIDATE_IDS').reset_index())
    # Compare within one reporting period; do not rank 26 years of nominal totals together.
    latest = combined[(combined.REPORTING_CYCLE == max(cycles))
                      & combined.COVERAGE_END_DATE.notna()
                      & ~combined.COVERAGE_DATE_FUTURE_AT_RUN]
    fields = ['CAND_ID', 'CAND_NAME', 'CAND_PTY_AFFILIATION', 'REPORTING_CYCLE',
              'PERIOD_LABEL', 'COVERAGE_END_DATE', 'TTL_RECEIPTS', 'TRANS_FROM_AUTH',
              'TTL_INDIV_CONTRIB', 'TOTAL_REFUNDS']
    receipts = latest[fields].dropna(subset=['TTL_RECEIPTS']).nlargest(20, 'TTL_RECEIPTS')
    refunds = latest[fields].dropna(subset=['TOTAL_REFUNDS']).nlargest(10, 'TOTAL_REFUNDS')
    audit = {'schema_url': SCHEMA_URL, 'cycles': cycles, 'cycle_files': len(cycles),
             'analysis_as_of_utc': datetime.now(timezone.utc).isoformat(),
             'candidate_period_records': len(combined), 'distinct_fec_candidate_ids': int(combined.CAND_ID.nunique()),
             'quality_by_cycle': quality, 'sources': sources,
             'latest_cycle_is_in_progress': max(cycles) >= datetime.now(timezone.utc).year,
             'limitations': [
                 'Amounts are reported gross receipts in nominal dollars, not donations or unique-donor totals.',
                 'Authorized-committee transfers can double count financial activity; no net-receipts claim.',
                 'Coverage end dates vary by candidate. Current-cycle results are provisional, not final election totals.',
                 'Missing/future coverage dates are retained and flagged, but excluded from latest-period receipt/refund rankings.',
                 'Party counts measure candidate-period records; the same FEC candidate ID can recur across periods.',
                 'Party affiliation can change; distinct IDs by party do not sum to unique individuals.',
                 'No causal inference, fraud finding, PandasAI interface, or inflation-adjusted trend is established.',
             ]}
    return combined, counts, unique, receipts, refunds, audit


def write_results(result, out: Path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    combined, counts, unique, receipts, refunds, audit = result
    out.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out / 'candidate_periods.csv', index=False)
    counts.to_csv(out / 'party_counts_by_period.csv', index=False)
    unique.to_csv(out / 'distinct_candidate_ids_by_party.csv', index=False)
    receipts.to_csv(out / 'latest_period_top20_gross_receipts.csv', index=False)
    refunds.to_csv(out / 'latest_period_top10_refunds.csv', index=False)
    (out / 'validation.json').write_text(json.dumps(audit, indent=2) + '\n')
    colors = {'DEM': '#2d6da3', 'REP': '#bd4e47'}
    latest_cycle = max(audit['cycles'])
    latest_counts = counts[counts.REPORTING_CYCLE == latest_cycle].nlargest(10, 'CANDIDATE_PERIOD_RECORDS')
    fig, ax = plt.subplots(figsize=(9, 5), layout='constrained')
    ax.barh(latest_counts.PARTY.iloc[::-1], latest_counts.CANDIDATE_PERIOD_RECORDS.iloc[::-1], color='#386c73')
    ax.set(title=f'Candidate-period records by party, {latest_cycle-1}-{latest_cycle}', xlabel='Records in FEC weball file (not voters or elected seats)')
    fig.savefig(out / 'party_counts.png', dpi=150)
    plt.close(fig)
    for table, measure, title, filename in [
        (receipts, 'TTL_RECEIPTS', '20 highest reported gross receipts', 'gross_receipts.png'),
        (refunds, 'TOTAL_REFUNDS', '10 highest reported contribution refunds', 'refunds.png'),
    ]:
        ordered = table.sort_values(measure)
        fig, ax = plt.subplots(figsize=(12, 9), layout='constrained')
        labels = ordered.CAND_NAME.fillna(ordered.CAND_ID) + ' [' + ordered.CAND_ID + ']'
        ax.barh(labels, ordered[measure].astype(float), color=[colors.get(x, '#687780') for x in ordered.CAND_PTY_AFFILIATION])
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f'${value/1_000_000:,.1f}M'))
        axis_note = ('Reported nominal dollars; gross receipts include transfers' if measure == 'TTL_RECEIPTS'
                     else 'Reported nominal dollars; refunds to individuals plus committees')
        ax.set(title=f'{title}, {latest_cycle-1}-{latest_cycle}\nCoverage differs by candidate; current period is provisional', xlabel=axis_note)
        ax.tick_params(axis='y', labelsize=8)
        fig.savefig(out / filename, dpi=150)
        plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--out', type=Path, default=Path('results'))
    parser.add_argument('--cycles', nargs='+', type=int, default=DEFAULT_CYCLES)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    if len(set(args.cycles)) != len(args.cycles):
        parser.error('Duplicate cycles would duplicate candidate-period records')
    result = analyze(args.data_dir, args.cycles, args.download)
    write_results(result, args.out)
    print(json.dumps({k:result[-1][k] for k in ['cycle_files','candidate_period_records','distinct_fec_candidate_ids']}, indent=2))
