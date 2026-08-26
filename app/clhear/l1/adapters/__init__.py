"""Adapter registry. Adapters self-describe via meta(); the pipeline owns
hashing, storage, diffing, and events (HLD §7.2)."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.clhear.l1.adapters.base import Adapter


def get_adapter(key: str) -> "Adapter":
    """Instantiate a registered adapter by key (import-on-demand keeps
    optional parser deps out of unrelated code paths)."""
    from app.clhear.l1.adapters import eur_lex, govinfo_us, nist, uk_legislation

    registry = {
        "uk_legislation": uk_legislation.UkLegislationAdapter,
        "eur_lex": eur_lex.EurLexAdapter,
        "govinfo_us_usc": govinfo_us.GovInfoUscAdapter,
        "govinfo_us_ecfr": govinfo_us.GovInfoEcfrAdapter,
        "nist_sp800_53": nist.NistSp80053Adapter,
        "nist_csf": nist.NistCsfAdapter,
    }
    return registry[key]()


ADAPTER_KEYS = (
    "uk_legislation",
    "eur_lex",
    "govinfo_us_usc",
    "govinfo_us_ecfr",
    "nist_sp800_53",
    "nist_csf",
)

# Adapters whose source has an official citator/relations feed (HLD §7.2).
CITATOR_KEYS = ("uk_legislation", "eur_lex")
