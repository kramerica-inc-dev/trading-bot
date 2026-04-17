#!/usr/bin/env python3
"""Safe utilities for refreshing live regime profiles and emitting audit artifacts."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple


def load_profile(path: str | Path) -> Dict:
    return json.loads(Path(path).read_text())


def _flatten_numeric_params(payload: Dict) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for regime, cfg in (payload.get('regime_profiles', {}) or {}).items():
        if not isinstance(cfg, dict):
            continue
        for key, value in cfg.items():
            if isinstance(value, (int, float)):
                result[f'{regime}.{key}'] = float(value)
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, (int, float)):
                        result[f'{regime}.{key}.{sub_key}'] = float(sub_value)
    return result


def _mean_metric(payload: Dict, key: str) -> float:
    diags = payload.get('regime_diagnostics', {}) or {}
    vals = [float(v.get(key, 0.0)) for v in diags.values() if isinstance(v, dict)]
    return sum(vals) / len(vals) if vals else 0.0


def profile_param_drift(current: Dict, candidate: Dict) -> float:
    left = _flatten_numeric_params(current)
    right = _flatten_numeric_params(candidate)
    shared = sorted(set(left) & set(right))
    if not shared:
        return 0.0
    ratios = []
    for key in shared:
        baseline = abs(left[key]) if abs(left[key]) > 1e-9 else 1.0
        ratios.append(abs(right[key] - left[key]) / baseline)
    return sum(ratios) / len(ratios) if ratios else 0.0


def evaluate_profile_refresh(
    current: Dict,
    candidate: Dict,
    *,
    min_regime_overlap: int = 1,
    max_param_drift: float = 0.35,
    require_improvement: bool = True,
) -> Tuple[bool, Dict]:
    current_profiles = set((current.get('regime_profiles') or {}).keys())
    candidate_profiles = set((candidate.get('regime_profiles') or {}).keys())
    overlap = len(current_profiles & candidate_profiles) if current_profiles else len(candidate_profiles)
    candidate_pf = _mean_metric(candidate, 'mean_test_pf')
    current_pf = _mean_metric(current, 'mean_test_pf')
    candidate_windows = _mean_metric(candidate, 'windows')
    current_windows = _mean_metric(current, 'windows')
    drift = profile_param_drift(current, candidate) if current_profiles else 0.0
    accepted = bool(candidate_profiles) and overlap >= int(min_regime_overlap) and drift <= float(max_param_drift)
    if require_improvement and current_profiles:
        accepted = accepted and ((candidate_pf > current_pf + 1e-9) or (candidate_windows >= current_windows + 0.5))
    report = {
        'accepted': bool(accepted),
        'candidate_regimes': sorted(candidate_profiles),
        'current_regimes': sorted(current_profiles),
        'regime_overlap': overlap,
        'candidate_mean_pf': candidate_pf,
        'current_mean_pf': current_pf,
        'candidate_mean_windows': candidate_windows,
        'current_mean_windows': current_windows,
        'param_drift': drift,
        'checked_at': datetime.now(timezone.utc).isoformat(),
    }
    return bool(accepted), report


def write_profile_refresh_report(
    report_dir: str | Path,
    *,
    report: Dict,
    current_path: str | Path,
    candidate_path: str | Path,
    current: Dict | None = None,
    candidate: Dict | None = None,
) -> Dict[str, str]:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stem = f'{stamp}-profile-refresh'
    current = current or (load_profile(current_path) if Path(current_path).exists() else {'regime_profiles': {}, 'regime_diagnostics': {}})
    candidate = candidate or load_profile(candidate_path)
    payload = {
        'report': dict(report),
        'current_path': str(current_path),
        'candidate_path': str(candidate_path),
        'current_summary': {
            'regimes': sorted((current.get('regime_profiles') or {}).keys()),
            'mean_pf': _mean_metric(current, 'mean_test_pf'),
            'mean_windows': _mean_metric(current, 'windows'),
        },
        'candidate_summary': {
            'regimes': sorted((candidate.get('regime_profiles') or {}).keys()),
            'mean_pf': _mean_metric(candidate, 'mean_test_pf'),
            'mean_windows': _mean_metric(candidate, 'windows'),
        },
    }
    json_path = report_dir / f'{stem}.json'
    md_path = report_dir / f'{stem}.md'
    json_path.write_text(json.dumps(payload, indent=2))
    md = [
        '# Live profile refresh report',
        '',
        f"- Checked at: {report.get('checked_at', '')}",
        f"- Accepted: {'yes' if report.get('accepted') else 'no'}",
        f"- Candidate mean PF: {report.get('candidate_mean_pf', 0.0):.3f}",
        f"- Current mean PF: {report.get('current_mean_pf', 0.0):.3f}",
        f"- Candidate mean windows: {report.get('candidate_mean_windows', 0.0):.2f}",
        f"- Current mean windows: {report.get('current_mean_windows', 0.0):.2f}",
        f"- Param drift: {report.get('param_drift', 0.0):.3f}",
        f"- Regime overlap: {report.get('regime_overlap', 0)}",
        '',
        '## Current regimes',
        ', '.join(payload['current_summary']['regimes']) or '(none)',
        '',
        '## Candidate regimes',
        ', '.join(payload['candidate_summary']['regimes']) or '(none)',
        '',
        '## Paths',
        f"- current: {current_path}",
        f"- candidate: {candidate_path}",
    ]
    md_path.write_text('\n'.join(md) + '\n')
    return {'report_json_path': str(json_path), 'report_md_path': str(md_path)}


def refresh_live_profile(
    current_path: str | Path,
    candidate_path: str | Path,
    *,
    min_regime_overlap: int = 1,
    max_param_drift: float = 0.35,
    require_improvement: bool = True,
    backup_suffix: str = '.bak',
    report_dir: str | Path | None = None,
) -> Dict:
    current_path = Path(current_path)
    candidate_path = Path(candidate_path)
    current = load_profile(current_path) if current_path.exists() else {'regime_profiles': {}, 'regime_diagnostics': {}}
    candidate = load_profile(candidate_path)
    accepted, report = evaluate_profile_refresh(
        current,
        candidate,
        min_regime_overlap=min_regime_overlap,
        max_param_drift=max_param_drift,
        require_improvement=require_improvement,
    )
    if accepted:
        if current_path.exists():
            backup_path = current_path.with_suffix(current_path.suffix + backup_suffix)
            shutil.copy2(current_path, backup_path)
            report['backup_path'] = str(backup_path)
        current_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = current_path.with_suffix(current_path.suffix + '.tmp')
        tmp.write_text(json.dumps(candidate, indent=2))
        tmp.replace(current_path)
        report['updated_path'] = str(current_path)
    if report_dir:
        report.update(write_profile_refresh_report(
            report_dir,
            report=report,
            current_path=current_path,
            candidate_path=candidate_path,
            current=current,
            candidate=candidate,
        ))
    return report
