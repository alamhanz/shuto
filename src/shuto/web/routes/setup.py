"""Old home of the setup-side routes; they now live in `home`, `wizard`, `imports`, `jobs`,
`updates`, `settings`, `models` ... Only the names the characterization tests import stay here."""

from __future__ import annotations

from shuto.web.routes.home import _n  # noqa: F401  alias until T9
from shuto.web.routes.model_support import _plain_seconds  # noqa: F401  alias until T9
