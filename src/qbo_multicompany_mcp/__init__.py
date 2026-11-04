"""Read-only MCP server for several QuickBooks Online company files.

Intuit issues one OAuth token pair per company file (a "realm"; the consent
callback names the company), and the ``com.intuit.quickbooks.accounting`` scope
covers reading and writing alike -- Intuit's scope list has no read-only
variant. Read-only is therefore a property of this code, enforced at run time
by :mod:`qbo_multicompany_mcp.netpolicy` and tested by the suite, not a
property of the credential.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
