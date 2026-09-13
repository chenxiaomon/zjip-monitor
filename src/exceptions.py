"""Shared exception hierarchy for zjip-monitor."""


class ZJIPError(Exception):
    """Base exception for all zjip-monitor errors."""


class LoginError(ZJIPError):
    """Login failed for any reason."""


class CredentialsRejectedError(LoginError):
    """The platform explicitly rejected the account/password pair.

    Distinct from transport failures / unexpected page states so the lockout
    protection (status_store failure streak) only counts genuine rejections —
    a platform outage must never pause an account's scheduled scans.
    """


class PlatformUnavailableError(LoginError):
    """The platform refused the login for a reason that is not our credentials:
    a captcha demand, rate limiting, or maintenance/busy.

    Kept apart from both CredentialsRejectedError (which feeds the lockout
    streak) and the generic LoginError so the console can say *why* an account
    stopped working, and so a throttled platform is never hammered with a
    browser retry on top of the HTTP attempt.
    """

    def __init__(self, message: str, reason: str = "unavailable"):
        super().__init__(message)
        self.reason = reason  # "captcha" | "throttled" | "maintenance"


class CaptchaRequiredError(LoginError):
    """Slider captcha appeared — manual intervention required.

    Run with headless=false (set login.headless: false in settings.yaml)
    to complete the captcha once; the token will be cached for future runs.
    """


class TokenExpiredError(LoginError):
    """Cached token is expired or rejected by the server."""


class TokenExtractionError(LoginError):
    """Login redirect succeeded but token_key was absent from localStorage."""


class ScraperError(ZJIPError):
    """Data fetching failed."""


class RunCancelled(ZJIPError):
    """The current run was stopped at a checkpoint (stop flag / shutdown)."""
