"""Local operator export of an owned year snapshot and explicitly selected originals."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from docket.config import Settings, get_settings
from docket.database import configure_database, get_session_factory
from docket.models import (
    AttachmentEvidence,
    AuthenticatedRequest,
    FinancialIntake,
    FinancialReportSnapshot,
)
from docket.services.funding import attachment_store


def write_members(
    session: Session, settings: Settings, report_ref: str, originals: list[str], output: BinaryIO
) -> None:
    owner = settings.canonical_operator_ref
    report = session.scalar(
        select(FinancialReportSnapshot).where(
            FinancialReportSnapshot.ref_id == report_ref,
            FinancialReportSnapshot.operator_ref == owner,
        )
    )
    if not report or report.parameters_json.get("report_kind") != "financial_year":
        raise ValueError("Select a retained year report owned by the configured operator.")
    evidence = {r["source_ref"]: r for r in report.result_json["evidence"]}
    if not set(originals) <= evidence.keys():
        raise ValueError("Select original references from the retained report.")
    with tarfile.open(fileobj=output, mode="w|") as archive:

        def add(name: str, content: bytes) -> None:
            entry = tarfile.TarInfo(name)
            entry.mode, entry.size = 0o600, len(content)
            archive.addfile(entry, io.BytesIO(content))

        add(
            "workpapers.json",
            json.dumps(
                dict(
                    report_ref=report_ref,
                    tax_year=report.parameters_json["tax_year"],
                    as_of=report.as_of.isoformat(),
                    sections={k: v for k, v in report.result_json.items() if k != "findings"},
                ),
                ensure_ascii=False,
                default=str,
            ).encode(),
        )
        for ref in sorted(set(originals)):
            source = session.get(FinancialIntake, ref)
            owned = source is not None and source.operator_ref == owner
            if not owned:
                owned = (
                    session.scalar(
                        select(AttachmentEvidence.ref_id)
                        .join(
                            AuthenticatedRequest,
                            AttachmentEvidence.authenticated_request_ref
                            == AuthenticatedRequest.ref_id,
                        )
                        .where(
                            AttachmentEvidence.ref_id == ref,
                            AuthenticatedRequest.operator_ref == owner,
                        )
                    )
                    is not None
                )
            if not owned or evidence[ref]["retention"] != "retained_encrypted":
                raise ValueError("An original is unavailable for this operator.")
            content = attachment_store(session, settings).plaintext(ref)
            if hashlib.sha256(content).hexdigest() != evidence[ref]["digest"]:
                raise ValueError("An original differs from the retained evidence index.")
            add("originals/" + ref, content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_ref")
    parser.add_argument("output", type=Path)
    parser.add_argument("--original", action="append", default=[])
    args = parser.parse_args()
    settings = get_settings()
    if not settings.backup_age_recipient:
        parser.error("Configure the operator's existing recovery encryption recipient.")
    configure_database(settings.database_url)
    with tempfile.NamedTemporaryFile(dir=args.output.parent) as tmp:
        with subprocess.Popen(
            ["age", "--encrypt", "--recipient", settings.backup_age_recipient],
            stdin=subprocess.PIPE,
            stdout=tmp,
            stderr=subprocess.DEVNULL,
        ) as encrypt:
            assert encrypt.stdin is not None
            with encrypt.stdin, get_session_factory()() as db:
                write_members(
                    db, settings, args.report_ref, args.original, cast(BinaryIO, encrypt.stdin)
                )
            if encrypt.wait():
                raise RuntimeError("The encrypted export could not be completed.")
        os.link(tmp.name, args.output)


if __name__ == "__main__":
    main()
