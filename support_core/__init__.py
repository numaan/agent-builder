"""support-core: the reusable customer support conversation library (DESIGN.md section 1, 18).

Package layout follows DESIGN.md section 18. The public API that packs import
(``Tool``, ``Risk``, ``Node``, ``NodeResult``, ``load_pack``, ``create_app``) is added as
the phases that implement those objects land (phases 1, 4 and 7); nothing is re-exported
here until it exists.
"""

__version__ = "0.0.1"
