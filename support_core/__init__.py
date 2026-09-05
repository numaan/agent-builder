"""support-core: the reusable customer support conversation library (DESIGN.md section 1, 18).

Package layout follows DESIGN.md section 18. The public API that packs import
(``Tool``, ``Risk``, ``Node``, ``NodeResult``, ``load_pack``, ``create_app``) is added as
the phases that implement those objects land (phases 1, 4 and 7); nothing is re-exported
here until it exists. Phase 1 adds ``load_pack`` and ``Risk``.
"""

__version__ = "0.0.1"

# Imported after ``__version__`` on purpose: ``support_core.graph.manifest`` reads it from this
# partially-initialised module while the import below is running.
from support_core.graph.loader import load_pack
from support_core.tools.risk import Risk

__all__ = ["Risk", "__version__", "load_pack"]
