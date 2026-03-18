# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 2.0.x   | ✅ Active support  |
| < 2.0   | ❌ No longer supported |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

### How to Report

1. **Do NOT** open a public GitHub issue for security vulnerabilities.
2. **Email** the maintainer directly at: [mahankali.harshith@epam.com](mailto:mahankali.harshith@epam.com)
3. Include the following in your report:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

### What to Expect

- **Acknowledgment** within 48 hours of your report.
- **Assessment** within 7 days — we'll confirm whether the issue is valid and its severity.
- **Fix timeline** depends on severity:
  - **Critical** (data exposure, RCE): Patch within 72 hours
  - **High** (auth bypass, injection): Patch within 7 days
  - **Medium** (info disclosure, CSRF): Patch within 30 days
  - **Low** (minor hardening): Next scheduled release

### Security Measures in Place

This project implements the following security controls:

- **CSP Headers** — Content Security Policy to mitigate XSS
- **Rate Limiting** — Per-IP sliding window (10 RPM scan, 60 RPM general)
- **API Key Protection** — Destructive endpoints require `X-API-Key` header
- **File Validation** — Extension check + magic byte validation for uploads
- **Path Traversal Guards** — Upload filenames are sanitized and UUID-suffixed
- **Input Size Limits** — 20MB max file size, stream-validated
- **CORS Restrictions** — Allowlist-based origin control
- **Non-root Docker** — Production container runs as `appuser` (UID 1000)
- **Secret Management** — No hardcoded secrets; all via environment variables
- **Dependency Pinning** — Exact versions in `requirements.txt` for reproducibility
- **Automated Scanning** — CI pipeline includes `bandit` (code) and `pip-audit` (dependencies)

### Security Configuration Checklist

For production deployments, ensure:

```bash
# Required
API_SECRET_KEY=<strong-random-key>      # Protects DELETE/reset endpoints
API_DOCS_ENABLED=false                  # Hide Swagger UI
API_DEBUG=false                         # Disable hot-reload

# Recommended
RATE_LIMIT_RPM=30                       # Tighten general rate limit
RATE_LIMIT_SCAN_RPM=10                  # Tighten scan rate limit
LOG_LEVEL=WARNING                       # Reduce log verbosity
CORS_ORIGINS=https://your-domain.com    # Restrict CORS origins
```
