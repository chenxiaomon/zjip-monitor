"""Local archive of the production data — runs on the operator's Mac, never on the server.

The server's data/attachments is a working cache: every document can be fetched
from the platform again, except notices that a later 补正 round replaced and the
files of companies that were removed. This package pulls everything to
ARCHIVE_DIR (append-only: nothing is ever deleted locally) and indexes it into
archive.sqlite — records, status history, documents and the notices' text —
as the long-term record for improving future filings.

    python -m archive pull     # rsync the server's data/ here, append-only
    python -m archive index    # update archive.sqlite incrementally
    python -m archive release  # free server space: dry run unless --yes (certificates stay)
    python -m archive restore <企业目录> <登记号>   # send released files back
    python -m archive restore --certificates [--yes]  # send every released certificate back
    python -m archive bundle <企业>   # the 交付包 (certificate list + certificates) into 交付包/
    python -m archive schedule install   # pull by itself after each production scan (launchd)
    python -m archive auto     # what the schedule runs: notices first, the rest, index

Local-only dependency: pypdf (requirements-dev.txt).
"""
