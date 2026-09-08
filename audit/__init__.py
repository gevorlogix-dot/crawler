"""Site audit engine.

Everything the AMPM-specific scripts do, generalised to run against any URL:
discovery, per-page extraction, the internal link graph, endpoint probes, an
optional rendered-browser sweep, a rule engine, and an HTML report.

    from audit import AuditConfig, run_audit
    result = run_audit(AuditConfig(base="https://example.com"))
    print(result.report_path)
"""

from .config import AuditConfig
from .runner import AuditResult, run_audit

__all__ = ["AuditConfig", "AuditResult", "run_audit"]
