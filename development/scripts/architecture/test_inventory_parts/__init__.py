"""Typed interior parts of the test-inventory authority.

Epic 10-D split this authority for a reason the 10-C handover got wrong, and
the correction matters enough to record here.

``python_scan.is_test_path`` classifies by **filename**, and the parent module
is called ``test_inventory.py``.  The scanner therefore treats a piece of
production governance machinery as a *test module*: its cap is 1200 rather
than 700, and its callables are exempt from the 100-logical-line cap — which
is why ``regenerate_inventory`` sits at 159 logical lines with zero recorded
violations.  The parent was never one line from its budget; it had roughly
500 lines of slack under a budget it should never have been given.

Renaming the parent is not available: ``development/scripts/architecture/test_inventory.py``
is one of the fourteen frozen ``_PKG02_TEST_PATHS`` and a member of
``seal.PROTECTED``, so a rename would delete a frozen path from
``python_test_files`` and break the PKG-02 transition relation.

So the parent stays, and logic moves *out* into this package, whose modules
are **not** test paths and therefore answer to the real 700/100 caps for the
first time.  New machinery lands here by default.

The parent keeps every name the adversarial suite monkeypatches through the
``test_inventory`` module object — the collectors, ``scan_mapping_static``,
``accepted_mapping_identity``, ``load_test_inventory`` and ``_config_identities``
— together with the orchestrators that read them, because a patch only takes
effect on the module whose globals the reader consults.

Every module here carries change-controlled governance bytes and is a member
of ``seal.PROTECTED`` in its own right.
"""

from __future__ import annotations
