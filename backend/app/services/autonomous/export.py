"""EXPORT: EPUB 3, DOCX, Markdown, plain text and JSON reports.

Everything is produced with the standard library (zipfile + XML strings) so
no new runtime dependency is introduced. The EPUB carries valid metadata, a
navigation document, an NCX for EPUB 2 readers, a title page and one XHTML
file per chapter in reading order.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import datetime
from html import escape
from typing import Any, Dict, Optional, Sequence, Tuple

from sqlmodel import Session, select

from app.db.models import AutonomousNovelJob, ExportArtifact, ModelInvocation
from app.services.autonomous.audit import chapter_texts
from app.services.bible.bible_service import BibleService
from app.services.forge.textmetrics import measure, split_paragraphs

EXPORT_VERSION = "export-1"


def _c(card) -> Dict[str, Any]:
    return card.content if card is not None and isinstance(card.content, dict) else {}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "novel").lower()).strip("-")[:60] or "novel"


def book_metadata(session: Session, project_id: int, job: AutonomousNovelJob) -> Dict[str, str]:
    from app.db.models import Project

    project = session.get(Project, project_id)
    foundation = _c(BibleService(session).singleton(project_id, "Story Foundation"))
    title = (job.options or {}).get("title") or (project.name if project else "Untitled Novel")
    return {"title": str(title), "author": str((job.options or {}).get("author") or "NovelForge"), "language": str((job.options or {}).get("language") or "en"), "description": str(foundation.get("core_premise") or ""), "identifier": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'novelforge-job-{job.id}')}"}


def _xhtml(title: str, body: str, *, lang: str) -> str:
    return f'<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{escape(lang)}" lang="{escape(lang)}"><head><title>{escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body>{body}</body></html>'


def _paragraphs_html(text: str) -> str:
    return "".join(f"<p>{escape(p.strip())}</p>" for p in split_paragraphs(text) if p.strip())


def build_epub(meta: Dict[str, str], chapters: Sequence[Tuple[int, str, str]], *, front_matter: str = "", back_matter: str = "") -> bytes:
    lang = meta.get("language") or "en"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="utf-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        zf.writestr("OEBPS/style.css", "body{font-family:serif;line-height:1.5;margin:1em}h1,h2{text-align:center}p{text-indent:1.2em;margin:0 0 .4em}")
        manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>', '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>', '<item id="css" href="style.css" media-type="text/css"/>', '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>']
        spine = ['<itemref idref="title"/>']
        nav_items = ['<li><a href="title.xhtml">Title Page</a></li>']
        ncx_points = ['<navPoint id="np-title" playOrder="1"><navLabel><text>Title Page</text></navLabel><content src="title.xhtml"/></navPoint>']
        zf.writestr("OEBPS/title.xhtml", _xhtml(meta["title"], f"<h1>{escape(meta['title'])}</h1><p style='text-align:center'>{escape(meta['author'])}</p>" + (f"<div class='front'>{_paragraphs_html(front_matter)}</div>" if front_matter else ""), lang=lang))
        order = 2
        for n, title, text in chapters:
            fname = f"chapter-{n:03d}.xhtml"
            manifest.append(f'<item id="ch{n}" href="{fname}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="ch{n}"/>')
            label = title or f"Chapter {n}"
            nav_items.append(f'<li><a href="{fname}">{escape(label)}</a></li>')
            ncx_points.append(f'<navPoint id="np{n}" playOrder="{order}"><navLabel><text>{escape(label)}</text></navLabel><content src="{fname}"/></navPoint>')
            order += 1
            zf.writestr(f"OEBPS/{fname}", _xhtml(label, f"<section epub:type='chapter'><h2>{escape(label)}</h2>{_paragraphs_html(text)}</section>", lang=lang))
        if back_matter:
            manifest.append('<item id="back" href="back.xhtml" media-type="application/xhtml+xml"/>')
            spine.append('<itemref idref="back"/>')
            nav_items.append('<li><a href="back.xhtml">Afterword</a></li>')
            ncx_points.append(f'<navPoint id="np-back" playOrder="{order}"><navLabel><text>Afterword</text></navLabel><content src="back.xhtml"/></navPoint>')
            zf.writestr("OEBPS/back.xhtml", _xhtml("Afterword", f"<h2>Afterword</h2>{_paragraphs_html(back_matter)}", lang=lang))
        zf.writestr("OEBPS/nav.xhtml", _xhtml("Contents", f"<nav epub:type='toc' id='toc'><h2>Contents</h2><ol>{''.join(nav_items)}</ol></nav>", lang=lang))
        zf.writestr("OEBPS/toc.ncx", f'<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="{escape(meta["identifier"])}"/><meta name="dtb:depth" content="1"/></head><docTitle><text>{escape(meta["title"])}</text></docTitle><navMap>{"".join(ncx_points)}</navMap></ncx>')
        modified = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        zf.writestr("OEBPS/content.opf", (
            '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{escape(meta["identifier"])}</dc:identifier><dc:title>{escape(meta["title"])}</dc:title><dc:creator>{escape(meta["author"])}</dc:creator><dc:language>{escape(lang)}</dc:language>'
            f'<dc:description>{escape(meta.get("description") or "")}</dc:description><meta property="dcterms:modified">{modified}</meta></metadata>'
            f'<manifest>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>'
        ))
    return buf.getvalue()


def build_docx(meta: Dict[str, str], chapters: Sequence[Tuple[int, str, str]]) -> bytes:
    def para(text: str, style: Optional[str] = None) -> str:
        ppr = f"<w:pPr><w:pStyle w:val=\"{style}\"/></w:pPr>" if style else ""
        return f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"

    body = [para(meta["title"], "Title"), para(meta["author"], "Subtitle")]
    for n, title, text in chapters:
        body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        body.append(para(title or f"Chapter {n}", "Heading1"))
        body += [para(p.strip()) for p in split_paragraphs(text) if p.strip()]
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{"".join(body)}<w:sectPr/></w:body></w:document>'
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:pPr><w:spacing w:after="120"/></w:pPr><w:rPr><w:sz w:val="24"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:b/><w:sz w:val="48"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:sz w:val="28"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="480" w:after="240"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
        '</w:styles>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>')
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>')
        zf.writestr("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)
        zf.writestr("docProps/core.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>{escape(meta["title"])}</dc:title><dc:creator>{escape(meta["author"])}</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}</dcterms:created></cp:coreProperties>')
    return buf.getvalue()


def build_markdown(meta: Dict[str, str], chapters: Sequence[Tuple[int, str, str]]) -> str:
    parts = [f"# {meta['title']}", f"*{meta['author']}*", ""]
    for n, title, text in chapters:
        parts += [f"## {title or f'Chapter {n}'}", "", text.strip(), ""]
    return "\n".join(parts)


def build_text(meta: Dict[str, str], chapters: Sequence[Tuple[int, str, str]]) -> str:
    parts = [meta["title"].upper(), meta["author"], ""]
    for n, title, text in chapters:
        parts += [(title or f"Chapter {n}").upper(), "", text.strip(), "", ""]
    return "\n".join(parts)


def synopsis_and_guide(session: Session, project_id: int) -> Tuple[str, str]:
    bible = BibleService(session)
    foundation = _c(bible.singleton(project_id, "Story Foundation"))
    lines = ["# Synopsis", "", foundation.get("core_premise") or "", "", f"**Central question:** {foundation.get('central_dramatic_question') or ''}", f"**Stakes:** {foundation.get('stakes') or ''}", ""]
    for card in bible.cards_of_type(project_id, "Chapter Text"):
        c = _c(card)
        if c.get("summary"):
            lines.append(f"- Chapter {c.get('chapter_number')}: {c['summary']}")
    guide = ["# Character Guide", ""]
    for card in bible.cards_of_type(project_id, "Character Card"):
        c = _c(card)
        guide += [f"## {c.get('name') or card.title} ({c.get('role_type') or ''})", c.get("description") or "", f"- Drive: {c.get('core_drive') or ''}", f"- Arc: {c.get('character_arc') or ''}", ""]
    return "\n".join(lines), "\n".join(guide)


def run_summary(session: Session, job: AutonomousNovelJob) -> Dict[str, Any]:
    rows = session.exec(select(ModelInvocation).where(ModelInvocation.job_id == job.id)).all()
    by_role: Dict[str, Dict[str, int]] = {}
    for r in rows:
        d = by_role.setdefault(r.role or "unknown", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "retries": 0, "errors": 0})
        d["calls"] += 1
        d["input_tokens"] += int(r.input_tokens or r.input_tokens_estimate or 0)
        d["output_tokens"] += int(r.output_tokens or 0)
        d["retries"] += int(r.retries or 0)
        d["errors"] += 1 if r.validation_status != "ok" else 0
    return {"job_id": job.id, "mode": job.mode, "chapter_count": job.chapter_count, "model_calls": len(rows), "input_tokens": sum(int(r.input_tokens or r.input_tokens_estimate or 0) for r in rows), "output_tokens": sum(int(r.output_tokens or 0) for r in rows), "by_role": by_role, "started_at": job.started_at.isoformat() if job.started_at else None, "finished_at": job.finished_at.isoformat() if job.finished_at else None, "warnings": job.warnings, "stage_results": {k: v for k, v in (job.stage_results or {}).items() if k not in ("CHAPTER_GENERATION_LOOP",)}}


def _store(session: Session, job: AutonomousNovelJob, project_id: int, kind: str, filename: str, media_type: str, data: bytes) -> ExportArtifact:
    existing = session.exec(select(ExportArtifact).where(ExportArtifact.job_id == job.id, ExportArtifact.kind == kind)).first()
    row = existing or ExportArtifact(job_id=job.id, project_id=project_id, kind=kind)
    row.filename = filename
    row.media_type = media_type
    row.size_bytes = len(data)
    row.content_hash = hashlib.sha256(data).hexdigest()
    row.data = data
    session.add(row)
    session.flush()
    return row


def stage_export(session: Session, *, job: AutonomousNovelJob, project_id: int, audit: Dict[str, Any]) -> Dict[str, Any]:
    meta = book_metadata(session, project_id, job)
    chapters = [(n, str(_c(card).get("title") or f"Chapter {n}"), text) for n, card, text in chapter_texts(session, project_id)]
    if not chapters:
        raise ValueError("No chapter texts to export")
    slug = _slug(meta["title"])
    synopsis, guide = synopsis_and_guide(session, project_id)
    quality = {"version": EXPORT_VERSION, "audit": {k: v for k, v in audit.items() if k != "findings"}, "findings": audit.get("findings", [])[:200], "chapters": [{"chapter": n, "title": t, "words": measure(x).unit_count} for n, t, x in chapters], "total_words": sum(measure(x).unit_count for _, _, x in chapters), "originality": audit.get("originality"), "unresolved_warnings": [f for f in audit.get("findings", []) if f.get("severity") in ("medium", "low")][:100], "run": run_summary(session, job)}
    artifacts = [
        _store(session, job, project_id, "epub", f"{slug}.epub", "application/epub+zip", build_epub(meta, chapters, front_matter=str((job.options or {}).get("front_matter") or ""), back_matter=str((job.options or {}).get("back_matter") or ""))),
        _store(session, job, project_id, "docx", f"{slug}.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", build_docx(meta, chapters)),
        _store(session, job, project_id, "markdown", f"{slug}.md", "text/markdown; charset=utf-8", build_markdown(meta, chapters).encode("utf-8")),
        _store(session, job, project_id, "text", f"{slug}.txt", "text/plain; charset=utf-8", build_text(meta, chapters).encode("utf-8")),
        _store(session, job, project_id, "synopsis", f"{slug}-synopsis.md", "text/markdown; charset=utf-8", synopsis.encode("utf-8")),
        _store(session, job, project_id, "character_guide", f"{slug}-characters.md", "text/markdown; charset=utf-8", guide.encode("utf-8")),
        _store(session, job, project_id, "report", f"{slug}-quality-report.json", "application/json", json.dumps(quality, ensure_ascii=False, indent=1, default=str).encode("utf-8")),
    ]
    session.commit()
    return {"artifacts": [{"id": a.id, "kind": a.kind, "filename": a.filename, "size_bytes": a.size_bytes} for a in artifacts], "total_words": quality["total_words"], "chapters": len(chapters)}


__all__ = ["EXPORT_VERSION", "book_metadata", "build_docx", "build_epub", "build_markdown", "build_text", "run_summary", "stage_export", "synopsis_and_guide"]
